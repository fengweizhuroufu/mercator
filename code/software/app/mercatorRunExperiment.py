#!/usr/bin/python

# =========================== adjust path =====================================

import os
import sys
if __name__ == '__main__':
    here = sys.path[0]
    sys.path.insert(0, os.path.join(here, '..', 'lib'))

# =========================== imports =========================================

import argparse
import threading
import json
import datetime
import logging.config
import gzip
import socket

# Mercator
import MoteHandler
import MercatorDefines as d

# IoT-lab
import iotlabcli as iotlab
from iotlabcli import experiment, node

# =========================== logging =========================================

logging.config.fileConfig('logging.conf')

logconsole  = logging.getLogger("console")
logfile     = logging.getLogger()  #root logger

# =========================== constants =======================================

FIRMWARE_PATH   = "../../firmware/"
DATASET_PATH    = "./"
METAS_PATH      = "../../../metas/"

# =========================== body ============================================

class MercatorRunExperiment(object):

    FREQUENCIES    = [n+11 for n in range(16)]   # frequencies to measure on, in IEEE notation
    TXIFDUR        = 10                          # inter-frame duration, in ms
    TXFILLBYTE     = 0x0a                        # padding byte

    def __init__(self, args, serialports, site="local"):

        # local variables
        self.dataLock        = threading.Lock()
        self.transctr        = 0
        self.motes           = {}
        self.isTransmitting  = False
        self.site            = site
        self.freq            = self.FREQUENCIES[0]
        self.transmitterPort = ""
        self.nbtrans         = args.nbtrans
        self.nbpackets       = args.nbpackets
        self.txpksize        = args.txpksize
        self.txpower         = args.txpower
        self.experiment_id   = args.expid

        # use the file created by auth-cli command
        usr, pwd = iotlab.get_user_credentials()

        # authenticate through the REST interface
        self.api = iotlab.rest.Api(usr, pwd)

        # connect to motes
        for s in serialports:
            logfile.debug("connected to %s", s)
            self.motes[s]    = MoteHandler.MoteHandler(s, self._cb, reset_cb=self._reset_cb)
            if not self.motes[s].isActive:
                raise Exception("Mote {0} is not responding.".format(s))

        # get current datetime
        now = datetime.datetime.now().strftime("%Y.%m.%d-%H.%M.%S")

        # open file
        self.file = gzip.open(
            '{0}{1}-{2}_raw.csv.gz'.format(
                DATASET_PATH,
                self.site,
                now
            ),
            'wb'
        )

        # write settings
        settings = {
            "interframe_duration": self.TXIFDUR,
            "fill_byte": self.TXFILLBYTE,
            "tx_length": self.txpksize,
            "tx_count": self.nbpackets,
            "transaction_count": self.nbtrans,
            "node_count": len(self.motes),
            "location": self.site,
            "channel_count": len(self.FREQUENCIES),
            "start_date": now,
            "txpower": self.txpower
        }
        json.dump(settings, self.file)
        self.file.write('\n')

        # write csv header
        self.file.write('datetime,src,dst,channel,rssi,crc,expected,' +
                        'transaction_id,pkctr\n')

        try:
            # start transactions
            for self.transctr in range(0, self.nbtrans):
                logconsole.info("Current transaction: %s", self.transctr)
                self._do_transaction()
        except (KeyboardInterrupt, socket.error):
            # print error
            print('\nExperiment ended before all transactions were done.')
        else:
            # print all OK
            print('\nExperiment ended normally.')
        finally:
            self.file.close()

    # ======================= public ==========================================

    # ======================= cli handlers ====================================

    def _do_transaction(self):

        for freq in self.FREQUENCIES:
            logconsole.info("Current frequency: %s", freq)
            self._do_experiment_per_frequency(freq)

    def _do_experiment_per_frequency(self, freq):

        for counter, transmitterPort in enumerate(self.motes):
            self._do_experiment_per_transmitter(freq, transmitterPort)
            if counter % (1+len(self.motes)/4) == 0:
                logconsole.info("%d/%d", counter, len(self.motes))

    def _do_experiment_per_transmitter(self, freq, transmitter_port):

        self.transmitterPort = transmitter_port
        self.freq            = freq
        logfile.debug('freq=%s transmitter_port=%s', freq, transmitter_port)

        # switch all motes to idle
        for (sp, mh) in self.motes.items():
            logfile.debug('    switch %s to idle', sp)
            mh.send_REQ_IDLE()

        # check state, assert that all are idle
        for (sp, mh) in self.motes.items():
            status = mh.send_REQ_ST()
            if status is None or status['status'] != d.ST_IDLE:
                logfile.warn('Node %s is not in IDLE state.', mh.mac)

        # switch all motes to rx
        for (sp, mh) in self.motes.items():
            logfile.debug('    switch %s to RX', sp)
            mh.send_REQ_RX(
                frequency         = freq,
                srcmac            = self.motes[transmitter_port].get_mac(),
                transctr          = self.transctr,
                txpksize          = self.txpksize,
                txfillbyte        = self.TXFILLBYTE,
            )

        # check state, assert that all are in rx mode
        for (sp, mh) in self.motes.items():
            status = mh.send_REQ_ST()
            if status is None or status['status'] != d.ST_RX:
                logfile.warn('Node %s is not in RX state.', mh.mac)

        # switch tx mote to tx
        logfile.debug('    switch %s to TX', transmitter_port)

        with self.dataLock:
            self.waitTxDone       = threading.Event()
            self.isTransmitting   = True

        self.motes[transmitter_port].send_REQ_TX(
            frequency             = freq,
            txpower               = self.txpower,
            transctr              = self.transctr,
            nbpackets             = self.nbpackets,
            txifdur               = self.TXIFDUR,
            txpksize              = self.txpksize,
            txfillbyte            = self.TXFILLBYTE,
        )

        # wait to be done
        maxwaittime = 3*self.nbpackets*(self.TXIFDUR/1000.0)
        self.waitTxDone.wait(maxwaittime)
        if self.waitTxDone.isSet():
            logfile.debug('done.')
        else:
            #raise SystemError('timeout when waiting for transmission
            # to be done (no IND_TXDONE after {0}s)'.format(maxwaittime))
            return

        # check state, assert numnotifications is expected
        for (sp, mh) in self.motes.items():
            status = mh.send_REQ_ST()
            if sp == transmitter_port:
                if status is None or status['status'] != d.ST_TXDONE:
                    logfile.warn('Node %s is not in TXDONE state.', mh.mac)
            else:
                if status is None or status['status'] != d.ST_RX:
                    logfile.warn('Node %s is not in RX state.', mh.mac)

    # ======================= private =========================================

    def _cb(self, serialport, notif):

        if isinstance(notif, dict):
            if   notif['type'] == d.TYPE_RESP_ST:
                print 'state {0}'.format(serialport)
            elif notif['type'] == d.TYPE_IND_TXDONE:
                with self.dataLock:
                    # assert self.isTransmitting
                    self.isTransmitting   = False
                    self.waitTxDone.set()
            elif notif['type'] == d.TYPE_IND_RX:
                timestamp  = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f")
                src        = d.format_mac(self.motes[self.transmitterPort].get_mac())
                dst        = d.format_mac(self.motes[serialport].get_mac())
                frequency  = self.freq
                rssi       = notif['rssi']
                crc        = notif['crc']
                expected   = notif['expected']
                transctr   = self.transctr
                pkctr      = notif['pkctr']
                self.file.write("{0},{1},{2},{3},{4},{5},{6},{7},{8}\n".format(
                        timestamp,
                        src,
                        dst,
                        frequency,
                        rssi,
                        crc,
                        expected,
                        transctr,
                        pkctr,
                    ))
            elif notif['type'] == d.TYPE_IND_UP:
                logfile.debug("Node %s restarted",
                              d.format_mac(self.motes[serialport].get_mac()))

    def _reset_cb(self, mote):
        logfile.debug('restarting mote {0}'.format(mote.serialport))
        mote_url = ".".join([mote.serialport, self.site, "iot-lab.info"])
        node.node_command(self.api, 'reset', self.experiment_id, [mote_url])
        logfile.debug('mote {0} restarted'.format(mote.serialport))

    @staticmethod
    def _quit_callback():
        print "quitting!"

