#!/usr/bin/env python
"""
Sample program to run on Raspberry Pi (headless) or Windows (using ORBCOMM/SkyWave Modem Simulator)
or Multitech Conduit with serial mCard (AP1 slot).
Periodically queries modem status, checks for incoming messages and sends location reports

Dependencies:
  - (REQUIRED) crcxmodem.py calculates CRC-16-CCITT xmodem
  - (REQUIRED) idpmodem.py contains object definitions for the modem
  - (optional) RPi.GPIO for running headless on Raspberry Pi
  - (optional) serialportfinder.py is used when running on Windows test environment (detect COM port)

Mobile-Originated location reports are 17 bytes using SIN 255 MIN 255
Mobile-Terminated location interval change uses SIN 255 MIN 1, plus 1 byte payload for the new interval in minutes.
  When a new interval is configured, a location report is generated immediately, thereafter at the new interval.
"""

import time
import datetime
import serial       # PySerial 2.7
import sys
import traceback
import logging
from logging.handlers import RotatingFileHandler
import threading
import binascii
import operator
import argparse
import subprocess
import idpmodem

# GLOBALS
global log                  # the log object used by most functions and classes
global modem                # the class instance of IDP modem object defined in 'idpmodem' module
global tracking_interval    # an interval that can be changed remotely to drive location reporting
global shutdown_flag        # a flag triggered by an interrupt from a parallel service (e.g. RPi GPIO input)


class RepeatingTimer(threading.Thread):
    """ A Thread class that repeats function calls like a Timer but allows:
        start_timer(), stop_timer(), restart_timer(), change_interval(), terminate()
    :param seconds (float) the interval time between callbacks
    :param name of the thread for identification
    :param sleep_chunk the divisor of the interval for intermediate steps/threading
    :param callback the function that will be executed each interval
    :param *args optional argument pointers for the callback function
    """
    # TODO: move this class into an imported module
    global log

    def __init__(self, seconds, name=None, sleep_chunk=0.25, callback=None, *args):
        threading.Thread.__init__(self)
        if name is not None:
            self.name = name
        else:
            self.name = str(callback) + "_timer_thread"
        self.interval = seconds
        if callback is None:
            log.warning("No callback specified for RepeatingTimer " + self.name)
        self.callback = callback
        self.callback_args = args
        self.sleep_chunk = sleep_chunk
        self.terminate_event = threading.Event()
        self.start_event = threading.Event()
        self.reset_event = threading.Event()
        self.count = self.interval / self.sleep_chunk

    def run(self):
        while not self.terminate_event.is_set():
            while self.count > 0 and self.start_event.is_set() and self.interval > 0:
                ''' # comment this line for debug output every second
                if (self.count * self.sleep_chunk - int(self.count * self.sleep_chunk)) == 0.0:
                    print(self.name + "%s countdown: %d (%ds @ step %02f" 
                          % (self.name, self.count, self.interval, self.sleep_chunk))
                # '''
                if self.reset_event.wait(self.sleep_chunk):
                    self.reset_event.clear()
                    self.count = self.interval / self.sleep_chunk
                self.count -= 1
                if self.count <= 0:
                    self.callback(*self.callback_args)
                    self.count = self.interval / self.sleep_chunk

    def start_timer(self):
        self.start_event.set()
        log.info(self.name + " timer started (" + str(self.interval) + " seconds)")

    def stop_timer(self):
        self.start_event.clear()
        self.count = self.interval / self.sleep_chunk
        log.info(self.name + " timer stopped (" + str(self.interval) + " seconds)")

    def restart_timer(self):
        if self.start_event.is_set():
            self.reset_event.set()
        else:
            self.start_event.set()
        log.info(self.name + " timer restarted (" + str(self.interval) + " seconds)")

    def change_interval(self, seconds):
        log.info(self.name + " timer interval changed (" + str(self.interval) + " seconds)")
        self.interval = seconds
        self.restart_timer()

    def terminate(self):
        self.terminate_event.set()
        log.info(self.name + " timer terminated")


