#!/usr/bin/env python
"""
Periodically queries modem status and writes to a log file

"""
from __future__ import absolute_import

import argparse
import sys

from idpmodem import idpmodem
from idpmodem.utils import get_wrapping_logger, RepeatingTimer

__version__ = "1.0.0"


global modem
global snr_log


def log_snr(ctrl_state, snr):
    global snr_log
    snr_log.info('{},{}'.format(ctrl_state, snr))


def get_snr():
    global modem
    modem.get_sat_status(callback=log_snr)


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")

    parser.add_argument('-l', '--log', dest='logfile', type=str, default='snr.log',
                        help="the log file name with optional extension (default extension .log)")

    parser.add_argument('-s', '--logsize', dest='log_size', type=int, default=5,
                        help="the maximum log file size, in MB (default 5 MB)")

    parser.add_argument('-i', '--interval', dest='interval', type=int, default=30,
                        help="snr logging interval in seconds")

    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB1',
                        help="the serial port of the IDP modem")

    return vars(parser.parse_args(args=argv[1:]))


def main():
    global modem
    global snr_log

    user_options = parse_args(sys.argv)
    port = user_options['port']
    interval = user_options['interval']
    logfilename = user_options['logfile']
    logsize = user_options['log_size']

    modem = None
    snr_log = get_wrapping_logger(name='snr_log', filename=logfilename, 
                                file_size=logsize)
    try:
        modem = idpmodem.Modem(serial_name=port, baudrate=9600,
                                auto_monitor=False, debug=True)
        while not modem.is_initialized:
            pass
        snr_monitor = RepeatingTimer(interval, name='snr_monitor', defer=False, 
                                    callback=get_snr)
        snr_monitor.start_timer()
        while True:
            pass
    
    except KeyboardInterrupt:
        print('Interrupted by user')
    
    except Exception as e:
        print(e)
    
    finally:
        # snr_monitor.join()
        if modem is not None:
            modem.terminate()


if __name__ == '__main__':
    main()