# ========================== helpers ==========================================


def get_motes(expid):
    # use the file created by auth-cli command
    usr, pwd    = iotlab.get_user_credentials()

    # authenticate through the REST interface
    api = iotlab.rest.Api(usr, pwd)

    # get experiment resources
    data = experiment.get_experiment(api, expid, 'resources')

    return (map(lambda x: x["network_address"].split('.')[0], data["items"]),
            data["items"][0]["network_address"].split('.')[1])


def submit_experiment(args):
    """
    Reserve nodes in the given site.
    The function uses the json experiment file corresponding to the site.
    :param str firmware: the name of the firmware as it is in the code/firmware/ folder
    :param str board: the type of board (ex: m3)
    :param str testbed: The name of the testbed (ex: grenoble)
    :param int duration: The duration of the experiment in minutes
    :param int nbnodes: The number of nodes to use
    :return: The id of the experiment
    """

    # use the file created by auth-cli command
    usr, pwd    = iotlab.get_user_credentials()

    # authenticate through the REST interface
    api         = iotlab.rest.Api(usr, pwd)

    # load the experiment
    firmware    = FIRMWARE_PATH + args.firmware
    profile     = "mercator"
    if args.nbnodes != 0:
        if args.board == "m3":
            args.board = "m3:at86rf231"
        nodes = experiment.AliasNodes(args.nbnodes, args.testbed, args.board)
    else:
        tb_file = open("{0}states.json".format(METAS_PATH))
        tb_json = json.load(tb_file)
        nodes = [x for x in tb_json[args.testbed] if args.board in x]
    resources = [experiment.exp_resources(nodes, firmware, profile)]

    # submit experiment
    logconsole.info("Submitting experiment.")
    expid       = experiment.submit_experiment(
                    api, "mercatorExp", args.duration,
                    resources)["id"]

    logconsole.info("Experiment submited with id: %u", expid)
    logconsole.info("Waiting for experiment to be running.")
    experiment.wait_experiment(api, expid)

    return expid