class Location(object):
    """ A class containing a specific set of location-based information for a given point in time
        Uses 91/181 if lat/lon are unknown
    """

    def __init__(self, latitude=91*60*1000, longitude=181*60*1000, altitude=0,
                 speed=0, heading=0, timestamp=0, satellites=0, fixtype=1,
                 PDOP=0, HDOP=0, VDOP=0):
        self.latitude = latitude                # 1/1000th minutes
        self.longitude = longitude              # 1/1000th minutes
        self.altitude = altitude                # metres
        self.speed = speed                      # knots
        self.heading = heading                  # degrees
        self.timestamp = timestamp              # seconds since 1/1/1970 unix epoch
        self.satellites = satellites
        self.fixtype = fixtype
        self.PDOP = PDOP
        self.HDOP = HDOP
        self.VDOP = VDOP
        self.lat_dec_deg = latitude / 60000
        self.lon_dec_deg = longitude / 60000
        self.time_readable = datetime.datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


class HMI(object):
    """A Human Machine Interface element that can be used for headless operation"""
    # TODO: develop
    def __init__(self):
        pass

    def indicate_normal_operation(self):
        pass

    def indicate_com_issue(self):
        pass

    def trigger_shutdown(self):
        pass


class ModemGPIO(object):
    """Physical input/output interfaces for the modem e.g. reset, interrupts"""
    # TODO: develop
    def __init__(self):
        pass

    def assert_modem_reset(self):
        pass

    def monitor_reset_out(self):
        pass

    def monitor_notification_pin(self):
        pass

    def monitor_pps(self):
        pass


def check_sat_status():
    """ Checks satellite status using Trace Log Mode to update state and statistics """
    global log
    global modem

    res = modem.at_check_sat_status()
    if res['success']:
        pass

    return


def handle_mt_tracking_command(message, data_type=2):
    """ Expects to get SIN 255 MIN 1 'reconfigure tracking interval, in minutes, in a range from 1-1440 
    :param  message dictionary for Mobile-Terminated message with
                'sin' Service Identifier Number
                'min' Message Identifier Number
                'payload' (including MIN byte)
                'size' bytes including SIN, MIN
    :param  data_type 1 = Text (string), 2 = Hex (bytearray), 3 = base64 (bytearray)
    """
    # TODO: Additional testing
    global log
    global tracking_interval

    tracking_thread = None
    if message['sin'] == 255 and message['min'] == 1 and data_type == 2:
        # Format: <SIN><MIN><tracking_interval> where tracking_interval is a 2-byte value in minutes
        payload = binascii.hexlify(bytearray(message['payload']))
        new_interval_minutes = int(payload[2:], 16)
        for t in threading.enumerate():
            if t.name == 'tracking':
                tracking_thread = t
        if (0 <= new_interval_minutes <= 1440) and ((new_interval_minutes * 60) != tracking_interval):
            log.info("Changing tracking interval to %d seconds" % (new_interval_minutes * 60))
            tracking_interval = new_interval_minutes * 60
            tracking_thread.change_interval(tracking_interval)
            if tracking_interval == 0:
                tracking_thread.stop_timer()
            else:
                get_send_location()
        else:
            log.warning("Invalid tracking interval requested (%d minutes not in range 0..1440)" % new_interval_minutes)
            # TODO: send an error response indicating 'invalid interval' over the air
    else:
        log.warning("Unsupported command SIN=%d MIN=%d" % (message['sin'], message['min']))


def check_mt_messages():
    """ Checks for Mobile-Terminated messages in modem queue, retrieves and handles."""
    global log
    global modem

    new_msgs, messages = modem.at_check_mt_messages()
    if new_msgs:
        for msg in messages:
            if msg['sin'] == 255:
                data_type = 2
            elif msg['sin'] == 128:
                data_type = 1
            else:
                data_type = 3
            msg_retrieved, message = modem.at_get_mt_message(msg_name=msg['name'],
                                                             msg_sin=msg['sin'],
                                                             msg_size=msg['size'],
                                                             data_type=data_type)
            if msg_retrieved:
                if message['sin'] == 255:
                    # TODO: clean up to separate MIN byte from rest of payload for processing - impacts handle_mt_...
                    handle_mt_tracking_command(message, data_type=data_type)
                else:
                    log.info("Message SIN=%d MIN=%d not handled", message['sin'], message['min'])

    return


