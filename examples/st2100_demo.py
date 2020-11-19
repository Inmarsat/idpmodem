#!/usr/bin/env python3
#coding: utf-8
"""
Periodically queries modem status and writes to a log file

"""
from __future__ import absolute_import

from argparse import ArgumentParser
from os.path import abspath, dirname, join, realpath
import sys
from time import sleep, time

'''
THIS_DIR = dirname(__file__)
MODULE_DIR = abspath(join(THIS_DIR, '..', 'idpmodem'))
sys.path.append(MODULE_DIR)
'''
from idpmodem.atcommand_thread import get_modem_thread, IdpModemBusy, AtException, AtCrcConfigError, AtCrcError, AtTimeout
from idpmodem.message import MobileOriginatedMessage, MobileTerminatedMessage
from idpmodem.codecs import common as idpcodec
from idpmodem.constants import FORMAT_B64, FORMAT_HEX
from idpmodem.utils import get_wrapping_logger, RepeatingTimer


__version__ = "2.0.2"


# TODO: more elegant way of managing than global variables
modem = None
timeout_count = 0
tracking_interval = 900
tracking_thread = None
log = None
snr = 0.0
network_state = None

STATS_LIST = [
    ("systemStats", "2,3"),
    ("satcomStats", "2,4"),
    ("rxMetricsHour", "3,18"),
    ("txMetricsHour", "3,22"),
    ("gnssFixStats", "4,1"),
    #: ("lowPowerStats", "2,2"),
    ("status", "3,1"),
]


def handle_serial_timeout(e):
    global timeout_count
    global log
    timeout_count += 1
    log.warning('{} (total = {})'.format(e, timeout_count))


def handle_modem_busy(e):
    global log
    log.warning('Modem busy: {}'.format(e))


def command(at_command, timeout=None):
    global log
    global modem
    try:
        response = modem.command(at_command, timeout)
        return response
    except IdpModemBusy as e:
        handle_modem_busy(e)
    except AtTimeout as e:
        handle_serial_timeout(e)
    return None


def log_stat(stat_label, stat, response):
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
    log.info('{}{}'.format(stat_label, response).replace(' ', '').strip())
    if stat == '3,1':
        status_metrics = response.replace('%EVNT:', '').strip().split(',')
        snr = round(int(status_metrics[23]) / 100, 2)
        network_state = int(status_metrics[29])
        log.debug('C/No={} | State={}'.format(snr, network_state))


def get_network_state():
    """Updates the network state"""
    global log
    global modem
    global network_state
    TIMEOUT = 10
    responses = command('ATS90=3 S91=1 S92=1 S122?', TIMEOUT)
    if responses is not None:
        if 'OK' in responses:
            network_state = int(responses[0])


def get_stats():
    """Requests relevant beta trial statistics."""
    global log
    global modem
    # event_str = ''
    TIMEOUT = 10
    for i in range(len(STATS_LIST)):
        stat_label, stat = STATS_LIST[i]
        log.debug('Getting stat: {}'.format(stat_label))
        response = command('AT%EVNT={}'.format(stat), TIMEOUT)
        if response is not None:
            if 'OK' in response:
                log_stat(stat_label, stat, response[0])
            else:
                if stat_label in ['rxMetricsHour', 'txMetricsHour']:
                    stat += ' (likely no hourly metrics exist yet)'
                log.warning('Error getting stat {}'.format(stat))


