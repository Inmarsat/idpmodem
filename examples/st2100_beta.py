#!/usr/bin/env python3
#coding: utf-8
"""
Periodically queries modem status and writes to a log file

"""
from __future__ import absolute_import

import argparse
import binascii
import os
import sys

from idpmodem import idpmodem
from idpmodem.codecs import common as idpcodec
from idpmodem.idpmodem import FORMAT_B64, FORMAT_HEX
from idpmodem.utils import get_wrapping_logger, RepeatingTimer


__version__ = "1.0.0"


global modem
global tracking_interval
global log
global snr
STATS_LIST = [
    ("systemStats", "2,3"),
    ("satcomStats", "2,4"),
    ("rxMetricsHour", "3,18"),
    ("txMetricsHour", "3,22"),
    ("gnssFixStats", "4,1"),
    #: ("lowPowerStats", "2,2"),
    ("status", "3,1"),
]


def log_stats(stats):
    """Logs the requested statistics.
    
    EVNT responses have the format:
    <dataCount>,<signedBitmask>,<MTID>,<timestamp>,<class>,<subclass>,
    <priority>,<data0>,<data1>,...,<dataN>
    
    Up to 24 x 32-bit values supported, only dataCount are populated.
    Timestamp is referenced to 2001-01-01T00:00:00Z

    """
    global modem
    global log
    global snr
    for i in range(len(STATS_LIST)):
        # at_cmd = '%EVNT={}'.format(STATS_LIST[i][1])
        to_log = '{},{}'.format(
            modem.mobile_id,
            stats[i].replace('%EVNT', '%EVNT{}'.format(STATS_LIST[i][1]))
        )
        log.info(to_log)
        if STATS_LIST[i][1] == '3,1':
            status_metrics = stats[i].replace('%EVNT:', '').strip().split(',')
            snr = round(int(status_metrics[23]) / 100, 2)


def get_stats():
    """Requests relevant beta trial statistics."""
    global modem
    event_str = ''
    for i in range(len(STATS_LIST)):
        event_str += '%EVNT={}'.format(STATS_LIST[i][1])
        if i < len(STATS_LIST) - 1:
            event_str += ';'
    modem.send_raw('AT{}'.format(event_str), callback=log_stats)


def handle_mt_messages(message_queue):
    """Processes Mobile-Terminated messages with SIN 255.
    
    Args:
        message_queue: list of pending MT messages

    """
    for msg in message_queue:
        if msg.sin == 255:
            modem.mt_message_get(msg.q_name,
                                 callback=handle_mt_tracking_command,
                                 data_format=FORMAT_HEX)


def handle_mt_tracking_command(message):
    """A remote command reconfigures the tracking interval.

    Expects to get SIN 255 MIN 1 to reconfigure tracking interval, 
    in minutes, in a range from 1-1440.

    Args:
        message: MobileTerminatedMessage

       - ``sin`` Service Identifier Number
       - ``min`` Message Identifier Number
       - ``payload`` (including MIN byte)
       - ``size`` in bytes including SIN, MIN
    """
    global log
    global modem
    global tracking_interval

    if (message.sin == 255 
        and message.min == 1 
        and message.data_format == FORMAT_HEX):
        # Format: <SIN><MIN><tracking_interval> where tracking_interval is a 2-byte value in minutes
        payload = binascii.hexlify(bytearray(message.payload))
        new_interval_minutes = int(payload[2:], 16)
        if (0 <= new_interval_minutes <= 1440
            and new_interval_minutes * 60 != tracking_interval):
            # Change tracking interval
            log.info("Changing tracking interval to {} minutes"
                     .format(new_interval_minutes))
            tracking_interval = new_interval_minutes
            tracking_seconds = round(tracking_interval / 60, 0)
            modem.tracking_setup(interval=tracking_seconds)
        else:
            log.warning("Invalid tracking interval requested " \
                        "({} minutes not in range 0..1440)"
                        .format(new_interval_minutes))
            # TODO: send an error response indicating 'invalid interval' over the air
    else:
        log.warning("Unsupported command SIN={} MIN={}"
                    .format(message.sin, message.min))