def _send_location(loc):
    """ Prepares a specific binary-optimized location report using SIN=255, MIN=255
    :param  loc: a Location object
    :return Boolean success
    """
    global modem
    data_fields = [
        (loc.timestamp, '031b'),
        (loc.latitude, '024b'),
        (loc.longitude, '025b'),
        (loc.altitude, '09b'),
        (loc.speed, '08b'),
        (loc.heading, '09b'),
        (loc.satellites, '04b'),
        (loc.fixtype, '02b'),
    ]
    bin_str = ''
    for field in data_fields:
        if field[0] < 0:
            inv_bin_field = format(-field[0], field[1])
            comp_bin_field = ''
            i = 0
            while len(comp_bin_field) < len(inv_bin_field):
                if inv_bin_field[i] == '0':
                    comp_bin_field += '1'
                else:
                    comp_bin_field += '0'
                i += 1
            bin_field = format(int(comp_bin_field, 2) + 1, field[1])
        else:
            bin_field = format(field[0], field[1])
        bin_str += bin_field
    pad_bits = len(bin_str) % 8
    while pad_bits > 0:
        bin_str += '0'
        pad_bits -= 1
    hex_str = ''
    index_byte = 0
    while len(hex_str)/2 < len(bin_str)/8:
        hex_str += format(int(bin_str[index_byte:index_byte+8], 2), '02X').upper()
        index_byte += 8
    success = modem.at_send_message(data_string=hex_str, data_format=2, msg_sin=255, msg_min=255)
    if not success:
        log.error("Failed to send location message")

    return success


def validate_nmea_checksum(sentence):
    """ Validates NMEA checksum according to the standard
    :param sentence: NMEA sentence including checksum
    :return: boolean result (checksum correct)
             raw NMEA data string, with prefix $Gx and checksum suffix removed
    """

    sentence = sentence.strip('\n').strip('\r')
    nmeadata, cksum = sentence.split('*', 1)
    nmeadata = nmeadata.replace('$', '')
    xcksum = str("%0.2x" % (reduce(operator.xor, (ord(c) for c in nmeadata), 0))).upper()
    return (cksum == xcksum), nmeadata[2:]


