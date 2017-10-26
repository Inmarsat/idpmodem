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
  When a new interval is configured, a location report is generated immediately, thereafter at the
  new interval.
"""

import time
import datetime
import serial       # PySerial 2.7
import sys
import traceback
import logging
from logging.handlers import RotatingFileHandler
import threading
import crcxmodem
import binascii
import operator
import argparse
import subprocess
import idpmodem

# GLOBALS
global _debug   # setting used for verbose console messages
global log      # the log object used by most functions and classes
global ser      # the serial port handle for AT communications
global modem    # the data structure for IDP modem operating parameters and statistics defined in 'idpmodem' module
global thread_lock   # a lock to ensure that parallel threads do not overlap AT request/response operations
global tracking_interval    # an interval that can be manipulated by several functions
global _shutdown    # a flag triggered by an interrupt from a parallel GPIO service on Raspberry Pi
global _at_timeout_count    # how many times successive AT commands have timed out
global AT_MAX_TIMEOUTS  # the maximum value of _at_timeout_count before triggering re-initialization


class RepeatingTimer(threading.Thread):
    """ A Thread class that repeats function calls like a Timer but allows:
        start_timer(), stop_timer(), restart_timer(), change_interval(), terminate()
    :param seconds (float) the interval time between callbacks
    :param name of the thread for identification
    :param sleep_chunk the divisor of the interval for intermediate steps/threading
    :param callback the function that will be executed each interval
    :param *args optional argument pointers for the callback function
    """
    global _debug
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
                # debug output every second
                if _debug and (self.count * self.sleep_chunk - int(self.count * self.sleep_chunk)) == 0.0:
                    print(self.name + " countdown: " + str(self.count) +
                          "(" + str(self.interval) + "s @ step " + str(self.sleep_chunk) + "s)")
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


class RpiShutdownException(Exception):
    """ GPIO input asserted on Raspberry Pi requesting shutdown """
    def __init__(self, code):
        self.code = code

    def __str__(self):
        return repr(self.code)


class RpiFishDish:
    """ Defines (BCM) pin mapping for the Fish Dish as a headless indicator on Raspberry Pi
    :param GPIO a valid RPi.GPIO import, with mode set to BCM
    """
    global _shutdown
    global log

    def __init__(self, GPIO):
        self.GPIO = GPIO
        self.LED_ON = GPIO.HIGH
        self.LED_OFF = GPIO.LOW
        self.BUZZ_ON = GPIO.HIGH
        self.BUZZ_OFF = GPIO.LOW
        self.LED_GRN = 4
        self.LED_YEL = 22
        self.LED_RED = 9
        self.BUZZ = 8
        self.BUTTON = 7
        self.leds = [self.LED_GRN, self.LED_YEL, self.LED_RED]
        self.led_states = {
            'green': False,
            'yellow': False,
            'red': False
        }
        self.GPIO.setup(self.leds, GPIO.OUT, initial=self.LED_OFF)
        self.GPIO.setup(self.BUZZ, GPIO.OUT, initial=self.BUZZ_OFF)
        self.GPIO.setup(self.BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        self.GPIO.add_event_detect(self.BUTTON, GPIO.RISING, callback=self.shutdown)
        self.led_flasher = RepeatingTimer(seconds=0.5, name='fish_dish_flasher',
                                          callback=self.led_toggle)

    def led_on(self, color='green'):
        if _debug:
            print("Switch ON %s LED", color)
        if color == 'green':
            self.GPIO.output(self.LED_GRN, self.LED_ON)
        elif color == 'yellow':
            self.GPIO.output(self.LED_YEL, self.LED_ON)
        elif color == 'red':
            self.GPIO.output(self.LED_GRN, self.LED_ON)
        self.led_states[color] = True

    def led_off(self, color='green'):
        if _debug:
            print("Switch OFF %s LED", color)
        if color == 'green':
            self.GPIO.output(self.LED_GRN, self.LED_OFF)
        elif color == 'yellow':
            self.GPIO.output(self.LED_YEL, self.LED_OFF)
        elif color == 'red':
            self.GPIO.output(self.LED_GRN, self.LED_OFF)
        self.led_states[color] = False

    def led_toggle(self, color='green'):
        new_state = not self.led_states[color]
        if _debug:
            print("Toggling %s LED (%s)", color, "ON" if new_state else "OFF")
        if new_state:
            led_assert = self.LED_ON
        else:
            led_assert = self.LED_OFF
        if color == 'green':
            self.GPIO.output(self.LED_GRN, led_assert)
        elif color == 'yellow':
            self.GPIO.output(self.LED_YEL, led_assert)
        elif color == 'red':
            self.GPIO.output(self.LED_GRN, led_assert)
        self.led_states[color] = new_state

    def shutdown(self, channel):
        global _shutdown

        debounce = 0
        while self.GPIO.input(self.BUTTON) == self.GPIO.HIGH and debounce < 3:
            time.sleep(1)
            debounce += 1
        if debounce >= 3:
            log.warning("Shutdown request from Raspberry Pi GPIO on input %d", self.BUTTON)
            _shutdown = True
            raise RpiShutdownException(self.BUTTON)


class RpiModemIO:
    """Defines (BCM) pin mapping for modem reset and notification functions
    :param GPIO a valid RPi.GPIO import, with mode set to BCM
    """

    def __init__(self, GPIO):
        # TODO: Other GPIO connected to modem for advanced use cases
        self.GPIO = GPIO
        self.IDP_RESET_OUT = 5         # Assumed to connect to a relay (NC) hard reboot for modem power supply
        self.IDP_RESET_ASSERT = GPIO.LOW
        self.IDP_RESET_CLEAR = GPIO.HIGH
        self.GPIO.setup(self.IDP_RESET_OUT, GPIO.OUT, initial=self.IDP_RESET_CLEAR)
        self.IDP_NOTIFY_IN = 6
        self.IDP_NOTIFICATION = GPIO.HIGH
        self.GPIO.setup(self.IDP_NOTIFY_IN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        self.GPIO.add_event_detect(self.IDP_NOTIFY_IN, GPIO.RISING, callback=self.idp_notification)

    def idp_notification(self, channel):
        # TODO: handle modem notifications from RPi.GPIO
        pass

    def idp_reset(self):
        # TODO: enable modem reset by asserting RPi.GPIO output
        pass


def get_crc(at_cmd):
    """ Returns the CRC-16-CCITT (initial value 0xFFFF) checksum
    :param at_cmd the AT command to calculate CRC on
    :return the CRC for the command
    """

    return '{:04X}'.format(crcxmodem.crc(at_cmd, 0xffff))


def clean_at(at_line, restore_cr_lf=False):
    """ OBSOLETE Removes debug tags used for visualizing <cr> and <lf> characters
    :param at_line: the AT command/response with debug characters included
    :param restore_cr_lf: an option to restore <cr> and <lf>
    :return: the cleaned AT command without debug tags
    """

    if restore_cr_lf:
        return at_line.replace('<cr>', '\r').replace('<lf>', '\n')
    else:
        return at_line.replace('<cr>', '').replace('<lf>', '')


def update_stats_at_response(at_send_time, at_cmd):
    """ Updates the last and average AT command response time statistics
    :param at_send_time the reference time the AT command was sent
    :param at_cmd the command that was sent
    """
    global _debug
    global modem

    at_response_time_ms = int((time.time() - at_send_time) * 1000)
    modem.systemStats['lastATResponseTime_ms'] = at_response_time_ms
    if _debug:
        log.debug("Response time for " + at_cmd + ": " + str(at_response_time_ms) + " [ms]")
    if modem.systemStats['avgATResponseTime_ms'] == 0:
        modem.systemStats['avgATResponseTime_ms'] = at_response_time_ms
    else:
        modem.systemStats['avgATResponseTime_ms'] = \
            int((modem.systemStats['avgATResponseTime_ms'] + at_response_time_ms) / 2)


def at_get_response(at_cmd, at_timeout=10):
    """ Takes a single AT command, applies CRC if enabled, sends to the modem and waits for response completion
      Parses the response, line by line, until a result code is received or at_timeout is exceeded
      Assumes Quiet mode is disabled, and will not pass 'Quiet enable' (ATQ1) to the modem
      Sets modem object properties (Echo, CRC, Verbose, Quiet) by inference from AT response
    :param  at_cmd       the AT command to send
    :param  at_timeout   the time in seconds to wait for a response
    :return a dictionary containing:
            echo        - the AT command sent (including CRC if applied) or empty string if Echo disabled
            response    - a list of (stripped) strings representing multi-line response
            result      - a string returned after the response when Quiet mode is disabled
                        'OK' or 'ERROR' if Verbose is enabled on the modem, 
                        or a numeric error code that can be looked up in modem.atErrorResultCodes
            checksum    - the CRC (if enabled) or None
            error       - Boolean if CRC is correct
            timeout     - Boolean if AT response timed out
    """
    global _debug
    global log
    global ser
    global modem
    global _at_timeout_count

    CHAR_WAIT = 0.05    # time to wait, in seconds, between serial characters

    at_echo = ''
    at_response = []     # container for multi-line response
    at_result_code = ''
    at_res_crc = ''
    timed_out = False

    # Rejection cases.  TODO: improve error handling
    if ";" in at_cmd:
        log.warning("Multiple AT commands not supported: " + at_cmd)
        return {'echo': at_echo, 'response': at_response, 'result': at_result_code}
    if 'ATQ1' in at_cmd:
        log.warning(at_cmd + " command rejected - quiet mode unsupported")
        return {'echo': at_echo, 'response': at_response, 'result': at_result_code}

    # Serial garbage collection
    orphan_response = ''
    while ser.inWaiting() > 0:
        r_char = ser.read(1)
        if _debug:
            if r_char == '\r':
                r_char = '<cr>'
            elif r_char == '\n':
                r_char = '<lf>'
        orphan_response += r_char
    if orphan_response != '':
        log.warning("Orphaned response: " + orphan_response.replace('\r', '<cr>').replace('\n', '<lf>'))
        # TODO: consider passing back orphaned response for additional handling

    ser.flushInput()
    ser.flushOutput()

    if modem.atConfig['CRC']:
        to_send = at_cmd + '*' + get_crc(at_cmd)
    else:
        to_send = at_cmd
    if "AT%CRC=1" in at_cmd.upper():
        modem.atConfig['CRC'] = True
        if _debug: print("CRC enabled for next command")
    elif "AT%CRC=0" in at_cmd.upper():
        modem.atConfig['CRC'] = False
        if _debug: print("CRC disabled for next command")

    log.debug("Sending:%s with timeout %d seconds", to_send, at_timeout)
    ser.write(to_send + '\r')
    at_send_time = time.time()

    res_line = ''        # each line of response
    raw_res_line = ''    # used for verbose debug purposes only
    at_rx_start = False
    at_rx_complete = False
    at_tick = 0
    while not at_rx_complete:
        time.sleep(CHAR_WAIT)
        while ser.inWaiting() > 0:
            if not at_rx_start:
                at_rx_start = True
            r_char = ser.read(1)
            if r_char == '\r':
                # cases <echo><cr>
                # or <cr>...
                # or <numeric code><cr> (verbose off, no crc)
                res_line += r_char  # <cr> might be followed by <lf>
                raw_res_line += '<cr>'
                if at_cmd in res_line:
                    # case <echo><cr>
                    if at_cmd.upper() == 'ATE0':
                        modem.atConfig['Echo'] = False
                        log.debug("ATE0 (echo disable) requested. Takes effect for next AT command.")
                    else:
                        modem.atConfig['Echo'] = True
                    at_echo = res_line.strip()  # copy the echo into a function return
                    # <echo><cr> will be not be followed by <lf>
                    # can be followed by <text><cr><lf>
                    # or <cr><lf><text><cr><lf>
                    # or <numeric code><cr>
                    # or <cr><lf><verbose code><cr><lf>
                    res_line = ''   # clear for next line of parsing
                elif ser.inWaiting() == 0 and res_line.strip() != '':
                    # or <text><cr>...with delay for <lf> between multi-line responses e.g. GNSS?
                    if modem.atConfig['Verbose']:
                        # case <cr><lf><text><cr>...<lf>
                        # or Quiet mode? --unsupported, suppresses result codes
                        log.debug("Assuming delay between <cr> and <lf> of Verbose response...waiting")
                    else:
                        # case <numeric code><cr> since all other alternatives should have <lf> or other pending
                        log.debug("Assuming receipt <numeric code><cr> with Verbose off: " + res_line.strip())
                        # modem.atConfig['Verbose'] = False
                        at_result_code = res_line   # copy the result code (numeric string) into a function return
                        at_rx_complete = True
                        break
                # else keep parsing next character
            elif r_char == '\n':
                # case <cr><lf>
                # or <text><cr><lf>
                # or <cr><lf><text><cr><lf>
                # or <cr><lf><verbose code><cr><lf>
                # or <*crc><cr><lf>
                res_line += r_char
                raw_res_line += '<lf>'
                if 'OK' in res_line or 'ERROR' in res_line:
                    # <cr><lf><verbose code><cr><lf>
                    at_result_code = res_line   # copy the verbose result (OK/ERROR) into a function return
                    if ser.inWaiting() == 0:    # no checksum pending...response complete
                        at_rx_complete = True
                        break
                    else:
                        res_line = ''   # continue parsing next line
                elif '*' in res_line and len(res_line.strip()) == 5:
                    # <*crc><cr><lf>
                    modem.atConfig['CRC'] = True
                    at_res_crc = res_line.replace('*', '').strip()
                    at_rx_complete = True
                    break
                else:
                    # case <cr><lf>
                    # or <text><cr><lf>
                    # or <cr><lf><text><cr><lf>
                    if res_line.strip() == '':
                        # <cr><lf> empty line...not done parsing yet
                        modem.atConfig['Verbose'] = True
                    else:
                        if res_line.strip() != '':     # don't add empty lines
                            at_response.append(res_line)    # don't include \r\n in function return
                        res_line = ''   # clear for next line parsing
            else:   # a character other than \r or \n
                res_line += r_char
                raw_res_line += r_char

        if at_result_code != '':
            if _at_timeout_count > 0:
                log.info("Valid AT response received - resetting AT timeout count")
                _at_timeout_count = 0
            modem.atConfig['Quiet'] = False
            break

        elif int(time.time()) - at_send_time > at_timeout:
            timed_out = True
            _at_timeout_count += 1
            log.warning("%s command response timed out after %d seconds - %d total AT timeouts",
                        to_send, at_timeout, _at_timeout_count)
            break

        if _debug and int(time.time()) > (at_send_time + at_tick):
            at_tick += 1
            print("Waiting AT response. Tick=" + str(at_tick))

    checksum_ok = False

    if not timed_out:

        if at_res_crc == '':
            modem.atConfig['CRC'] = False
        else:
            modem.atConfig['CRC'] = True
            if len(at_response) == 0 and at_result_code != '':
                str_to_validate = at_result_code
            else:
                str_to_validate = ''
                for res_line in at_response:
                    str_to_validate += res_line
                if at_result_code != '':
                    str_to_validate += at_result_code
            if get_crc(str_to_validate) == at_res_crc:
                checksum_ok = True
            else:
                expected_checksum = get_crc(str_to_validate)
                log.error("Bad checksum received: *" + at_res_crc + " expected: *" + expected_checksum)

        for i, res_line in enumerate(at_response):
            at_response[i] = res_line.strip()
        at_result_code = at_result_code.strip()

        update_stats_at_response(at_send_time, at_cmd)

        log.debug("Raw response: " + raw_res_line)

    return {'echo': at_echo,
            'response': at_response,
            'result': at_result_code,
            'checksum': at_res_crc,
            'error': checksum_ok,
            'timeout': timed_out}


def at_get_result_code(result_code):
    """ Queries the details of an error response on the AT command interface
    :param result_code: the value returned by the AT command response
    :returns: error_code - the specific error code
             error_desc - the interpretation of the error code
    """
    global modem
    global thread_lock

    error_code = -1
    error_desc = "UNDEFINED result code: " + result_code
    if 'OK' in result_code or result_code == '0':
        error_code = 0
    elif 'ERROR' in result_code or result_code == '':
        with thread_lock:
            response = at_get_response('ATS80?')
            err_code2, err_desc2 = at_get_result_code(response['result'])
            if err_code2 == 0:
                error_code = int(response['response'][0])
            else:
                log.error("Error querying last error code from S80: " + err_desc2)
    elif int(result_code) > 0:
        error_code = int(result_code)
    if str(error_code) in modem.atErrResultCodes:
        error_desc = modem.atErrResultCodes[str(error_code)]

    return error_code, error_desc


def at_attach(max_attempts=3):
    """Attempts to attach using basic AT command
    :param  max_attempts to attach
    :returns success    - Boolean result
    """
    global _debug
    global log
    global thread_lock

    AT_TIMEOUT = 1  # second to wait for response

    success = False
    attempt_count = 0
    while attempt_count < max_attempts and not success:
        with thread_lock:
            response = at_get_response('AT', at_timeout=AT_TIMEOUT)
            if response['timeout']:
                log.debug("Failed attempt to establish AT response (" +
                          str(attempt_count + 1) + "/" + str(max_attempts) + ")")
            elif response['result'] != '':
                success = True
                log.info("AT command mode confirmed")
            else:
                log.warning("Unexpected response from AT command")
            attempt_count += 1
        time.sleep(1)
    return success


def at_init_modem(use_crc=False, verbose=True):
    """ Initializes the modem after new connection. Restores saved defaults, disables Quiet mode,

    :param use_crc  - optionally enables CRC on AT commands (e.g. if using long serial cable)
    :param verbose  - optionally use verbose mode for results (OK/ERROR)
    """
    global log
    AT_WAIT = 0.1  # seconds between initialization commands
    
    # Restore saved defaults - modem AT config will also be inferred
    time.sleep(AT_WAIT)
    defaults_restored = False
    restore_attempts = 0
    while not defaults_restored and restore_attempts < 2:
        restore_attempts += 1
        response = at_get_response('ATZ')
        err_code, err_str = at_get_result_code(response['result'])
        if err_code == 100 and modem.atConfig['CRC'] == False:
            modem.atConfig['CRC'] = True
            log.info("ATZ CRC error; retrying with CRC enabled")
        elif err_code != 0:
            err_msg = "Failed to restore saved defaults - exiting (" + err_str + ")"
            log.error(err_msg)
            sys.exit(err_msg)
        else:
            defaults_restored = True
            log.info("Saved defaults restored")

    # Enable CRC if desired
    if use_crc:
        response = at_get_response('AT%CRC=1')
        err_code, err_str = at_get_result_code(response['result'])
        if err_code == 0:
            log.info("CRC enabled")
        elif err_code == 100 and modem.atConfig['CRC']:
            log.info("Attempted to set CRC when already set")
        else:
            log.error("CRC enable failed (" + err_str + ")")
    elif modem.atConfig['CRC']:
        response = at_get_response('AT%CRC=0')
        err_code, err_str = at_get_result_code(response['result'])
        if err_code == 0:
            log.info("CRC disabled")
        else:
            log.warning("CRC disable failed (" + err_str + ")")

    # Ensure Quiet mode is disabled to receive response codes
    time.sleep(AT_WAIT)
    response = at_get_response('ATS61?')  # S61 = Quiet mode
    err_code, err_str = at_get_result_code(response['result'])
    if err_code == 0:
        if response['response'][0] == '1':
            response = at_get_response('ATQ0')
            err_code, err_str = at_get_result_code(response['result'])
            if err_code != 0:
                err_msg = "Failed to disable Quiet mode (" + err_str + ")"
                log.error(err_msg)
                sys.exit(err_msg)
        log.info("Quiet mode disabled")
    else:
        err_msg = "Failed query of Quiet mode S-register ATS61? (" + err_str + ")"
        log.error(err_msg)
        sys.exit(err_msg)
    modem.atConfig['Quiet'] = False

    # Enable echo to validate receipt of AT commands
    time.sleep(AT_WAIT)
    response = at_get_response('ATE1')
    err_code, err_str = at_get_result_code(response['result'])
    if err_code == 0:
        log.info("Echo enabled")
    else:
        log.warning("Echo enable failed (" + err_str + ")")

    # Configure verbose error code (OK / ERROR) for easier result validation
    time.sleep(AT_WAIT)
    if verbose:
        response = at_get_response('ATV1')
    else:
        response = at_get_response('ATV0')
    err_code, err_str = at_get_result_code(response['result'])
    if err_code == 0:
        log.info("Verbose " + ("enabled" if verbose else "disabled"))
        modem.atConfig['Verbose'] = verbose
    else:
        log.warning("Verbose " + ("enable" if verbose else "disable") + " failed (" + err_str + ")")

    # Get modem ID
    time.sleep(AT_WAIT)
    response = at_get_response('AT+GSN')
    err_code, err_str = at_get_result_code(response['result'])
    if err_code == 0:
        mobile_id = response["response"][0].lstrip('+GSN:').strip()
        if mobile_id != '':
            log.info("Mobile ID: " + str(mobile_id))
            modem.mobileId = mobile_id
        else:
            log.warning("Mobile ID not returned")
    else:
        log.error("Get Mobile ID failed (" + err_str + ")")


def at_check_sat_status():
    """ Checks satellite status using Trace Log Mode to update state and statistics """
    global _debug
    global log
    global modem
    global thread_lock

    AT_SATSTATUS_QUERY = 'ATS90=3 S91=1 S92=1 S122? S116?'

    with thread_lock:
        if _debug:
            log.debug("Checking satellite status. Previous control state: " + modem.satStatus['CtrlState'])
        response = at_get_response(AT_SATSTATUS_QUERY)
        if not response['timeout']:
            err_code, err_str = at_get_result_code(response['result'])
            if err_code == 0:
                oldSatCtrlState = modem.satStatus['CtrlState']
                newSatCtrlState = modem.ctrlStates[int(response['response'][0])]
                if newSatCtrlState != oldSatCtrlState:
                    log.info("Satellite control state change: OLD=" + oldSatCtrlState + " NEW=" + newSatCtrlState)
                    modem.satStatus['CtrlState'] = newSatCtrlState

                    # Key events for relevant state changes and statistics tracking
                    if newSatCtrlState == 'Waiting for GNSS fix':
                        modem.systemStats['lastGNSSStartTime'] = int(time.time())
                        modem.systemStats['nGNSS'] += 1
                    elif newSatCtrlState == 'Registration in progress':
                        modem.systemStats['lastRegStartTime'] = int(time.time())
                        modem.systemStats['nRegistration'] += 1
                    elif newSatCtrlState == 'Downloading Bulletin Board':
                        modem.satStatus['BBWait'] = True
                        modem.systemStats['lastBBStartTime'] = time.time()
                    elif newSatCtrlState == 'Registration in progress':
                        modem.systemStats['lastRegStartTime'] = int(time.time())
                    elif newSatCtrlState == 'Active':
                        if modem.satStatus['Blocked'] == True:
                            log.info("Blockage cleared")
                            blockDuration = int(time.time() - modem.systemStats['lastBlockStartTime'])
                            if modem.systemStats['avgBlockageDuration'] > 0:
                                modem.systemStats['avgBlockageDuration'] = int((blockDuration + modem.systemStats['avgBlockageDuration'])/2)
                            else:
                                modem.systemStats['avgBlockageDuration'] = blockDuration
                        modem.satStatus['Registered'] = True
                        modem.satStatus['Blocked'] = False
                        modem.satStatus['BBWait'] = False
                        if modem.systemStats['lastRegStartTime'] > 0:
                            regDuration = int(time.time() - modem.systemStats['lastRegStartTime'])
                        else:
                            regDuration = 0
                        if modem.systemStats['avgRegistrationDuration'] > 0:
                            modem.systemStats['avgRegistrationDuration'] = int((regDuration + modem.systemStats['avgRegistrationDuration'])/2)
                        else:
                            modem.systemStats['avgRegistrationDuration'] = regDuration
                    elif newSatCtrlState == 'Blocked':
                        modem.satStatus['Blocked'] = True
                        modem.systemStats['lastBlockStartTime'] = time.time()
                        log.info("Blockage started")

                    # Other transitions for statistics tracking:
                    if oldSatCtrlState == 'Waiting for GNSS fix' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                        gnssDuration = int(time.time() - modem.systemStats['lastGNSSStartTime'])
                        log.info("GNSS acquired in " + str(gnssDuration) + " seconds")
                        if modem.systemStats['avgGNSSFixDuration'] > 0:
                            modem.systemStats['avgGNSSFixDuration'] = int((gnssDuration + modem.systemStats['avgGNSSFixDuration'])/2)
                        else:
                            modem.systemStats['avgGNSSFixDuration'] = gnssDuration
                    if oldSatCtrlState == 'Downloading Bulletin Board' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                        bbDuration = int(time.time() - modem.systemStats['lastBBStartTime'])
                        log.info("Bulletin Board downloaded in: " + str(bbDuration) + " seconds")
                        if modem.systemStats['avgBBReacquireDuration'] > 0:
                            modem.systemStats['avgBBReacquireDuration'] = int((bbDuration + modem.systemStats['avgBBReacquireDuration'])/2)
                        else:
                            modem.systemStats['avgBBReacquireDuration'] = bbDuration
                    if oldSatCtrlState == 'Active' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                        modem.systemStats['lastRegStartTime'] = int(time.time())
                        modem.systemStats['nRegistration'] += 1

                CN0 = int(response['response'][1]) / 100.0
                if modem.systemStats['avgCN0'] == 0:
                    modem.systemStats['avgCN0'] = CN0
                else:
                    modem.systemStats['avgCN0'] = round((modem.systemStats['avgCN0'] + CN0) / 2.0, 2)
            else:
                log.error("Bad response to satellite status query (" + err_str + ")")
        else:
            log.warning("Timeout occurred on satellite status query")
    return


def handle_mt_tracking_command(msg_content, msg_sin=255, msg_min=1):
    """ Expects to get SIN 255 MIN 1 'reconfigure tracking interval, in minutes, in a range from 1-1440 
    :param msg_content: Mobile-Terminated message payload with format <SIN><MIN><interval>
    :param msg_sin placeholder for future features
    :param msg_min placeholder for future features
    """
    global log
    global tracking_interval

    tracking_thread = None
    if msg_sin == 255 and msg_min == 1:
        new_tracking_interval_min = int(msg_content[2:], 16)
        for t in threading.enumerate():
            if t.name == 'GetSendLocation':
                tracking_thread = t
        if (0 <= new_tracking_interval_min <= 1440) and ((new_tracking_interval_min * 60) != tracking_interval):
            log.info("Changing tracking interval to " + str(tracking_interval) + " seconds")
            tracking_interval = new_tracking_interval_min * 60
            tracking_thread.change_interval(tracking_interval)
            if tracking_interval == 0:
                tracking_thread.stop_timer()
            else:
                at_get_location_send()
        else:
            log.warning("Invalid tracking interval change requested (" + str(new_tracking_interval_min)
                        + " minutes")
            # TODO: send an error response indicating 'invalid interval' over the air
    else:
        log.warning("Unsupported command SIN=" + str(msg_sin) + " MIN=" + str(msg_min))


def at_check_mt_messages():
    """ Checks for Mobile-Terminated messages in modem queue and retrieves if present.
     Logs a record of the receipt, and handles supported messages
    """
    global _debug
    global log
    global thread_lock
    global modem

    msg_retrieved = False
    with thread_lock:
        if _debug:
            log.debug("Checking for Mobile-Terminated messages")
        response = at_get_response('AT%MGFN')
        if not response['timeout']:
            err_code, err_str = at_get_result_code(response['result'])
            if err_code == 0:
                msg_summary = response['response'][0].replace('%MGFN:', '').strip()
                if msg_summary:
                    msg_parms = msg_summary.split(',')
                    msg_name = msg_parms[0]
                    # msgNum = msg_parms[1]
                    # msgPriority = msg_parms[2]
                    msg_sin = int(msg_parms[3])   # TODO: broken on RPi?
                    msg_state = int(msg_parms[4])
                    msg_len = int(msg_parms[5])
                    if msg_state == 2:  # Complete and not read
                        # TODO: more elegant handling of data_type based on length, pass to helper functions for parsing
                        if msg_sin == 128:
                            data_type = '1'  # Text
                        elif msg_sin == 255:
                            data_type = '2'  # ASCII-Hex
                        else:
                            data_type = '3'  # base64
                        response = at_get_response('AT%MGFG=' + msg_name + "," + data_type)
                        err_code, err_str = at_get_result_code(response['result'])
                        if err_code == 0:
                            msg_retrieved = True
                            msg_envelope = response['response'][0].replace('%MGFG:', '').strip().split(',')
                            msg_content = msg_envelope[7]
                            if data_type == '1':
                                msg_min = int(msg_content.replace('"', '')[1:3], 16)
                                msg_content_str = msg_content.replace('"', '')[3:]
                            elif data_type == '2':
                                msg_min = int(msg_content[0:2])
                                msg_content_str = '0x' + str(msg_content)
                            elif data_type == '3':
                                msg_content_bytes = bytearray(binascii.a2b_base64(msg_content))
                                msg_min = int(msg_content_bytes[0])
                                msg_content_str = str(msg_content)
                            else:
                                msg_min = 0
                                msg_content_str = 'unknown'
                            log.info("Mobile Terminated %d-byte message received (SIN=%d MIN=%d) rawpayload:%s",
                                     msg_len, msg_sin, msg_min, msg_content_str)
                            if modem.systemStats['avgMTMsgSize'] == 0:
                                modem.systemStats['avgMTMsgSize'] = msg_len
                            else:
                                modem.systemStats['avgMTMsgSize'] = int(
                                    (modem.systemStats['avgMTMsgSize'] + msg_len) / 2)
                        else:
                            log.error("Could not get MT message (" + err_str + ")")
            else:
                log.error("Could not get new MT message info (" + err_str + ")")
        else:
            log.warning("Timeout occurred on MT message query")

    # TODO: more elegant/generic processing with helper functions
    if msg_retrieved:
        if msg_sin == 255:
            handle_mt_tracking_command(msg_content, msg_sin, msg_min)
        else:
            log.info("Message SIN=%d MIN=%d not handled", msg_sin, msg_min)

    return


def at_send_message(dataString, dataFormat=1, SIN=128, MIN=1, priority=4):
    """ Transmits a Mobile-Originated message. If ASCII-Hex format is used, 0-pads to nearest byte boundary
    :param dataString: data to be transmitted
    :param dataFormat: 1=Text (default), 2=ASCII-Hex, 3=base64
    :param SIN: first byte of message (default 128 "user")
    :param MIN: second byte of message (default 1 "user")
    :return: nothing
    """
    global _debug
    global log
    global thread_lock
    global modem
    global _at_timeout_count
    global AT_MAX_TIMEOUTS

    mo_msg_name = str(int(time.time()))[:8]
    mo_msg_priority = priority
    mo_msg_sin = SIN
    mo_msg_min = MIN
    mo_msg_format = dataFormat
    if dataFormat == 1:
        mo_msg_content = '"' + dataString + '"'
    else:
        mo_msg_content = dataString
        if dataFormat == 2 and len(dataString)%2 > 0:
            mo_msg_content += '0'     # insert 0 padding to byte boundary
    with thread_lock:
        response = at_get_response(
            'AT%MGRT="' + mo_msg_name + '",' + str(mo_msg_priority) + ',' + str(mo_msg_sin) + '.' + str(
                mo_msg_min) + ',' + str(mo_msg_format) + ',' + mo_msg_content)
        if not response['timeout']:
            err_code, err_str = at_get_result_code(response['result'])
            mo_submit_time = time.time()
            if err_code == 0:
                msg_complete = False
                status_poll_count = 0
                while not msg_complete and _at_timeout_count < AT_MAX_TIMEOUTS:
                    time.sleep(1)
                    status_poll_count += 1
                    if _debug:
                        log.debug("MGRS queries: " + str(status_poll_count))
                    response = at_get_response('AT%MGRS="' + mo_msg_name + '"')
                    err_code, err_str = at_get_result_code(response['result'])
                    if err_code == 0:
                        res_param = response['response'][0].split(',')
                        res_header = res_param[0]
                        res_msg_no = res_param[1]
                        res_priority = int(res_param[2])
                        res_sin = int(res_param[3])
                        res_state = int(res_param[4])
                        res_size = int(res_param[5])
                        res_sent = int(res_param[6])
                        if res_state > 5:
                            msg_complete = True
                            if res_state == 6:
                                msg_latency = int(time.time() - mo_submit_time)
                                log.info("MO message SIN=%d MIN=%d (%d bytes) completed in %d seconds",
                                         mo_msg_sin, mo_msg_min, res_size, msg_latency)
                                if modem.systemStats['avgMOMsgSize'] == 0:
                                    modem.systemStats['avgMOMsgSize'] = res_size
                                else:
                                    modem.systemStats['avgMOMsgSize'] = int(
                                        (modem.systemStats['avgMOMsgSize'] + res_size) / 2)
                                if modem.systemStats['avgMOMsgLatency_s'] == 0:
                                    modem.systemStats['avgMOMsgLatency_s'] = msg_latency
                                else:
                                    modem.systemStats['avgMOMsgLatency_s'] = int(
                                        (modem.systemStats['avgMOMsgLatency_s'] + msg_latency) / 2)
                            else:
                                log.info("MO message (" + str(res_size) + " bytes) failed after " +
                                         str(int(time.time() - mo_submit_time)) + " seconds")
                    elif err_code == 109:
                        if _debug:
                            print("Message complete, Unavailable")
                        break
                    else:
                        log.error("Error getting message state (" + err_str + ")")
            else:
                log.error("Message submit error (" + err_str + ")")
        else:
            log.warning("Timeout attempting to submit MO message")


def build_location_msg_send(loc):
    """ Prepares a specific binary-optimized location report using SIN=255, MIN=255
    :param loc: a Location object
    :return: nothing; calls at_send_message function
    """

    data_fields = [
        (loc.timestamp, '031b'),
        (loc.latitude, '024b'),
        (loc.longitude, '025b'),
        (loc.altitude, '08b'),
        (loc.speed, '08b'),
        (loc.heading, '09b'),
        (loc.satellites, '04b'),
        (loc.fixtype, '02b'),
        (loc.PDOP, '05b')
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
    at_send_message(hex_str, dataFormat=2, SIN=255, MIN=255)


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
            loc.altitude = int(GGAaltitude) # 545.4 = meters above mean sea level

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
            loc.HDOP = max(int(float(GSAhdop)), 32)
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
            loc.satellites = int(GSVsatellites)

        else:
            err_str = "NMEA sentence type not recognized"
    else:
        err_str = "Invalid NMEA checksum"

    return err_str == '', err_str


def at_get_location_send():
    """ Queries GPS NMEA strings from the modem and submits to a send/processing routine. """
    global log
    global modem
    global thread_lock
    global tracking_interval

    MIN_STALE_SECS = 1
    MAX_STALE_SECS = 600
    MIN_WAIT_SECS = 1
    MAX_WAIT_SECS = 600

    # TODO: Enable or disable AT%TRK tracking mode based on update interval, to improve fix times
    stale_secs = min(MAX_STALE_SECS, max(MIN_STALE_SECS, int(tracking_interval / 2)))
    wait_secs = min(MAX_WAIT_SECS, max(MIN_WAIT_SECS, int(max(45, stale_secs - 1))))
    NMEA_sentences = '"GGA","RMC","GSA","GSV"'
    if modem.mobileId == '00000000SKYEE3D':
        NMEA_sentences = NMEA_sentences.replace(',"GSA"', '')
    loc = Location()
    with thread_lock:
        log.debug("requesting location to send")
        modem.GNSSStats['nGNSS'] += 1
        modem.GNSSStats['lastGNSSReqTime'] = int(time.time())
        response = at_get_response('AT%GPS=' + str(stale_secs) + ',' + str(wait_secs) + ',' +
                                   NMEA_sentences, at_timeout=wait_secs + 5)
        if not response['timeout']:
            err_code, err_str = at_get_result_code(response['result'])
            if err_code == 0:
                gnssFixDuration = int(time.time()) - modem.GNSSStats['lastGNSSReqTime']
                if _debug:
                    print("GNSS response time [s]: " + str(gnssFixDuration))
                if modem.GNSSStats['avgGNSSFixDuration'] > 0:
                    modem.GNSSStats['avgGNSSFixDuration'] = int((gnssFixDuration +
                                                                 modem.GNSSStats['avgGNSSFixDuration'])/2)
                else:
                    modem.GNSSStats['avgGNSSFixDuration'] = gnssFixDuration
                for res in response['response']:
                    if res.startswith('$GP') or res.startswith('$GL'):     # TODO: Galileo/Beidou?
                        NMEAsentence = res
                        success, err = parse_nmea_to_location(NMEAsentence, loc)
                        if not success:
                            log.error(str(err))
            else:
                log.error("Unable to get GNSS (" + err_str + ")")
            build_location_msg_send(loc)
            if tracking_interval > 0:
                log.debug("Next location report in ~" + str(tracking_interval) + " seconds.")
        else:
            log.warning("Timeout occurred on GNSS query")
    return


def at_wait_boot(wait=15):
    """ Waits for key strings output by the modem on (re)boot and returns boolean for success 
    :param wait: an optional timeout in seconds
    :return: Boolean success
             error string on failure
    """
    # TODO: UNUSED...deprecate or improve handling
    global _debug
    global log
    global ser

    errStr = ''
    BOOT_MSG = 'uC Loader'
    AT_INIT_MSG = 'AT Command I/F'
    log.info("Waiting for boot initialization...")
    initVerified = False
    initTick = 0
    INIT_TIMEOUT = wait
    nLines = 0
    serOutLine = ''
    while initTick < INIT_TIMEOUT and not initVerified:
        time.sleep(1)
        if _debug: print("Countdown: " + str(INIT_TIMEOUT - initTick))
        while ser.inWaiting() > 0:
            rChar = ser.read(1)
            if rChar == '\n':
                nLines += 1
                if _debug:
                    rChar = '<lf>'
                    serOutLine += rChar
                    print('Received line: ' + serOutLine)
                if BOOT_MSG in serOutLine:
                    log.info("Modem booting...")
                    if initTick < 5: initTick = 5  # add a bit of extra time to complete
                elif AT_INIT_MSG in serOutLine:
                    log.info("AT command mode ready")
                    initVerified = True
                serOutLine = ''     # clear for next line parsing
            elif rChar == '\r':
                if _debug: rChar = '<cr>'
            serOutLine += rChar
        initTick += 1

    return initVerified, errStr


def init_windows(default_log_name):
    """ Initializes for Windows testing by presenting a dialog to assign COM port and log file name.
      Also allows user to enable/disable verbose debug and set a tracking interval
    :param default_log_name the name that will be used if nothing is selected
    :returns serial port name e.g. 'COM1'
            log file name e.g. 'myLogFile.log'
    """
    global _debug
    global tracking_interval

    try:
        import Tkinter as tk
    except ImportError:
        raise ImportError("Unable to import Tkinter or tkFileDialog.")
    import tkFileDialog

    try:
        import serialportfinder
    except ImportError:
        raise ImportError("Unable to import serialportfinder.py - check root directory")
    serial_port_list = serialportfinder.listports()
    if len(serial_port_list) == 0 or serial_port_list[0] == '':
        sys.exit("No serial COM ports found.")

    global ser_name

    print("Windows environment detected.")
    _debug = True

    dialog = tk.Tk()
    dialog.title("Select Options...")
    dialog.geometry("325x150+30+30")
    port_sel_label = tk.Label(dialog, text="Select COM port")
    port_selection = tk.StringVar(dialog)
    port_selection.set(serial_port_list[0])
    option = apply(tk.OptionMenu, (dialog, port_selection) + tuple(serial_port_list))
    option.grid(row=0, column=0, sticky='EW')
    port_sel_label.grid(row=0, column=1, sticky='W')

    dbg_flag = tk.IntVar()
    dbg_checkbox = tk.Checkbutton(dialog, text="Enable debug", variable=dbg_flag)
    dbg_checkbox.grid(row=1, column=0, columnspan=2, padx=5, pady=5)
    dbg_checkbox.select()

    track = tk.IntVar()
    track.set(tracking_interval)
    track_label = tk.Label(dialog, text="Tracking interval minutes (0..1440)")
    track_label.grid(row=2, column=1, sticky="W")
    track_box = tk.Entry(dialog, text="Tracking interval", textvariable=track, justify='right')
    track_box.grid(row=2, column=0, padx=5, pady=5, sticky="E")

    def ok_select():
        global ser_name
        global _debug
        global tracking_interval
        ser_name = port_selection.get()
        _debug = dbg_flag.get() == 1
        if 0 <= track.get() <= 1440:
            tracking_interval = track.get() * 60
        dialog.quit()

    def on_closing():
        sys.exit('COM port port_selection cancelled.')

    button_ok = tk.Button(dialog, text='OK', command=ok_select, width=10)
    button_ok.grid(row=3, column=0, padx=5, pady=5)

    button_cancel = tk.Button(dialog, text="Cancel", command=on_closing, width=10)
    button_cancel.grid(row=3, column=1, padx=5, pady=5)

    dialog.protocol('WM_DELETE_WINDOW', on_closing)
    dialog.mainloop()
    dialog.destroy()
    print("Options selected - Debug: %s  Tracking: %d seconds"
          % ("enabled" if _debug else "disabled", tracking_interval))

    file_formats = [('Log', '*.log'), ('Text', '*.txt')]
    logfile_selector = tk.Tk()
    logfile_selector.withdraw()
    filename = tkFileDialog.asksaveasfilename(defaultextension='.log', initialfile=default_log_name,
                                              parent=logfile_selector, filetypes=file_formats,
                                              title="Save log file as...")
    if filename == '':
        print("Logfile port_selection dialog cancelled. Using default filename " + default_log_name)
        filename = default_log_name
    logfile_selector.destroy()
    return ser_name, filename


def init_log(log_filename, log_max_mb):
    """ Initializes logging to file and console
    :param log_filename the name of the file
    :param log_max_mb the max size of the file in megabytes, before wrapping occurs
    :return log object
    """
    log_formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d,(%(threadName)-10s),' \
                                          '[%(levelname)s],%(funcName)s(%(lineno)d),%(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
    log_handler = RotatingFileHandler(log_filename, mode='a', maxBytes=log_max_mb * 1024 * 1024,
                                      backupCount=2, encoding=None, delay=0)
    log_handler.setFormatter(log_formatter)
    if _debug:
        log_handler.setLevel(logging.DEBUG)
    else:
        log_handler.setLevel(logging.INFO)
    log_object = logging.getLogger(log_filename)
    log_object.setLevel(log_handler.level)
    log_object.addHandler(log_handler)
    console = logging.StreamHandler()
    console.setFormatter(log_formatter)
    console.setLevel(logging.DEBUG)
    log_object.addHandler(console)
    return log_object


def init_com(max_attempts=3, fish_dish=None):
    """ Initializes communications with the modem. If using Raspberry Pi headless, a Fish Dish is assumed
     Calls an AT command dispatcher and flashes a LED while waiting for completion
    :param max_attempts - the maximum number of tries sending a basic AT command
    :param fish_dish - an optional object to provide headless notification (LED flasher)
    :return Boolean success
    """
    global _at_timeout_count

    log.info("Attempting to establish modem communications")
    success = False
    if fish_dish is not None:
        fish_dish.led_on('yellow')
        fish_dish.led_flasher.start_timer()
    init_verified = at_attach(max_attempts)
    if init_verified:
        _at_timeout_count = 0
        success = True
        if fish_dish is not None:
            fish_dish.led_flasher.stop_timer()
            fish_dish.led_off('yellow')
            fish_dish.led_on('green')
    return success


def monitor_com(timeout_count, max_timeouts, recon_count, timer_threads, fish_dish=None):
    """ TODO: docs"""
    global log

    MAX_RECONNECT_ATTEMPTS = 3

    if timeout_count >= max_timeouts:
        if recon_count == 0:  # only log message once per disconnect
            log.warning("AT responses timed out " + str(_at_timeout_count) +
                        " times. Attempting to re-establish communications")
            for t in threading.enumerate():
                if t.name in timer_threads:
                    t.stop_timer()
        recon_count += 1
        reconnected = init_com(max_attempts=3, fish_dish=fish_dish)
        if not reconnected:
            log.info("Reconnect attempts: " + str(recon_count) + "/" + str(MAX_RECONNECT_ATTEMPTS))
            if recon_count == MAX_RECONNECT_ATTEMPTS:
                err_str = "Modem communications could not be reestablished...exiting."
                log.error(err_str)
                sys.exit(err_str)
        else:
            for t in threading.enumerate():
                if t.name in timer_threads:
                    if fish_dish is not None and t.name == fish_dish.led_flasher.name:
                        pass
                    else:
                        t.restart_timer()
    return recon_count


def main():     # TODO: trim more functions out of main, refactor for module import to run as thread

    global _debug
    global log
    global ser
    global modem
    global thread_lock
    global tracking_interval
    global _shutdown
    global _at_timeout_count
    global AT_MAX_TIMEOUTS

    _shutdown = False
    AT_MAX_TIMEOUTS = 3

    ser = None
    SERIAL_BAUD = 9600

    modem = None

    # Timer intervals (seconds)
    SAT_STATUS_INTERVAL = 5
    MT_MESSAGE_CHECK_INTERVAL = 15
    tracking_interval = 900

    # Thread lock for background processes to avoid overlapping AT requests
    thread_lock = threading.RLock()
    threads = []

    # Derive run options from command line
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
    user_options = parser.parse_args()

    if not '.' in user_options.logfile:
        log_filename = user_options.logfile + '.log'
    else:
        log_filename = user_options.logfile
    log_max_mb = user_options.log_size

    _debug = user_options.debug

    if user_options.tracking is not None:
        if 0 <= user_options.tracking <= 1440:
            tracking_interval = int(user_options.tracking * 60)
        else:
            sys.exit("Invalid tracking interval, must be in range 0..1440")

    # Pre-initialization of platform
    try:  # GPIO bindings (headless Raspberry Pi using FishDish I/O board)
        import RPi.GPIO as GPIO     # Successful import of this module implies running on Raspberry Pi
        print("\n ** Raspberry Pi / GPIO environment detected")
        GPIO.setmode(GPIO.BCM)
        if user_options.fish_dish:
            fish_dish = RpiFishDish(GPIO)
            threads.append(fish_dish.led_flasher.name)
            fish_dish.led_flasher.start()
            modem_io = RpiModemIO(GPIO)
        else:
            fish_dish = None
            modem_io = None
        log_filename = '/home/pi/' + log_filename
        SERIAL_NAME = '/dev/ttyUSB0'  # TODO: validate RPi USB/serial port assignment

    except ImportError:
        fish_dish = None
        modem_io = None

        if sys.platform.lower().startswith('win32'):
            print("\n ** Windows environment detected")
            SERIAL_NAME, log_filename = init_windows(log_filename)

        elif sys.platform.lower().startswith('linux2'):
            # Assumes linux2 platform is MultiTech Conduit AEP.  NOTE: also true for RPi.
            print("\n ** Linux environment detected (assuming MultiTech Conduit AEP)")
            # consider prefixing the below with '/home/root'
            log_filename = '/home/root' + log_filename    # TODO: validate path availability
            subprocess.call('mts-io-sysfs store ap1/serial-mode rs232', shell=True)
            SERIAL_NAME = '/dev/ttyAP1'

        else:
            sys.exit('ERROR: Operation undefined on current platform. Please use Windows, RPi/GPIO or MultiTech AEP.')

    # Set up log file
    log = init_log(log_filename, log_max_mb)

    if _debug:
        print("\n\n\n**** PROGRAM STARTING ****\n\n\n")

    try:
        # TODO: handle serial exception for writeTimeout vs. write_timeout
        ser = serial.Serial(port=SERIAL_NAME, baudrate=SERIAL_BAUD,
                            timeout=None, writeTimeout=0,
                            xonxoff=False, rtscts=False, dsrdtr=False)

        if ser.isOpen():
            start_time = str(datetime.datetime.utcnow())

            log.info("Connected to serial port " + ser.name + " at " + str(ser.baudrate) + " baud")
            sys.stdout.flush()

            modem = idpmodem.IDPModem()
            ever_connected = False
            _at_timeout_count = 0

            # Attempt to solicit AT response for some time before exiting
            ever_connected = init_com(max_attempts=30, fish_dish=fish_dish)
            if not ever_connected:
                err_str = "Modem communications could not be established...exiting."
                log.error(err_str)
                sys.exit(err_str)

            at_init_modem(use_crc=user_options.use_crc)

            # (Proxy) Timer threads for background processes

            status_thread = RepeatingTimer(seconds=SAT_STATUS_INTERVAL, name='check_sat_status',
                                           callback=at_check_sat_status)
            threads.append(status_thread.name)
            status_thread.start_timer()
            status_thread.start()
            at_check_sat_status()

            mt_polling_thread = RepeatingTimer(seconds=MT_MESSAGE_CHECK_INTERVAL, name='check_mt_messages',
                                               callback=at_check_mt_messages)
            threads.append(mt_polling_thread.name)
            mt_polling_thread.start_timer()
            mt_polling_thread.start()

            tracking_thread = RepeatingTimer(seconds=tracking_interval, name='tracking',
                                             callback=at_get_location_send)
            threads.append(tracking_thread.name)
            tracking_thread.start_timer()
            tracking_thread.start()
            at_get_location_send()

            reconnect_attempts = 0
            while not _shutdown:
                # monitor communications
                reconnect_attempts = monitor_com(_at_timeout_count, AT_MAX_TIMEOUTS, reconnect_attempts, threads,
                                                 fish_dish)
                time.sleep(0.5)

    except KeyboardInterrupt:
        log.info("Execution stopped by keyboard interrupt.")

    except Exception, e:
        err_str = "Exception in user code:" + '-' * 40 + '\n' + traceback.format_exc()
        # err_str = "Error on line {}:".format(sys.exc_info()[-1].tb_lineno) + ',' + str(type(e)) + ',' + str(e)
        log.error(err_str)
        raise

    finally:
        end_time = str(datetime.datetime.utcnow())
        log.info("idpmodemsample exiting")
        if ever_connected and modem is not None:
            log.info("*" * 30 + " MODEM STATISTICS " + "*" * 30)
            log.info("* Mobile ID: %s \n* start: %s \n* end: %s", modem.mobileId, start_time, end_time)
            stats_list = modem.get_statistics()
            for stat in stats_list:
                log.info("* " + stat + ":" + str(stats_list[stat]))
            log.info("*" * 75)
        for t in threading.enumerate():
            if _debug:
                print("Assessing " + t.name)
            if t.name in threads:
                if _debug:
                    print("Found " + t.name + " in threads list")
                t.stop_timer()
                t.terminate()
                t.join()
        if fish_dish is not None or modem_io is not None:
            GPIO.cleanup()
        if ser is not None and ser.isOpen():
            ser.close()
            log.info("Closing serial port " + SERIAL_NAME)
        if _debug:
            print("\n\n*** END PROGRAM ***\n\n")


if __name__ == "__main__":
    main()