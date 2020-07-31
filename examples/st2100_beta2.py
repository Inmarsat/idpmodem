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
from time import sleep, time
import traceback

from idpmodem.protocol_factory import get_modem_thread, IdpModemBusy, AtException, AtCrcConfigError, AtCrcError, AtTimeout
from idpmodem.message import MobileOriginatedMessage, MobileTerminatedMessage
from idpmodem.codecs import common as idpcodec
from idpmodem.constants import FORMAT_B64, FORMAT_HEX
from idpmodem.utils import get_wrapping_logger, RepeatingTimer


__version__ = "2.0.0"


global modem
global tracking_interval
global tracking_thread
global log
global snr
global network_state
snr = 0.0
network_state = 0
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
    global network_state
    for i in range(len(STATS_LIST)):
        # at_cmd = '%EVNT={}'.format(STATS_LIST[i][1])
        to_log = '{}'.format(
            stats[i].replace('%EVNT', '%EVNT{}'.format(STATS_LIST[i][1]))
        )
        log.info(to_log)
        if STATS_LIST[i][1] == '3,1':
            status_metrics = stats[i].replace('%EVNT:', '').strip().split(',')
            snr = round(int(status_metrics[23]) / 100, 2)
            network_state = int(status_metrics[29])
            log.debug('C/No={} | State={}'.format(snr, network_state))

def get_status():
    global log
    global modem
    global network_state
    TIMEOUT = 10
    responses = modem.raw_command('ATS90=3 S91=1 S92=1 S122?', TIMEOUT)
    if 'OK' in responses:
        network_state = int(responses[0])


def get_stats():
    """Requests relevant beta trial statistics."""
    global log
    global modem
    event_str = ''
    TIMEOUT = 10
    for i in range(len(STATS_LIST)):
        event_str += '%EVNT={}'.format(STATS_LIST[i][1])
        if i < len(STATS_LIST) - 1:
            event_str += ';'
    log.debug('Getting satellite statistics')
    try:
        responses = modem.raw_command('AT{}'.format(event_str), TIMEOUT)
        if 'OK' in responses:
            responses.remove('OK')
            log_stats(responses)
        else:
            log.warning('Error getting statistics...likely no hourly metrics exist yet')
    except IdpModemBusy:
        log.warning('Timed out modem busy')
    except Exception as e:
        log.error(e)


def handle_mt_messages():
    """Processes Mobile-Terminated messages with SIN 255.
    
    Args:
        message_queue: list of pending MT messages

    """
    global log
    global modem
    global tracking_interval
    log.debug('Checking forward messages')
    try:
        mt_messages_queued = modem.message_mt_waiting()
        if isinstance(mt_messages_queued, list):
            if len(mt_messages_queued) > 0:
                for msg in mt_messages_queued:
                    if msg['sin'] == 255:
                        try:
                            log.info('Retrieving forward message SIN 255')
                            data = modem.message_mt_get(msg['name'], data_format=FORMAT_HEX)
                            if data is None:
                                log.error('Failed to retreive message {}'.format(msg['name']))
                            else:
                                data = data.replace('0x', '')
                                msg_sin = int(data[0:2], 16)
                                msg_min = int(data[2:4], 16)
                                if msg_sin == 255 and msg_min == 1:
                                    interval = int(data[4:], 16)
                                    update_tracking_interval(interval)
                        except IdpModemBusy:
                            log.warning('Timed out modem busy')
            else:
                log.debug('No forward messages queued')
    except IdpModemBusy:
        log.warning('Timed out modem busy')


def update_tracking_interval(interval_minutes):
    """A remote command reconfigures the tracking interval.

    Args:
        interval_minutes: The new interval in minutes

    """
    global log
    global tracking_interval
    global tracking_thread
    
    if (0 <= interval_minutes <= 1440
        and interval_minutes != tracking_interval):
        # Change tracking interval
        log.info("Changing tracking interval to {} minutes"
                    .format(interval_minutes))
        tracking_interval = interval_minutes
        tracking_thread.change_interval(int(tracking_interval * 60))
    else:
        log.warning("Invalid tracking interval requested " \
                    "({} minutes not in range 0..1440)"
                    .format(interval_minutes))
        # TODO: send an error response indicating 'invalid interval' over the air