def parse_nmea_to_location(sentence, loc):
    """ parses NMEA string(s) to populate a Location object
    Several sentence parameters are unused but remain as placeholders for completeness/future use
    :param sentence: NMEA sentence (including prefix and suffix)
    :param loc: the Location object to be populated
    :return: Boolean success of operation
             error string if not successful
    """

    err_str = ''
    res, NMEA_data = validate_nmea_checksum(sentence)
    if res:
        sentence_type = NMEA_data[0:3]
        if sentence_type == 'GGA':
            GGA = NMEA_data.split(',')
            GGAutc_hhmmss = GGA[1]
            GGAlatitude_dms = GGA[2]
            GGAns = GGA[3]
            GGAlongitude_dms = GGA[4]
            GGAew = GGA[5]
            GGAqual = GGA[6]
            GGAFixQualities = [
                'invalid',
                'GPS fix',
                'DGPS fix',
                'PPS fix',
                'RTK',
                'Float RTK',
                'Estimated',
                'Manual',
                'Simulation'
            ]
            GGAsatellites = GGA[7]
            GGAhdop = GGA[8]
            GGAaltitude = GGA[9]
            GGAheightWGS84 = GGA[11]
            loc.satellites = int(GGAsatellites)
            if loc.satellites > 3:
                loc.fixtype = 3
            elif int(GGAqual) > 0:
                loc.fixtype = 2
            loc.altitude = int(float(GGAaltitude))
            loc.HDOP = max(int(float(GGAhdop)), 32)

        elif sentence_type == 'RMC':
            RMC = NMEA_data.split(',')
            RMCutc_hhmmss = RMC[1]
            # RMCactive = RMC[2]
            RMClatitude_dms = RMC[3]        # 4807.038 = 48 deg 07.038'
            RMCns = RMC[4]
            RMClongitude_dms = RMC[5]       # 01131.000 = 11 deg 31.000'
            RMCew = RMC[6]
            RMCspeed_kn = RMC[7]            # 022.4 = 22.4 knots
            RMCheading_deg = RMC[8]         # 084.4 = 84.4 degrees True
            RMCdate_ddmmyy = RMC[9]
            # RMCmvmag = RMC[10]
            # RMCmvdir = RMC[11]
            year = int(RMCdate_ddmmyy[4:6]) + 2000
            month = int(RMCdate_ddmmyy[2:4])
            day = int(RMCdate_ddmmyy[0:2])
            hour = int(RMCutc_hhmmss[0:2])
            minute = int(RMCutc_hhmmss[2:4])
            second = int(RMCutc_hhmmss[4:6])
            dt = datetime.datetime(year, month, day, hour, minute, second)
            loc.timestamp = int(time.mktime(dt.timetuple()))
            loc.latitude = int((float(RMClatitude_dms[0:2]) + float(RMClatitude_dms[2:]) / 60) * 60 * 1000)
            if RMCns == 'S': loc.latitude *= -1
            loc.longitude = int((float(RMClongitude_dms[0:3]) + float(RMClongitude_dms[3:]) / 60) * 60 * 1000)
            if RMCew == 'W': loc.longitude *= -1
            loc.speed = int(float(RMCspeed_kn))
            loc.heading = int(float(RMCheading_deg))
            # Update human-readable attributes
            loc.lat_dec_deg = round(float(loc.latitude) / 60000.0, 6)
            loc.lon_dec_deg = round(float(loc.longitude) / 60000.0, 6)
            loc.time_readable = datetime.datetime.utcfromtimestamp(loc.timestamp).strftime('%Y-%m-%d %H:%M:%S')

        elif sentence_type == 'GSA':
            GSA = NMEA_data.split(',')
            # GSAauto = GSA[1]
            GSAfixtype = GSA[2]
            # GSAfixtypes = {'none':1,'2D':2,'3D':3}
            prn = 1
            idx = 3
            GSAprns = ''
            while prn <= 12:
                GSAprns += GSA[idx]
                if prn < 12: GSAprns += ','
                prn += 1
                idx += 1
            GSApdop = GSA[15]
            GSAhdop = GSA[16]
            GSAvdop = GSA[17]
            loc.fixtype = int(GSAfixtype)
            loc.PDOP = max(int(float(GSApdop)), 32)     # values above 20 are bad; cap at 5-bit representation
            # loc.HDOP = max(int(float(GSAhdop)), 32)
            loc.VDOP = max(int(float(GSAvdop)), 32)

        elif sentence_type == 'GSV':
            GSV = sentence.split(',')
            # GSVsentences = GSV[1]
            # GSVsentence = GSV[2]
            GSVsatellites = GSV[3]
            # GSVprn1 = GSV[4]
            # GSVel1 = GSV[5]
            # GSVaz1 = GSV[6]
            # GSVsnr1 = GSV[7]
            # up to 4 satellites total per sentence, each as above in successive indices
            # loc.satellites = int(GSVsatellites)

        else:
            err_str = "NMEA sentence type not recognized"
    else:
        err_str = "Invalid NMEA checksum"

    return err_str == '', err_str


def get_send_location():
    """ Queries GPS NMEA strings from the modem and submits to a send/processing routine. """
    global log
    global modem
    # global thread_lock
    global tracking_interval

    MAX_ATTEMPTS = 3

    loc = Location()
    log.debug("Requesting location to send")
    retrieved = False
    sentences = []
    attempts = 0
    while not retrieved and attempts < MAX_ATTEMPTS:
        retrieved, sentences = modem.at_get_nmea(refresh=tracking_interval)
        attempts += 1
        time.sleep(3)
    if retrieved:
        for s in sentences:
            parsed, parse_err = parse_nmea_to_location(s, loc)
            if not parsed:
                log.warning("NMEA sentence parsing failed (%s)" % parse_err)
        _send_location(loc)
        if tracking_interval > 0:
            log.debug("Next location report in ~" + str(tracking_interval) + " seconds.")
    else:
        log.warning("Timed out %d attempts to query GNSS" % MAX_ATTEMPTS)
    return