# =========================== main ============================================


def main():

    # parsing user arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("testbed", help="The name of the current testbed")
    parser.add_argument("firmware", help="The firmware to flash", type=str)
    parser.add_argument("-d", "--duration", help="Duration of the experiment in minutes", type=int, default=30)
    parser.add_argument("-e", "--expid", help="The experiment id", type=int, default=None)
    parser.add_argument("-b", "--board", help="The type of board to use", type=str, default="m3")
    parser.add_argument("-n", "--nbnodes", help="The number of nodes to use (0=all)", type=int, default=0)
    parser.add_argument("-p", "--nbpackets", help="The number of packet per transaction", type=int, default=100)
    parser.add_argument("-t", "--nbtrans", help="The number of transaction", type=int, default=1)
    parser.add_argument("-s", "--txpksize", help="The size of each packet in bytes", type=int, default=100)
    parser.add_argument("--txpower", help="The transmission power (dBm)", type=int, default=0)
    args = parser.parse_args()

    if args.testbed == "local":
        MercatorRunExperiment(
            args = args,
            serialports = ['/dev/ttyUSB1', '/dev/ttyUSB3'],
        )
    else:
        if args.expid is None:
            expid = submit_experiment(args)
        else:
            expid = args.expid
        (serialports, site) = get_motes(expid)
        MercatorRunExperiment(
            args = args,
            serialports = serialports,
            site = site,
        )

if __name__ == '__main__':
    main()
