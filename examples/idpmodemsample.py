#!/usr/bin/env python
"""
Periodically queries modem status, checks for incoming messages 
and sends location reports

Mobile-Originated location reports are 17 bytes using SIN 255 MIN 255
Mobile-Terminated location interval change uses SIN 255 MIN 1, 
with 1 byte payload for the new interval in minutes.
When a new interval is configured, a location report is generated 
immediately, thereafter at the new interval.

.. todo::

   * Restructure parse_args for automagic documentation with sphinx argparse extension

"""
from __future__ import absolute_import

import argparse
import binascii
import datetime
import sys
import traceback

from idpmodem import idpmodem, idpcodec, headless
from idpmodem.headless import get_wrapping_logger

__version__ = "1.1.0"

global log                  
global modem                # the instance of IDP modem class
global tracking_interval    # minutes


def handle_mt_tracking_command(message):
    """
    Expects to get SIN 255 MIN 1 to reconfigure tracking interval, 
    in minutes, in a range from 1-1440.

    :param message: MobileTerminatedMessage

       - ``sin`` Service Identifier Number
       - ``min`` Message Identifier Number
       - ``payload`` (including MIN byte)
       - ``size`` in bytes including SIN, MIN
    """
    # TODO: Additional testing
    global log
    global modem
    global tracking_interval

    if (message['sin'] == 255 
        and message['min'] == 1 
        and message['data_format'] == 2):
        # Format: <SIN><MIN><tracking_interval> where tracking_interval is a 2-byte value in minutes
        payload = binascii.hexlify(bytearray(message['payload']))
        new_interval_minutes = int(payload[2:], 16)
        if (0 <= new_interval_minutes <= 1440
            and new_interval_minutes * 60 != tracking_interval):
            # Change tracking interval
            log.info("Changing tracking interval to {} seconds"
                     .format(new_interval_minutes * 60))
            tracking_interval = new_interval_minutes * 60
            modem.tracking_setup(interval=tracking_interval)
        else:
            log.warning("Invalid tracking interval requested " \
                        "({} minutes not in range 0..1440)"
                        .format(new_interval_minutes))
            # TODO: send an error response indicating 'invalid interval' over the air
    else:
        log.warning("Unsupported command SIN={} MIN={}"
                    .format(message['sin'], message['min']))


def send_idp_location(loc):
    """
    Prepares a specific binary-optimized location report
    using SIN=255, MIN=255.

    :param loc: a Location object
    :return: Boolean success
    """
    global modem
    msg_sin = 255
    msg_min = 255
    payload = idpcodec.CommonMessageFormat(msg_sin=msg_sin, 
                                           msg_min=msg_min, 
                                           name='location')
    payload.add_field('timestamp', 'uint_32', loc.timestamp, '031b')
    payload.add_field('latitude', 'int_32', loc.latitude, '024b')
    payload.add_field('longitude', 'int_32', loc.longitude, '025b')
    payload.add_field('speed', 'int_16', loc.speed, '08b')
    payload.add_field('heading', 'int_16', loc.heading, '09b')
    payload.add_field('satellites', 'int_8', loc.satellites, '04b')
    payload.add_field('fixtype', 'int_8', loc.fixtype, '02b')
    payload.add_field('pdop', 'int_8', loc.PDOP, '04b')
    payload.delete_field('pdop')
    data_str = payload.encode_idp(data_format=3)
    # message_name = 'LOC'
    message = idpmodem.MobileOriginatedMessage(payload=data_str, 
                                               data_format=3, 
                                               msg_sin=msg_sin, 
                                               msg_min=msg_min)
    modem.send_message(message)


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")

    parser.add_argument('-l', '--log', dest='logfile', type=str, default='idpmodemsample',
                        help="the log file name with optional extension (default extension .log)")

    parser.add_argument('-s', '--logsize', dest='log_size', type=int, default=5,
                        help="the maximum log file size, in MB (default 5 MB)")

    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        help="enable verbose debug logging (default OFF)")

    parser.add_argument('-c', '--crc', dest='use_crc', action='store_true',
                        help="force use of CRC on serial port (default OFF)")

    parser.add_argument('-t', '--track', dest='tracking', type=int, default=15,
                        help="location reporting interval in minutes (0..1440, default = 15, 0 = disabled)")

    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB0',
                        help="the serial port of the IDP modem")

    return vars(parser.parse_args(args=argv[1:]))


def main():
    """
    Sets up timer_threads for polling satellite status, incoming 
    over-the-air messages, and location updates.
    Monitors the serial connection to the modem 
    and re-initializes on reconnect.
    """
    global log
    global modem
    global tracking_interval

    SERIAL_BAUD = 9600
    modem = None

    # Timer intervals (seconds)
    SAT_STATUS_INTERVAL = 5
    MT_MESSAGE_CHECK_INTERVAL = 15

    # Derive run options from command line
    user_options = parse_args(sys.argv)
    serial_name = user_options['port']
    if '.' not in user_options['logfile']:
        logfile = user_options['logfile'] + '.log'
    else:
        logfile = user_options['logfile']
    log_size = user_options['log_size']
    debug = user_options['debug']
    if 0 <= user_options['tracking'] <= 1440:
        tracking_interval = int(user_options['tracking'] * 60)
    else:
        sys.exit("Invalid tracking interval, must be in range 0..1440")

    # Set up log file
    log = get_wrapping_logger(filename=logfile)
    sys.stdout.flush()

    # log.debug("**** PROGRAM STARTING ****")

    ever_connected = False
    start_time = str(datetime.datetime.utcnow())
    try:
        modem = idpmodem.Modem(serial_name=serial_name, log=log)
        success, error = modem.register_event_callback(event='new_mt_message',
            callback=handle_mt_tracking_command)
        # TODO: modem.register_event_callback(event='blocked', callback=tbd)
        modem.tracking_setup(interval=tracking_interval, 
            on_location=send_idp_location)
        while True:
            pass

    except KeyboardInterrupt:
        log.info("Execution stopped by keyboard interrupt.")

    except Exception:
        err_str = "Exception in user code:" + '-' * 40 + '\n' + traceback.format_exc()
        # err_str = "Error on line {}:".format(sys.exc_info()[-1].tb_lineno) + ',' + str(type(e)) + ',' + str(e)
        log.error(err_str)
        raise

    finally:
        end_time = str(datetime.datetime.utcnow())
        if modem is not None:
            log.info("*** Statistics from %s to %s ***" % (start_time, end_time))
            modem.log_statistics()
            modem.terminate()
        log.debug("\n\n*** END PROGRAM ***\n\n")


if __name__ == "__main__":
    main()