def handle_mt_messages():
    """Processes Mobile-Terminated messages with SIN 255."""
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
                        except IdpModemBusy as e:
                            handle_modem_busy(e)
                        except AtTimeout as e:
                            handle_serial_timeout(e)
            else:
                log.debug('No forward messages queued')
    except IdpModemBusy as e:
        handle_modem_busy(e)
    except AtTimeout as e:
        handle_serial_timeout(e)


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
    Sends a binary-optimized location report using SIN=255, MIN=255.
    """
    global log
    global modem
    global snr
    FIX_STALE = 1
    FIX_TIMEOUT = 35
    log.info('Getting location to send ({}s timeout)'.format(FIX_TIMEOUT))
    try:
        loc = modem.location_get(FIX_STALE, FIX_TIMEOUT)
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
        spd_kph = int(loc.speed * 1.852)   #: convert from knots
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
        log.debug('Submitting mobile-originated location message')
        message_id = modem.message_mo_send(data=data_str,
                                        data_format=data_format,
                                        sin=msg_sin,
                                        min=msg_min)
        if message_id.startswith('ERR'):
            log.error('Failed to submit location message: {}'.format(message_id))
        else:
            log.info('Location message submitted with ID {}'.format(message_id))
    except IdpModemBusy as e:
        handle_modem_busy(e)
    except AtTimeout as e:
        handle_serial_timeout(e)


def complete_mo_messages():
    """Checks return message status in the queue to avoid queue filling."""
    global log
    global modem
    log.debug('Checking return message status')
    try:
        message_states = modem.message_mo_state()
        if message_states is not None:
            if len(message_states) == 0:
                log.debug('No return messages queued')
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
    except IdpModemBusy as e:
        handle_modem_busy(e)
    except AtTimeout as e:
        handle_serial_timeout(e)


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = ArgumentParser(description="Interface with an IDP modem.")
    dir_path = dirname(realpath(__file__))
    logfilename = dir_path + '/st2100_beta.log'
    parser.add_argument('-l', '--log', dest='logfile', type=str, default=logfilename,
                        help="the log file name with optional extension (default extension .log)")
    parser.add_argument('-s', '--logsize', dest='log_size', type=int, default=5,
                        help="the maximum log file size, in MB (default 5 MB)")
    parser.add_argument('-i', '--interval', dest='interval', type=int, default=900,
                        help="stats logging interval in seconds (default 900)")
    parser.add_argument('-t', '--tracking', dest='tracking', type=int, default=15,
                        help="tracking interval in minutes (default 15)")
    parser.add_argument('--debug', dest='debug', action='store_true',
                        help="enable verbose debug logging")
    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB0',
                        help="the serial port of the IDP modem")
    parser.add_argument('-q', dest='quit_timeout', type=int, default=60,
                        help="Timeout seconds with no modem connection to quit")
    parser.add_argument('-x', dest='max_timeouts', type=int, default=100,
                        help="Maximum serial timeouts before quit (default 100)")
    return vars(parser.parse_args(args=argv[1:]))


def main():
    global modem
    global log
    global tracking_interval
    global tracking_thread
    global network_state
    global snr
    global timeout_count

    user_options = parse_args(sys.argv)
    port = user_options['port']
    interval = user_options['interval']
    logfilename = user_options['logfile']
    logsize = user_options['log_size']
    tracking_interval = int(user_options['tracking'])
    debug = user_options['debug']
    quit_timeout = user_options['quit_timeout']
    max_timeouts = user_options['max_timeouts']
    blockage_timeout = 15 * 60

    modem = None
    stats_monitor = None
    log = get_wrapping_logger(name='st2100_beta_log',
                              filename=logfilename,
                              file_size=logsize,
                              debug=debug)
    log.info('{}Starting ST2100 Beta{}'.format('*' * 15, '*' * 15))
    log.info('Python App Version {}'.format(__version__))
    network_state = 0
    snr = 0.0
    timeout_count = 0
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
                crc_enabled = modem.crc_enable()
                mobile_id = modem.device_mobile_id()
                versions = modem.device_version()
                log.debug('Connected to modem {} (FW:{})'.format(mobile_id,
                        versions['firmware']))
            except AtCrcConfigError:
                log.warning('CRC detected retrying connect to IDP modem')
                modem.crc = True
                connected = modem.config_restore_nvm()
            except AtTimeout:
                log.warning('Timeout connecting to IDP modem, retrying in 6 seconds')
                sleep(6)
        messages_cleared = modem.message_mo_clear()
        if messages_cleared > 0:
            log.info('Cleared {} message(s) from modem transmit queue'.format(messages_cleared))
        #: Initially check status every 5 seconds until registered
        stats_monitor = RepeatingTimer(interval, name='beta_stats', defer=False, 
                                    target=get_stats, auto_start=True)
        at_threads.append(stats_monitor)
        # TODO: wait for registration before starting messaging threads
        while network_state != 10:
            log.debug('Getting network state')
            get_network_state()
            if time() > start_time + blockage_timeout:
                raise Exception('Timed out due to blockage')
            sleep(5)
        tracking_thread = RepeatingTimer(int(tracking_interval * 60), 
                                    name='tracking', target=send_idp_location,
                                    defer=False, auto_start=True)
        at_threads.append(tracking_thread)
        mo_cleanup = RepeatingTimer(11, name='mo_message_cleanup', defer=False,
                                    target=complete_mo_messages, auto_start=True)
        at_threads.append(mo_cleanup)
        mt_commands = RepeatingTimer(12, name='mt_message_check', defer=False,
                                    target=handle_mt_messages, auto_start=True)
        at_threads.append(mt_commands)
        while timeout_count < max_timeouts:
            pass
    
    except KeyboardInterrupt:
        print('Interrupted by user')
    
    except Exception as e:
        log.exception(e)
    
    finally:
        # stats_monitor.join()
        for at_thread in at_threads:
            at_thread.terminate()
            at_thread.join()
        if modem is not None:
            modem.stop()
            t.close()
        sys.exit()


if __name__ == '__main__':
    main()