def init_log(logfile=None, file_size=5, debug=False):
    """ Initializes logging to file and console
    :param  logfile the name of the file
    :param  file_size the max size of the file in megabytes, before wrapping occurs
    :param  debug enables verbose logging
    :return log object
    """
    # TODO: move into imported module
    if debug:
        log_lvl = logging.DEBUG
    else:
        log_lvl = logging.INFO
    log_formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d,(%(threadName)-10s),' \
                                          '[%(levelname)s],%(funcName)s(%(lineno)d),%(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
    if logfile is not None:
        log_object = logging.getLogger(logfile)
        log_handler = RotatingFileHandler(logfile, mode='a', maxBytes=file_size * 1024 * 1024,
                                          backupCount=2, encoding=None, delay=0)
        log_handler.setFormatter(log_formatter)
        log_object.addHandler(log_handler)
    else:
        log_object = logging.getLogger("temp_log")
    log_object.setLevel(log_lvl)
    console = logging.StreamHandler()
    console.setFormatter(log_formatter)
    console.setLevel(log_lvl)
    log_object.addHandler(console)
    return log_object


def modem_attach(max_attempts=0, hmi_indicator=None):
    """ Initializes communications with the modem. Allows for HMI indicator use.
    :param  max_attempts - the maximum number of tries sending a basic AT command (0 = infinite)
    :param  hmi_indicator - an optional object to provide headless notification (LED flasher)
    :return Boolean success
    """
    # TODO: add HMI capability
    global log
    global modem

    log.info("Attempting to establish modem communications")
    success = False
    if hmi_indicator is not None:
        # hmi_indicator.indicate_com_issue()
        pass
    attempts = 0
    while not success:
        success = modem.at_attach()
        attempts += 1
        if attempts == max_attempts:
            break
    if success:
        modem.is_connected = True
        if hmi_indicator is not None:
            # hmi_indicator.indicate_normal_operation()
            pass
        modem.at_initialize_modem()
    return success


def monitor_com(disconnect_timeouts=3):
    """ TODO: docs"""
    global log
    global modem

    if modem.at_timeouts >= disconnect_timeouts and modem.is_connected:
        modem.is_connected = False
        log.warning("AT responses timed out %d times - attempting to reconnect" % modem.at_timeouts)

    return modem.is_connected


def init_environment(default_logfile=None, debug=False):
    """Initializes the OS environment
    :param  default_logfile name to use
    :param  debug value passed in from execution options
    :returns    Boolean success
                Dictionary:
                'serial_name' e.g. 'COM1'
                'logfile' e.g. 'logfile.log'
                'tracking' interval in seconds
                'debug' value (may be overridden by Windows GUI)
    """
    success = False
    serial_name = None
    logfile = None
    tracking = None
    if sys.platform.lower().startswith('linux2'):
        try:
            import RPi.GPIO as GPIO  # Successful import of this module implies running on Raspberry Pi
            success = True
            print("\n ** Raspberry Pi / GPIO environment detected")
            logfile = '/home/pi/' + default_logfile
            serial_name = '/dev/ttyUSB0'  # TODO: validate RPi USB/serial port assignment
        except ImportError:
            print("\n ** Linux environment detected (assuming MultiTech Conduit AEP)")
            # TODO: more robust check for MTS
            success = True
            subprocess.call('mts-io-sysfs store ap1/serial-mode rs232', shell=True)
            logfile = '/home/root/' + default_logfile
            serial_name = '/dev/ttyAP1'     # TODO: validate serial port assignment
    elif sys.platform.lower().startswith('win32'):
        try:
            import idpwindows
            success = True
            print("\n ** Windows environment detected")
            res = idpwindows.initialize()
            serial_name = res['serial']
            debug = res['debug']
            if res['logfile'] != '':
                logfile = res['logfile']
            tracking = res['tracking']
        except ImportError:
            print("\n Could not import idpwindows.py test utility")
    else:
        print('\n Operation undefined on current platform. Please use RPi/GPIO, MultiTech AEP or Windows.')

    return success, {'serial_name': serial_name, 'logfile': logfile, 'tracking': tracking, 'debug': debug}


def parse_args(argv):
    """Parse the command line arguments
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

    parser.add_argument('-t', '--track', dest='tracking', type=int, default=0,
                        help="location reporting interval in minutes (0..1440, default = 15, 0 = disabled)")

    parser.add_argument('-f', '--fishdish', dest='fish_dish', action='store_true',
                        help="use Fish Dish for headless operation indicators")

    return vars(parser.parse_args(args=argv[1:]))


def main():

    global log
    global modem
    global tracking_interval
    global shutdown_flag

    shutdown_flag = False

    ser = None
    SERIAL_BAUD = 9600

    modem = None

    # Timer intervals (seconds)
    SAT_STATUS_INTERVAL = 5
    MT_MESSAGE_CHECK_INTERVAL = 15
    tracking_interval = 900

    # Thread lock for background processes to avoid overlapping AT requests
    thread_lock = threading.RLock()     # TODO: is there a need to pass this into the modem instance?
    threads = []

    # Derive run options from command line
    user_options = parse_args(sys.argv)
    if '.' not in user_options['logfile']:
        logfile = user_options['logfile'] + '.log'
    else:
        logfile = user_options['logfile']
    log_size = user_options['log_size']
    debug = user_options['debug']
    if user_options['tracking'] is not None:
        if 0 <= user_options['tracking'] <= 1440:
            tracking_interval = int(user_options['tracking'] * 60)
        else:
            sys.exit("Invalid tracking interval, must be in range 0..1440")

    # Pre-initialization of platform
    env, res = init_environment(default_logfile=logfile, debug=debug)
    if not env:
        sys.exit('Unable to initialize environment.')
    else:
        serial_name = res['serial_name']
        logfile = res['logfile']
        tracking_interval = res['tracking']
        debug = res['debug']

    # Set up log file
    log = init_log(logfile, log_size, debug=debug)
    sys.stdout.flush()

    log.debug("**** PROGRAM STARTING ****")

    ever_connected = False
    start_time = str(datetime.datetime.utcnow())
    try:
        # TODO: handle serial exception for writeTimeout vs. write_timeout
        ser = serial.Serial(port=serial_name, baudrate=SERIAL_BAUD,
                            timeout=None, writeTimeout=0,
                            xonxoff=False, rtscts=False, dsrdtr=False)

        if ser.isOpen():

            log.info("Connected to serial port " + ser.name + " at " + str(ser.baudrate) + " baud")
            ser.flushInput()
            ser.flushOutput()
            ser.flush()

            modem = idpmodem.Modem(ser, log)

            # (Proxy) Timer threads for background tasks
            status_thread = RepeatingTimer(seconds=SAT_STATUS_INTERVAL, name='check_sat_status',
                                           callback=check_sat_status)
            threads.append(status_thread.name)
            status_thread.start()

            mt_polling_thread = RepeatingTimer(seconds=MT_MESSAGE_CHECK_INTERVAL, name='check_mt_messages',
                                               callback=check_mt_messages)
            threads.append(mt_polling_thread.name)
            mt_polling_thread.start()

            tracking_thread = RepeatingTimer(seconds=tracking_interval, name='tracking',
                                             callback=get_send_location)
            threads.append(tracking_thread.name)
            tracking_thread.start()

            while True and not shutdown_flag:
                if not modem.is_connected:
                    connected = modem_attach(max_attempts=10)
                    if connected:
                        if not ever_connected:
                            ever_connected = True
                        modem_initialized = modem.at_initialize_modem(use_crc=user_options['use_crc'])
                        if not modem_initialized:
                            log.error("Unable to initialize modem - exiting")
                            break
                        modem.at_check_sat_status()
                        if tracking_interval > 0:
                            get_send_location()
                        for t in threading.enumerate():
                            if t.name in threads:
                                log.debug("Starting task: %s" % t.name)
                                t.start_timer()
                    else:
                        log.error("Unable to establish modem communications - exiting")
                        break
                else:
                    connected = monitor_com(disconnect_timeouts=3)
                    if not connected:
                        for t in threading.enumerate():
                            if t.name in threads:
                                log.debug("Stopping task: %s" % t.name)
                                t.stop_timer()
                time.sleep(1)

        else:
            log.error("Could not establish serial communications on %s" % serial_name)

    except KeyboardInterrupt:
        log.info("Execution stopped by keyboard interrupt.")

    except Exception:
        err_str = "Exception in user code:" + '-' * 40 + '\n' + traceback.format_exc()
        # err_str = "Error on line {}:".format(sys.exc_info()[-1].tb_lineno) + ',' + str(type(e)) + ',' + str(e)
        log.error(err_str)
        raise

    finally:
        end_time = str(datetime.datetime.utcnow())
        if ever_connected and modem is not None:
            log.info("*** Statistics from %s to %s ***" % (start_time, end_time))
            modem.log_statistics()
        for t in threading.enumerate():
            if t.name in threads:
                t.stop_timer()
                t.terminate()
                t.join()
        if ser is not None and ser.isOpen():
            ser.close()
            log.info("Closing serial port %s" % serial_name)
        log.debug("\n\n*** END PROGRAM ***\n\n")


if __name__ == "__main__":
    main()