def send_idp_location():
    """
    Prepares a specific binary-optimized location report
    using SIN=255, MIN=255.

    :param loc: a Location object
    :return: Boolean success
    """
    global log
    global modem
    global snr
    log.info('Getting location to send')
    try:
        loc = modem.location_get()
        if loc is None:
            log.warning('Location not returned')
            return
        # TODO if location is not valid send invalid?
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
        payload.add_field('speed', 'uint_16', spd_kph, bits=8)
        payload.add_field('heading', 'uint_16', hdg, bits=9)
        payload.add_field('satellites', 'uint_8', loc.satellites, bits=4)
        payload.add_field('fixtype', 'uint_8', loc.fix_type, bits=2)
        payload.add_field('snr', 'uint_16', int(snr * 10), bits=9)
        # Get binary string payload to send
        data_str = payload.encode_idp(data_format=data_format)
        # Create message wrapper with SIN/MIN
        '''
        message = MobileOriginatedMessage(payload=data_str, 
                                        data_format=data_format, 
                                        msg_sin=msg_sin, 
                                        msg_min=msg_min)
        '''
        message_id = modem.message_mo_send(data=data_str,
                                        data_format=data_format,
                                        sin=msg_sin,
                                        min=msg_min)
        if message_id.startswith('ERR'):
            log.error('Failed to submit location message: {}'.format(message_id))
        else:
            log.info('Location message submitted with ID {}'.format(message_id))

    except IdpModemBusy:
        log.warning('Timed out modem busy')


def complete_mo_messages():
    global log
    global modem
    log.debug('Checking return message status')
    try:
        message_states = modem.message_mo_state()
        if message_states is not None:
            for status in message_states:
                log_message = 'Mobile-originated message {} {}'.format(status['name'], status['state'])
                if status['state'] == 'TX_COMPLETE':
                    log.info(log_message)
                elif status['state'] == 'TX_FAILED':
                    log.warning('{} getting modem statistics'.format(log_message))
                    get_stats()
                else:
                    log.debug(log_message)
        else:
            log.warning('Get message states returned None')
    except Exception as e:
        log.error(e)


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
    parser.add_argument('-q', dest='quit_timeout', type=int, default=60,
                        help="Timeout seconds with no modem connection to quit")
    return vars(parser.parse_args(args=argv[1:]))


def main():
    global modem
    global log
    global tracking_interval
    global tracking_thread
    global network_state

    user_options = parse_args(sys.argv)
    port = user_options['port']
    interval = user_options['interval']
    logfilename = user_options['logfile']
    logsize = user_options['log_size']
    tracking_interval = int(user_options['tracking'])
    debug = user_options['debug']
    quit_timeout = user_options['quit_timeout']
    blockage_timeout = 15 * 60

    modem = None
    stats_monitor = None
    log = get_wrapping_logger(name='st2100_beta_log',
                              filename=logfilename,
                              file_size=logsize,
                              debug=debug)
    log.info('{}Starting ST2100 Beta{}'.format('*' * 15, '*' * 15))
    at_threads = []
    try:
        connected = False
        (modem, t) = get_modem_thread(port)
        start_time = time()
        while not connected:
            if time() - start_time > quit_timeout:
                raise Exception('Timed out trying to connect to modem')
            try:
                connected = modem.config_restore_nvm()
                log.debug('Connected to modem')
            except AtCrcConfigError:
                log.warning('CRC detected retrying connect to IDP modem')
                modem.crc = True
                connected = modem.config_restore_nvm()
            except AtTimeout:
                log.warning('Timeout connecting to IDP modem, retrying in 6 seconds')
                sleep(6)
        messages_cleared = modem.message_mo_clear()
        log.debug('Cleared {} from modem transmit queue'.format(messages_cleared))
        #: Initially check status every 5 seconds until registered
        stats_monitor = RepeatingTimer(interval, name='beta_stats', defer=False, 
                                    callback=get_stats, auto_start=True)
        at_threads.append(stats_monitor)
        # TODO: wait for registration before starting messaging threads
        while network_state != 10:
            log.debug('Getting network state')
            get_status()
            if time() > start_time + blockage_timeout:
                raise Exception('Timed out due to blockage')
            sleep(5)
        tracking_thread = RepeatingTimer(int(tracking_interval * 60), 
                                    name='tracking', callback=send_idp_location,
                                    defer=False, auto_start=True)
        at_threads.append(tracking_thread)
        mo_cleanup = RepeatingTimer(5, name='mo_message_cleanup', defer=False,
                                    callback=complete_mo_messages, auto_start=True)
        at_threads.append(mo_cleanup)
        mt_commands = RepeatingTimer(5, name='mt_message_check', defer=False,
                                    callback=handle_mt_messages, auto_start=True)
        at_threads.append(mt_commands)
        while True:
            pass
    
    except KeyboardInterrupt:
        print('Interrupted by user')
    
    except Exception as e:
        traceback.print_exc()
        print(e)
    
    finally:
        # stats_monitor.join()
        if modem is not None:
            modem.stop()
            t.close()
        for at_thread in at_threads:
            at_thread.terminate()
        sys.exit()


if __name__ == '__main__':
    main()