def send_idp_location(loc):
    """
    Prepares a specific binary-optimized location report
    using SIN=255, MIN=255.

    :param loc: a Location object
    :return: Boolean success
    """
    global modem
    global snr
    # Prepare data content
    msg_sin = 255
    msg_min = 255
    lat_milliminutes = int(loc.latitude * 60000)
    lng_milliminutes = int(loc.longitude * 60000)
    alt_m = int(loc.altitude)
    spd_kph = int(loc.speed * 1.852)
    hdg = int(loc.heading)
    #: pdop = int(loc.pdop)
    data_format = FORMAT_B64
    # Build message bit-optimized
    payload = idpcodec.CommonMessageFormat(msg_sin=msg_sin, 
                                           msg_min=msg_min, 
                                           name='location')
    payload.add_field('timestamp', 'uint_32', loc.timestamp, bits=32)
    payload.add_field('latitude', 'int_32', lat_milliminutes, bits=24)
    payload.add_field('longitude', 'int_32', lng_milliminutes, bits=25)
    payload.add_field('altitude', 'int_16', alt_m, bits=16)
    payload.add_field('speed', 'int_16', spd_kph, bits=8)
    payload.add_field('heading', 'int_16', hdg, bits=9)
    payload.add_field('satellites', 'int_8', loc.satellites, bits=4)
    payload.add_field('fixtype', 'int_8', loc.fix_type, bits=2)
    payload.add_field('snr', 'int_16', int(snr * 10), bits=9)
    # Get binary string payload to send
    data_str = payload.encode_idp(data_format=data_format)
    # Create message wrapper with SIN/MIN
    message = idpmodem.MobileOriginatedMessage(payload=data_str, 
                                               data_format=data_format, 
                                               msg_sin=msg_sin, 
                                               msg_min=msg_min)
    modem.mo_message_send(message)


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")
    dir_path = os.path.dirname(os.path.realpath(__file__))
    logfilename = dir_path + '/st2100_beta.log'
    parser.add_argument('-l', '--log', dest='logfile', type=str, default=logfilename,
                        help="the log file name with optional extension (default extension .log)")
    parser.add_argument('-s', '--logsize', dest='log_size', type=int, default=5,
                        help="the maximum log file size, in MB (default 5 MB)")
    parser.add_argument('-i', '--interval', dest='interval', type=int, default=900,
                        help="stats logging interval in seconds")
    parser.add_argument('-t', '--tracking', dest='tracking', type=int, default=15,
                        help="tracking interval in minutes")
    parser.add_argument('--debug', dest='debug', action='store_true',
                        help="enable verbose debug logging")
    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB0',
                        help="the serial port of the IDP modem")
    return vars(parser.parse_args(args=argv[1:]))


def main():
    global modem
    global log
    global tracking_interval

    user_options = parse_args(sys.argv)
    port = user_options['port']
    interval = user_options['interval']
    logfilename = user_options['logfile']
    logsize = user_options['log_size']
    tracking_interval = int(user_options['tracking'])
    tracking_seconds = int(tracking_interval * 60)
    debug = user_options['debug']

    modem = None
    stats_monitor = None
    log = get_wrapping_logger(name='st2100_beta_log',
                              filename=logfilename,
                              file_size=logsize,
                              debug=debug)
    log.info('{}Starting ST2100 Beta{}'.format('*' * 15, '*' * 15))
    try:
        modem = idpmodem.Modem(serial_name=port, baudrate=9600, 
                               log=log, debug=debug)
        while not modem.is_initialized:
            pass
        stats_monitor = RepeatingTimer(interval, name='beta_stats', defer=False, 
                                    callback=get_stats)
        stats_monitor.start_timer()
        modem.mt_message_callback_add(255, handle_mt_tracking_command)
        modem.tracking_setup(interval=tracking_seconds, 
                             on_location=send_idp_location)
        while True:
            pass
    
    except KeyboardInterrupt:
        print('Interrupted by user')
    
    except Exception as e:
        print(e)
    
    finally:
        # stats_monitor.join()
        if modem is not None:
            modem.terminate()
        if stats_monitor is not None:
            stats_monitor.terminate()
        sys.exit()


if __name__ == '__main__':
    main()
