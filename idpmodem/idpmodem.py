"""
Data structure and operations for a SkyWave/ORBCOMM IDP modem using AT commands.

.. todo::
   Reference contextual documentation pages for things like event notifications, low power, etc.

"""
__version__ = "1.0.2"

import crcxmodem
from collections import OrderedDict
import time
import threading
import binascii
import base64
import struct
import sys
# import json


class Modem(object):
    """Abstracts attributes and statistics related to an IDP modem"""

    ctrl_states = [
        'Stopped',
        'Waiting for GNSS fix',
        'Starting search',
        'Beam search',
        'Beam found',
        'Beam acquired',
        'Beam switch in progress',
        'Registration in progress',
        'Receive only',
        'Downloading Bulletin Board',
        'Active',
        'Blocked',
        'Confirm previously registered beam',
        'Confirm requested beam',
        'Connect to confirmed beam'
        ]

    at_err_result_codes = {
        '0': 'OK',
        '4': 'UNRECOGNIZED',
        '100': 'INVALID CRC SEQUENCE',
        '101': 'UNKNOWN COMMAND',
        '102': 'INVALID COMMAND PARAMETERS',
        '103': 'MESSAGE LENGTH EXCEEDS PERMITTED SIZE FOR FORMAT',
        '104': 'RESERVED',
        '105': 'SYSTEM ERROR',
        '106': 'INSUFFICIENT RESOURCES',
        '107': 'MESSAGE NAME ALREADY IN USE',
        '108': 'TIMEOUT OCCURRED',
        '109': 'UNAVAILABLE',
        '110': 'RESERVED',
        '111': 'RESERVED',
        '112': 'ATTEMPT TO WRITE READ-ONLY PARAMETER'
        }

    wakeup_intervals = {
        '5 seconds': 0,
        '30 seconds': 1,
        '1 minute': 2,
        '3 minute': 3,
        '10 minute': 4,
        '30 minute': 5,
        '60 minute': 6,
        '2 minute': 7,
        '5 minute': 8,
        '15 minute': 9,
        '20 minute': 10
    }

    power_modes = {
        'Mobile Powered': 0,
        'Fixed Powered': 1,
        'Mobile Battery': 2,
        'Fixed Battery': 3,
        'Mobile Minimal': 4,
        'Mobile Stationary': 5
    }

    gnss_modes = {
        'GPS': 0,               # HW v4
        'GLONASS': 1,           # HW v5
        'BEIDOU': 2,            # HW v5.2
        'GPS+GLONASS': 10,      # UBX-M80xx
        'GPS+BEIDOU': 11,       # UBX-M80xx
        'GLONASS+BEIDOU': 12    # UBX-M80xx
    }

    gnss_dpm_modes = {
        'Portable': 0,
        'Stationary': 2,
        'Pedestrian': 3,
        'Automotive': 4,
        'Sea': 5,
        'Air 1g': 6,
        'Air 2g': 7,
        'Air 4g': 8
    }
    
    def __init__(self, serial_port, log=None, debug=False):
        """
        Initializes attributes and pointers used by Modem class methods.

        :param serial_port: a pySerial.serial object
        :param log: an optional logger
        :param debug: Boolean option for verbose trace

        """
        self.mobile_id = 'unknown'
        self.is_connected = False
        self.at_config = {
            'CRC': False,
            'Echo': True,
            'Verbose': True,
            'Quiet': False
        }
        self.sat_status = {
            'Registered': False,
            'Blocked': False,
            'RxOnly': False,
            'BBWait': False,
            'CtrlState': 'Stopped'
        }
        self.event_notifications = OrderedDict({
            ('newGnssFix', False),
            ('newMtMsg', False),
            ('moMsgComplete', False),
            ('modemRegistered', False),
            ('modemReset', False),
            ('jamCutState', False),
            ('modemResetPending', False),
            ('lowPowerChange', False),
            ('utcUpdate', False),
            ('fixTimeout', False),
            ('eventCached', False)
        })
        self.wakeup_interval = self.wakeup_intervals['5 seconds']
        self.power_mode = self.power_modes['Mobile Powered']
        self.asleep = False
        self.antenna_cut = False
        self.stats_start_time = 0
        self.system_stats = {
            'nGNSS': 0,
            'nRegistration': 0,
            'nBBAcquisition': 0,
            'nBlockage': 0,
            'lastGNSSStartTime': 0,
            'lastRegStartTime': 0,
            'lastBBStartTime': 0,
            'lastBlockStartTime': 0,
            'avgGNSSFixDuration': 0,
            'avgRegistrationDuration': 0,
            'avgBBReacquireDuration': 0,
            'avgBlockageDuration': 0,
            'lastATResponseTime_ms': 0,
            'avgATResponseTime_ms': 0,
            'avgMOMsgLatency_s': 0,
            'avgMOMsgSize': 0,
            'avgMTMsgSize': 0,
            'avgCN0': 0.0
        }
        self.gnss_mode = self.gnss_modes['GPS']
        self.gnss_continuous = 0
        self.gnss_dpm_mode = self.gnss_dpm_modes['Portable']
        self.gnss_stats = {
            'nGNSS': 0,
            'lastGNSSReqTime': 0,
            'avgGNSSFixDuration': 0,
            'timeouts': 0
        }
        # TODO: track response times per AT command type
        self.at_cmd_stats = {
            'lastResTime': 0,
            'avgResTime': 0
        }
        self.mo_msg_count = 0
        self.mo_queue = []
        self.mt_msg_count = 0
        self.mt_queue = []
        self.hardware_version = 'unknown'
        self.software_version = 'unknown'
        self.at_version = 'unknown'
        self.serial_port = serial_port
        self.at_timeouts = 0
        self.at_timeouts_total = 0
        self.thread_lock = threading.RLock()
        if log is not None:
            self.log = log
        else:
            import logging
            self.log = logging.getLogger("idpmodem")
            log_formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d,(%(threadName)-10s),'
                                                  '[%(levelname)s],%(funcName)s(%(lineno)d),%(message)s',
                                              datefmt='%Y-%m-%d %H:%M:%S')
            log_formatter.converter = time.gmtime
            console = logging.StreamHandler()
            console.setFormatter(log_formatter)
            if debug:
                console.setLevel(logging.DEBUG)
            else:
                console.setLevel(logging.INFO)
            self.log.setLevel(console.level)
            self.log.addHandler(console)
        self.debug = debug
        self.GPIO = {
            "event_notification": None,
            "reset_out": None,
            "pps": None,
            "reset_in": None
        }

    def _get_crc(self, at_cmd):
        """
        Returns the CRC-16-CCITT (initial value 0xFFFF) checksum using crcxmodem module.

        :param at_cmd: the AT command to calculate CRC on
        :return: the CRC for the AT command

        """
        return '{:04X}'.format(crcxmodem.crc(at_cmd, 0xffff))

    def _update_stats_at_response(self, at_send_time, at_cmd):
        """
        Updates the last and average AT command response time statistics.

        :param at_send_time: (integer) the reference time the AT command was sent
        :param at_cmd: the command that was sent

        """
        log = self.log
        at_response_time_ms = int((time.time() - at_send_time) * 1000)
        self.system_stats['lastATResponseTime_ms'] = at_response_time_ms
        log.debug("Response time for %s: %d [ms]" % (at_cmd, at_response_time_ms))
        if self.system_stats['avgATResponseTime_ms'] == 0:
            self.system_stats['avgATResponseTime_ms'] = at_response_time_ms
        else:
            self.system_stats['avgATResponseTime_ms'] = \
                int((self.system_stats['avgATResponseTime_ms'] + at_response_time_ms) / 2)

    def at_get_response(self, at_cmd, at_timeout=10):
        """
        Takes a single AT command, applies CRC if enabled, sends to the modem and waits for response completion.
        Parses the response, line by line, until a result code is received or at_timeout is exceeded.
        Assumes Quiet mode is disabled, and will not pass 'Quiet enable' (ATQ1) to the modem.
        Sets modem object properties (Echo, CRC, Verbose, Quiet) by inference from AT response.

        :param at_cmd: the AT command to send
        :param at_timeout: the time in seconds to wait for a response
        :return: a ``dictionary`` containing:

           - ``echo`` the AT command sent (including CRC if applied) or empty string if Echo disabled
           - ``response`` a list of (stripped) strings representing multi-line response
           - ``result`` a string returned after the response when Quiet mode is disabled
              'OK' or 'ERROR' if Verbose is enabled on the modem, or a numeric error code that can be looked up
              in idpmodem.atErrorResultCodes
           - ``checksum`` the CRC (if enabled) or None
           - ``error`` Boolean if CRC is correct
           - ``timeout`` Boolean if AT response timed out

        """
        ser = self.serial_port
        log = self.log
        debug = self.debug

        CHAR_WAIT = 0.05  # time to wait, in seconds, between serial characters

        at_echo = ''
        at_response = []  # container for multi-line response
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
            if debug:
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

        if self.at_config['CRC']:
            to_send = at_cmd + '*' + self._get_crc(at_cmd)
        else:
            to_send = at_cmd
        if "AT%CRC=1" in at_cmd.upper():
            self.at_config['CRC'] = True
            log.debug("CRC enabled for next command")
        elif "AT%CRC=0" in at_cmd.upper():
            self.at_config['CRC'] = False
            log.debug("CRC disabled for next command")

        log.debug("Sending:%s with timeout %d seconds", to_send, at_timeout)
        ser.write(to_send + '\r')
        at_send_time = time.time()

        res_line = ''  # each line of response
        raw_res_line = ''  # used for verbose debug purposes only
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
                            self.at_config['Echo'] = False
                            log.debug("ATE0 (echo disable) requested. Takes effect for next AT command.")
                        else:
                            self.at_config['Echo'] = True
                        at_echo = res_line.strip()  # copy the echo into a function return
                        # <echo><cr> will be not be followed by <lf>
                        # can be followed by <text><cr><lf>
                        # or <cr><lf><text><cr><lf>
                        # or <numeric code><cr>
                        # or <cr><lf><verbose code><cr><lf>
                        res_line = ''  # clear for next line of parsing
                    elif ser.inWaiting() == 0 and res_line.strip() != '':
                        # or <text><cr>...with delay for <lf> between multi-line responses e.g. GNSS?
                        if self.at_config['Verbose']:
                            # case <cr><lf><text><cr>...<lf>
                            # or Quiet mode? --unsupported, suppresses result codes
                            log.debug("Assuming delay between <cr> and <lf> of Verbose response...waiting")
                        else:
                            # case <numeric code><cr> since all other alternatives should have <lf> or other pending
                            log.debug("Assuming receipt <numeric code><cr> with Verbose off: " + res_line.strip())
                            # self.at_config['Verbose'] = False
                            at_result_code = res_line  # copy the result code (numeric string) into a function return
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
                        at_result_code = res_line  # copy the verbose result (OK/ERROR) into a function return
                        if ser.inWaiting() == 0:  # no checksum pending...response complete
                            at_rx_complete = True
                            break
                        else:
                            res_line = ''  # continue parsing next line
                    elif '*' in res_line and len(res_line.strip()) == 5:
                        # <*crc><cr><lf>
                        self.at_config['CRC'] = True
                        at_res_crc = res_line.replace('*', '').strip()
                        at_rx_complete = True
                        break
                    else:
                        # case <cr><lf>
                        # or <text><cr><lf>
                        # or <cr><lf><text><cr><lf>
                        if res_line.strip() == '':
                            # <cr><lf> empty line...not done parsing yet
                            self.at_config['Verbose'] = True
                        else:
                            if res_line.strip() != '':  # don't add empty lines
                                at_response.append(res_line)  # don't include \r\n in function return
                            res_line = ''  # clear for next line parsing
                else:  # a character other than \r or \n
                    res_line += r_char
                    raw_res_line += r_char

            if at_result_code != '':
                if self.at_timeouts > 0:
                    log.debug("Valid AT response received - resetting AT timeout count")
                    self.at_timeouts = 0
                self.at_config['Quiet'] = False
                break

            elif int(time.time()) - at_send_time > at_timeout:
                timed_out = True
                self.at_timeouts += 1
                self.at_timeouts_total += 1
                log.warning("%s command response timed out after %d seconds - %d consecutive timeouts"
                                 % (to_send, at_timeout, self.at_timeouts))
                break

            if debug and int(time.time()) > (at_send_time + at_tick):
                at_tick += 1
                print("Waiting AT response. Tick=" + str(at_tick))

        checksum_ok = False

        if not timed_out:

            if at_res_crc == '':
                self.at_config['CRC'] = False
            else:
                self.at_config['CRC'] = True
                if len(at_response) == 0 and at_result_code != '':
                    str_to_validate = at_result_code
                else:
                    str_to_validate = ''
                    for res_line in at_response:
                        str_to_validate += res_line
                    if at_result_code != '':
                        str_to_validate += at_result_code
                if self._get_crc(str_to_validate) == at_res_crc:
                    checksum_ok = True
                else:
                    expected_checksum = self._get_crc(str_to_validate)
                    log.error("Bad checksum received: *" + at_res_crc + " expected: *" + expected_checksum)

            for i, res_line in enumerate(at_response):
                at_response[i] = res_line.strip()
            at_result_code = at_result_code.strip()

            self._update_stats_at_response(at_send_time, at_cmd)

            log.debug("Raw response: " + raw_res_line)

        return {'echo': at_echo,
                'response': at_response,
                'result': at_result_code,
                'checksum': at_res_crc,
                'error': checksum_ok,
                'timeout': timed_out}

    def at_get_result_code(self, result_code):
        """
        Queries the details of an error response on the AT command interface.

        :param result_code: the value returned by the AT command response
        :returns:
           - the specific error code
           - the string description of the error code

        """
        log = self.log

        error_code = -1
        error_desc = "UNDEFINED result code: " + result_code
        if 'OK' in result_code or result_code == '0':
            error_code = 0

        elif 'ERROR' in result_code or result_code == '':
            with self.thread_lock:

                response = self.at_get_response('ATS80?')

                err_code2, err_desc2 = self.at_get_result_code(response['result'])
                if err_code2 == 0:
                    error_code = int(response['response'][0])
                else:
                    log.error("Error querying last error code from S80: " + err_desc2)

        elif int(result_code) > 0:
            error_code = int(result_code)

        if str(error_code) in self.at_err_result_codes:
            error_desc = self.at_err_result_codes[str(error_code)]

        return error_code, error_desc

    def at_attach(self, at_timeout=1):
        """
        Attempts to attach using basic AT command.

        :return: Boolean success

        """
        log = self.log

        success = False
        with self.thread_lock:
            response = self.at_get_response('AT', at_timeout=at_timeout)
            if response['timeout']:
                log.debug("Failed attempt to establish AT response")
            elif response['result'] != '':
                success = True
                log.info("AT attach confirmed")
            else:
                log.warning("Unexpected response from AT command")
        return success

    def at_initialize_modem(self, use_crc=False, verbose=True):
        """
        Initializes the modem after new connection. Restores saved defaults, disables Quiet mode, enables Echo.

        .. note::
           CRC use is recommended when using a long cable between your controller and the satellite modem.

        :param use_crc: optionally enables CRC on AT commands (e.g. if using long serial cable)
        :param verbose: optionally use verbose mode for results (OK/ERROR)
        :return: Boolean success

        """
        log = self.log
        AT_WAIT = 0.1  # seconds between initialization commands

        # Restore saved defaults - modem AT config will also be inferred
        time.sleep(AT_WAIT)
        defaults_restored = False
        restore_attempts = 0
        while not defaults_restored and restore_attempts < 2:
            restore_attempts += 1
            response = self.at_get_response('ATZ')
            err_code, err_str = self.at_get_result_code(response['result'])
            if err_code == 100 and modem.atConfig['CRC'] == False:
                self.at_config['CRC'] = True
                log.info("ATZ CRC error; retrying with CRC enabled")
            elif err_code != 0:
                err_msg = "Failed to restore saved defaults - exiting (%s)" % err_str
                log.error(err_msg)
                return False
            else:
                defaults_restored = True
                log.info("Saved defaults restored")

        # Enable CRC if desired
        if use_crc:
            response = self.at_get_response('AT%CRC=1')
            err_code, err_str = self.at_get_result_code(response['result'])
            if err_code == 0:
                log.info("CRC enabled")
            elif err_code == 100 and self.at_config['CRC']:
                log.info("Attempted to set CRC when already set")
            else:
                log.error("CRC enable failed (" + err_str + ")")
        elif self.at_config['CRC']:
            response = self.at_get_response('AT%CRC=0')
            err_code, err_str = self.at_get_result_code(response['result'])
            if err_code == 0:
                log.info("CRC disabled")
            else:
                log.warning("CRC disable failed (%s)" % err_str)

        # Ensure Quiet mode is disabled to receive response codes
        time.sleep(AT_WAIT)
        response = self.at_get_response('ATS61?')  # S61 = Quiet mode
        err_code, err_str = self.at_get_result_code(response['result'])
        if err_code == 0:
            if response['response'][0] == '1':
                response = self.at_get_response('ATQ0')
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code != 0:
                    err_msg = "Failed to disable Quiet mode (%s)" % err_str
                    log.error(err_msg)
                    return False
            log.info("Quiet mode disabled")
        else:
            err_msg = "Failed query of Quiet mode S-register ATS61? (" + err_str + ")"
            log.error(err_msg)
            sys.exit(err_msg)
        self.at_config['Quiet'] = False

        # Enable echo to validate receipt of AT commands
        time.sleep(AT_WAIT)
        response = self.at_get_response('ATE1')
        err_code, err_str = self.at_get_result_code(response['result'])
        if err_code == 0:
            log.info("Echo enabled")
        else:
            log.warning("Echo enable failed (" + err_str + ")")

        # Configure verbose error code (OK / ERROR) for easier result validation
        time.sleep(AT_WAIT)
        if verbose:
            response = self.at_get_response('ATV1')
        else:
            response = self.at_get_response('ATV0')
        err_code, err_str = self.at_get_result_code(response['result'])
        if err_code == 0:
            log.info("Verbose " + ("enabled" if verbose else "disabled"))
            self.at_config['Verbose'] = verbose
        else:
            log.warning("Verbose %s failed (%s)" % ("enable" if verbose else "disable", err_str))

        # Get modem ID
        time.sleep(AT_WAIT)
        response = self.at_get_response('AT+GSN')
        err_code, err_str = self.at_get_result_code(response['result'])
        if err_code == 0:
            mobile_id = response["response"][0].lstrip('+GSN:').strip()
            if mobile_id != '':
                log.info("Mobile ID: %s" % mobile_id)
                self.mobile_id = mobile_id
            else:
                log.warning("Mobile ID not returned")
        else:
            log.error("Get Mobile ID failed (%s)" % err_str)

        # Get hardware and firmware versions
        time.sleep(AT_WAIT)
        response = self.at_get_response('AT+GMR')
        err_code, err_str = self.at_get_result_code(response['result'])
        if err_code == 0:
            fw_ver, hw_ver, at_ver = response["response"][0].lstrip('+GMR:').strip().split(",")
            log.info("Hardware: %s | Firmware: %s | AT: %s" % (hw_ver, fw_ver, at_ver))
            self.hardware_version = hw_ver
            self.software_version = fw_ver
            self.at_version = at_ver
        else:
            log.error("Get versions failed (%s)" % err_str)

        # Get relevant configuration (S-register) values
        time.sleep(AT_WAIT)
        self.get_event_notification_control()
        self.get_wakeup_interval()
        self.get_power_mode()
        self.get_gnss_mode()
        self.get_gnss_continuous()
        self.get_gnss_dpm()

        success = self.at_save_config()

        return success

    def at_check_sat_status(self):
        """
        Checks satellite status and updates state and statistics.

        :returns: A ``dictionary`` with:

           - ``success`` Boolean
           - ``changed`` Boolean
           - ``state`` (string from ctrl_states)

        """
        log = self.log
        success = False
        changed = False
        with self.thread_lock:
            log.debug("Checking satellite status. Last known state: " + self.sat_status['CtrlState'])

            # S122: satellite trace status
            # S116: C/N0
            response = self.at_get_response('ATS90=3 S91=1 S92=1 S122? S116?')

            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    old_sat_ctrl_state = self.sat_status['CtrlState']
                    new_sat_ctrl_state = self.ctrl_states[int(response['response'][0])]
                    if new_sat_ctrl_state != old_sat_ctrl_state:
                        changed = True
                        log.info("Satellite control state change: OLD=%s NEW=%s"
                                 % (old_sat_ctrl_state, new_sat_ctrl_state))
                        self.sat_status['CtrlState'] = new_sat_ctrl_state

                        # Key events for relevant state changes and statistics tracking
                        if new_sat_ctrl_state == 'Waiting for GNSS fix':
                            self.system_stats['lastGNSSStartTime'] = int(time.time())
                            self.system_stats['nGNSS'] += 1
                        elif new_sat_ctrl_state == 'Registration in progress':
                            self.system_stats['lastRegStartTime'] = int(time.time())
                            self.system_stats['nRegistration'] += 1
                        elif new_sat_ctrl_state == 'Downloading Bulletin Board':
                            self.sat_status['BBWait'] = True
                            self.system_stats['lastBBStartTime'] = time.time()
                        elif new_sat_ctrl_state == 'Registration in progress':
                            self.system_stats['lastRegStartTime'] = int(time.time())
                        elif new_sat_ctrl_state == 'Active':
                            if self.sat_status['Blocked']:
                                log.info("Blockage cleared")
                                blockage_duration = int(time.time() - self.system_stats['lastBlockStartTime'])
                                if self.system_stats['avgBlockageDuration'] > 0:
                                    self.system_stats['avgBlockageDuration'] \
                                        = int((blockage_duration + self.system_stats['avgBlockageDuration']) / 2)
                                else:
                                    self.system_stats['avgBlockageDuration'] = blockage_duration
                            self.sat_status['Registered'] = True
                            self.sat_status['Blocked'] = False
                            self.sat_status['BBWait'] = False
                            if self.system_stats['lastRegStartTime'] > 0:
                                registration_duration = int(time.time() - self.system_stats['lastRegStartTime'])
                            else:
                                registration_duration = 0
                            if self.system_stats['avgRegistrationDuration'] > 0:
                                self.system_stats['avgRegistrationDuration'] \
                                    = int((registration_duration + self.system_stats['avgRegistrationDuration']) / 2)
                            else:
                                self.system_stats['avgRegistrationDuration'] = registration_duration
                        elif new_sat_ctrl_state == 'Blocked':
                            self.sat_status['Blocked'] = True
                            self.system_stats['lastBlockStartTime'] = time.time()
                            log.info("Blockage started")

                        # Other transitions for statistics tracking:
                        if old_sat_ctrl_state == 'Waiting for GNSS fix' \
                                and new_sat_ctrl_state != 'Stopped' and new_sat_ctrl_state != 'Blocked':
                            gnss_duration = int(time.time() - self.system_stats['lastGNSSStartTime'])
                            log.info("GNSS acquired in " + str(gnss_duration) + " seconds")
                            if self.system_stats['avgGNSSFixDuration'] > 0:
                                self.system_stats['avgGNSSFixDuration'] \
                                    = int((gnss_duration + self.system_stats['avgGNSSFixDuration']) / 2)
                            else:
                                self.system_stats['avgGNSSFixDuration'] = gnss_duration
                        if old_sat_ctrl_state == 'Downloading Bulletin Board' \
                                and new_sat_ctrl_state != 'Stopped' and new_sat_ctrl_state != 'Blocked':
                            bulletin_duration = int(time.time() - self.system_stats['lastBBStartTime'])
                            log.info("Bulletin Board downloaded in: " + str(bulletin_duration) + " seconds")
                            if self.system_stats['avgBBReacquireDuration'] > 0:
                                self.system_stats['avgBBReacquireDuration'] \
                                    = int((bulletin_duration + self.system_stats['avgBBReacquireDuration']) / 2)
                            else:
                                self.system_stats['avgBBReacquireDuration'] = bulletin_duration
                        if old_sat_ctrl_state == 'Active' \
                                and new_sat_ctrl_state != 'Stopped' and new_sat_ctrl_state != 'Blocked':
                            self.system_stats['lastRegStartTime'] = int(time.time())
                            self.system_stats['nRegistration'] += 1

                    c_n0 = int(response['response'][1]) / 100.0
                    if self.system_stats['avgCN0'] == 0:
                        self.system_stats['avgCN0'] = c_n0
                    else:
                        self.system_stats['avgCN0'] = round((self.system_stats['avgCN0'] + c_n0) / 2.0, 2)
                else:
                    log.error("Bad response to satellite status query (" + err_str + ")")
            else:
                log.warning("Timeout occurred on satellite status query")

        return {'success': success, 'changed': changed, 'state': self.sat_status['CtrlState']}

    def at_check_mt_messages(self):
        """
        Checks for Mobile-Terminated messages in modem queue and retrieves if present.
        Logs a record of the receipt, and handles supported messages.

        :returns:
           - Boolean True if message(s) have been received/completed and ready for retrieval
           - ``list`` of ``dictionary`` messages consisting of
              - ``name`` used for retrieval
              - ``priority`` 0 for mobile-terminated messages
              - ``num`` number assigned by modem
              - ``sin`` Service Identifier Number (decimal 0..255)
              - ``state`` where 2 = complete and ready to retrieve
              - ``size`` including SIN and MIN bytes

        """
        log = self.log
        messages = []
        with self.thread_lock:
            log.debug("Checking for Mobile-Terminated messages")

            response = self.at_get_response('AT%MGFN')

            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    for res in response['response']:
                        msg_pending = res.replace('%MGFN:', '').strip()
                        if msg_pending:
                            msg = {}
                            msg_parms = msg_pending.split(',')
                            msg['name'] = msg_parms[0]
                            msg['num'] = msg_parms[1]
                            msg['priority'] = int(msg_parms[2])
                            msg['sin'] = int(msg_parms[3])
                            msg['state'] = int(msg_parms[4])
                            msg['size'] = int(msg_parms[5])
                            if msg['state'] == 2:  # Complete and not read
                                messages.append(msg)
                            else:
                                log.debug("Message %s not complete" % msg['name'])
                else:
                    log.error("Could not get new MT message info (%s)" % err_str)
            else:
                log.warning("Timeout occurred on MT message query")

        return True if len(messages) > 0 else False, messages

    def at_get_mt_message(self, msg_name, msg_sin, msg_size, data_type=2):
        """
        Retrieves a pending completed mobile-terminated message.

        :param msg_name: to be retrieved
        :param msg_sin: to be retrieved
        :param msg_size: to be retrieved
        :param data_type: 1 = Text, 2 = Hex, 3 = base64
        :returns:
           - Boolean success
           - ``dictionary`` message consisting of:
              - ``sin`` Service Identifier Number
              - ``min`` Message Identifier Number
              - ``payload`` including MIN byte, structure depends on data_type
              - ``size`` total in bytes including SIN, MIN

        """
        log = self.log
        msg_retrieved = False
        message = {}
        if 1 <= data_type <= 3:
            with self.thread_lock:
                response = self.at_get_response('AT%MGFG=' + msg_name + "," + str(data_type))
                if not response['timeout']:
                    err_code, err_str = self.at_get_result_code(response['result'])
                    if err_code == 0:
                        msg_retrieved = True
                        self.mt_msg_count += 1
                        message['sin'] = msg_sin
                        msg_envelope = response['response'][0].replace('%MGFG:', '').strip().split(',')
                        msg_content = msg_envelope[7]
                        msg_content_str = ''
                        if data_type == 1:
                            message['min'] = int(msg_content.replace('"', '')[1:3], 16)
                            msg_content_str = msg_content.replace('"', '')[3:]
                            message['payload'] = msg_content_str
                            message['size'] = len(msg_content_str) + 2
                        elif data_type == 2:
                            message['min'] = int(msg_content[0:2], 16)
                            msg_content_str = '0x' + str(msg_content)
                            message['payload'] = bytearray(msg_content.decode("hex"))
                            message['size'] = len(message['payload']) + 1
                        elif data_type == 3:
                            msg_content_bytes = bytearray(binascii.a2b_base64(msg_content))
                            message['min'] = int(msg_content_bytes[0])
                            msg_content_str = str(msg_content)
                            message['payload'] = msg_content_bytes
                            message['size'] = len(msg_content_bytes) + 1
                        log.info("Mobile Terminated %d-byte message received (SIN=%d MIN=%d) rawpayload:%s"
                                 % (message['size'], message['sin'], message['min'], msg_content_str))
                        if message['size'] != msg_size:
                            log.warning("Error calculating message size (got %d expected %d)"
                                        % (message['size'], msg_size))
                        if self.system_stats['avgMTMsgSize'] == 0:
                            self.system_stats['avgMTMsgSize'] = message['size']
                        else:
                            self.system_stats['avgMTMsgSize'] \
                                = int((self.system_stats['avgMTMsgSize'] + message['size']) / 2)
                    else:
                        log.error("Could not get MT message (%s)" % err_str)
        else:
            log.error("Invalid data_type passed (%d)" % data_type)

        return msg_retrieved, message

    def at_send_message(self, data_string, data_format=1, msg_sin=128, msg_min=1, priority=4):
        """
        Transmits a Mobile-Originated message. If ASCII-Hex format is used, 0-pads to nearest byte boundary.

        :param data_string: data to be transmitted
        :param data_format: 1=Text (default), 2=ASCII-Hex, 3=base64
        :param msg_sin: first byte of message (default 128 "user")
        :param msg_min: second byte of message (default 1 "user")
        :param priority: 1(high) through 4(low, default)
        :return: Boolean result

        """
        log = self.log
        self.mo_msg_count += 1
        mo_msg_name = str(int(time.time()))[:8]
        mo_msg_priority = priority
        mo_msg_sin = msg_sin
        mo_msg_min = msg_min
        mo_msg_format = data_format
        msg_complete = False
        if data_format == 1:
            mo_msg_content = '"' + data_string + '"'
        else:
            mo_msg_content = data_string
            if data_format == 2 and len(data_string) % 2 > 0:
                mo_msg_content += '0'  # insert 0 padding to byte boundary
        with self.thread_lock:
            response = self.at_get_response(
                'AT%MGRT="' + mo_msg_name + '",' + str(mo_msg_priority) + ',' + str(mo_msg_sin) + '.' + str(
                    mo_msg_min) + ',' + str(mo_msg_format) + ',' + mo_msg_content)
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                mo_submit_time = time.time()
                if err_code == 0:
                    status_poll_count = 0
                    while not msg_complete:
                        time.sleep(1)
                        status_poll_count += 1
                        log.debug("MGRS queries: %d" % status_poll_count)
                        response1 = self.at_get_response('AT%MGRS="' + mo_msg_name + '"')
                        if not response1['timeout']:
                            err_code1, err_str1 = self.at_get_result_code(response['result'])
                            if err_code1 == 0:
                                res_param = response1['response'][0].split(',')
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
                                        log.info("MO message SIN=%d MIN=%d (%d bytes) completed in %d seconds"
                                                 % (mo_msg_sin, mo_msg_min, res_size, msg_latency))
                                        if self.system_stats['avgMOMsgSize'] == 0:
                                            self.system_stats['avgMOMsgSize'] = res_size
                                        else:
                                            self.system_stats['avgMOMsgSize'] = int(
                                                (self.system_stats['avgMOMsgSize'] + res_size) / 2)
                                        if self.system_stats['avgMOMsgLatency_s'] == 0:
                                            self.system_stats['avgMOMsgLatency_s'] = msg_latency
                                        else:
                                            self.system_stats['avgMOMsgLatency_s'] = int(
                                                (self.system_stats['avgMOMsgLatency_s'] + msg_latency) / 2)
                                    else:
                                        log.info("MO message (%d bytes) failed after %d seconds"
                                                 % (res_size, int(time.time() - mo_submit_time)))
                            elif err_code == 109:
                                log.debug("Message complete, Unavailable")
                                break
                            else:
                                log.error("Error getting message state (%s)" % err_str1)
                        else:
                            log.error("Message status check timed out")
                            break
                else:
                    log.error("Message submit error (%s)" % err_str)
            else:
                log.warning("Timeout attempting to submit MO message")

        return msg_complete

    def at_get_nmea(self, rmc=True, gga=True, gsa=True, gsv=True, refresh=0):
        """
        Queries GNSS NMEA strings from the modem and returns an array.

        :param gga: essential fix data
        :param rmc: recommended minimum
        :param gsa: dilution of precision (DOP) and satellites
        :param gsv: satellites in view
        :param refresh: the update rate being used, in seconds
        :returns:
           - Boolean success
           - ``list`` of NMEA sentences requested
        """
        log = self.log

        MIN_STALE_SECS = 1
        MAX_STALE_SECS = 600
        MIN_WAIT_SECS = 1
        MAX_WAIT_SECS = 600

        # TODO: Enable or disable AT%TRK tracking mode based on update interval, to improve fix times
        stale_secs = min(MAX_STALE_SECS, max(MIN_STALE_SECS, int(refresh / 2)))
        wait_secs = min(MAX_WAIT_SECS, max(MIN_WAIT_SECS, int(max(45, stale_secs - 1))))
        # example sentence string: '"GGA","RMC","GSA","GSV"'
        s_list = []
        if gga:
            s_list.append('"GGA"')
        if rmc:
            s_list.append('"RMC"')
        if gsa and not self.mobile_id == '00000000SKYEE3D':
            s_list.append('"GSA"')
        if gsv:
            s_list.append('"GSV"')
        req_sentences = ",".join(tuple(s_list))
        resp_sentences = []
        success = False
        timeout = False
        with self.thread_lock:
            log.debug("requesting location to send")
            self.gnss_stats['nGNSS'] += 1
            self.gnss_stats['lastGNSSReqTime'] = int(time.time())
            response = self.at_get_response('AT%GPS=' + str(stale_secs) + ',' + str(wait_secs) + ',' + req_sentences,
                                            at_timeout=wait_secs + 5)
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    gnss_fix_duration = int(time.time()) - self.gnss_stats['lastGNSSReqTime']
                    log.debug("GNSS response time [s]: " + str(gnss_fix_duration))
                    if self.gnss_stats['avgGNSSFixDuration'] > 0:
                        self.gnss_stats['avgGNSSFixDuration'] = int((gnss_fix_duration +
                                                                     self.gnss_stats['avgGNSSFixDuration']) / 2)
                    else:
                        self.gnss_stats['avgGNSSFixDuration'] = gnss_fix_duration
                    for res in response['response']:
                        if res == response['response'][0]:
                            res = res.replace('%GPS:', '').strip()
                        if res.startswith('$GP') or res.startswith('$GL'):  # TODO: Galileo/Beidou?
                            resp_sentences.append(res)
                elif err_code == 108:
                    log.warning("ERROR 108 - Timed out GNSS query")
                    self.gnss_stats['timeouts'] += 1
                else:
                    log.error("Unable to get GNSS (%s)" % err_str)
            else:
                log.warning("Timeout occurred on GNSS query")
                timeout = True

        return success, resp_sentences

    def at_save_config(self):
        """Store the current configuration including all S registers."""
        log = self.log
        success = False
        with self.thread_lock:
            response = self.at_get_response('AT&W')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                else:
                    log.error("Save configuration failed (%s)" % err_str)
        return success

    def at_get_event_notify_control_bitmap(self):
        """
        Returns the Event Notification Control (S88) bitmap as an integer value,
        and updates the event_notifications attribute of the Modem instance.

        Event Notifications:

           - ``newGnssFix`` (bit 0) the modem has acquired new time/position from GNSS (e.g. GPS, GLONASS)
           - ``newMtMsg`` (bit 1) a new Mobile-Terminated (aka Forward) message has been received over-the-air
           - ``moMsgComplete`` (bit 2) a Mobile-Originated (aka Return) message has completed sending over-the-air
           - ``modemRegistered`` (bit 3) the modem has registered on the satellite network
           - ``modemReset`` (bit 4) the modem has just reset
           - ``jamCutState`` (bit 5) the modem has detected antenna cut or GNSS signal jamming
           - ``modemResetPending`` (bit 6) a modem reset has been requested (typically received over-the-air)
           - ``lowPowerChange`` (bit 7) the modem's low power wakeup interval has been changed
           - ``utcUpdate`` (bit 8) the modem has received a system time update/correction
           - ``fixTimeout`` (bit 9) the latest GNSS location request has timed out (unable to acquire GNSS/location)
           - ``eventCached`` (bit 10) a requested event has been cached for retrieval using capture trace S-registers

        :return: (integer) event notification bitmap (e.g. 139 = newGnssFix, newMtMsg, modemRegistered, lowPowerChange)

        """
        log = self.log
        binary = '0b'
        for event in reversed(self.event_notifications):
            binary += '1' if self.event_notifications[event] else '0'
        value = int(binary, 2)
        with self.thread_lock:
            response = self.at_get_response('ATS88?')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    register_value = int(response['response'][0])
                    if value != register_value:
                        log.warning("S88 register value mismatch")
                        self._set_event_notify_bitmap_proxy(register_value)
                else:
                    log.error("Error querying S88")
        return register_value

    def get_event_notification_control(self):
        """
        Updates the ``event_notifications`` attribute by calling
        :py:func:`at_get_event_notify_control_bitmap <at_get_event_notify_control_bitmap>`.

        :return: An ``OrderedDict`` corresponding to the ``event_notifications`` attribute.

        """
        self.at_get_event_notify_control_bitmap()   # Does not use the integer value directly
        return self.event_notifications

    def _set_event_notify_bitmap_proxy(self, value):
        """
        Sets the proxy bitmap values for event notification in the modem object.

        :param value: the event bitmap (integer)

        """
        event_notify_bitmap = bin(value)[2:]
        if len(event_notify_bitmap) > len(self.event_notifications):
            event_notify_bitmap = event_notify_bitmap[:len(self.event_notifications) - 1]
        while len(event_notify_bitmap) < len(self.event_notifications):  # pad leading zeros
            event_notify_bitmap = '0' + event_notify_bitmap
        i = 0
        for key in reversed(self.event_notifications):
            self.event_notifications[key] = True if event_notify_bitmap[i] == '1' else False
            i += 1

    def at_set_event_notification_control_bitmap(self, value, save=False):
        """
        Sets the :py:func:`event notifications bitmap <at_get_event_notify_control_bitmap>` using an integer mask.
        Truncates the bitmap if too large.

        :param value: integer to set the S88 register bitmap
        :param save: Boolean whether to write to Non-Volatile Memory
        :return: Boolean result

        """
        log = self.log
        success = False
        with self.thread_lock:
            response = self.at_get_response('ATS88=' + str(value))
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    self._set_event_notify_bitmap_proxy(value)
                    if save:
                        self.at_save_config()
                else:
                    log.error("Write %d to S88 failed (%s)" % (value, err_str))

        return success

    def set_event_notification_control(self, key, value, save=False):
        """
        Sets a particular event monitoring status in the
        :py:func:`event notification bitmap <at_get_event_notify_control_bitmap>`.

        :param key: event name as defined in the ``OrderedDict``
        :param value: Boolean to set/clear the bit
        :param save: Boolean to store the configuration to Non-Volatile Memory
        :return: Boolean success

        """
        log = self.log
        success = False
        if isinstance(value, bool):
            if key in self.event_notifications:
                binary = '0b'
                register_bitmap = self.at_get_event_notify_control_bitmap()
                for event in reversed(self.event_notifications):
                    bit = '1' if self.event_notifications[event] else '0'
                    if key == event:
                        if self.event_notifications[event] != value:
                            bit = '1' if value else '0'
                    binary += bit
                new_bitmap = int(binary, 2)
                if new_bitmap != register_bitmap:
                    success = self.at_set_event_notification_control_bitmap(value=new_bitmap, save=save)
                    if success:
                        log.info("%s event notification %s" % (key, "enabled" if value else "disabled"))
                else:
                    log.debug("No change to %s event notification (%s)" % (key, "enabled" if value else "disabled"))
                    success = True
            else:
                log.error("Event %s not defined" % key)
        else:
            log.error("Value not Boolean")

        return success

    def at_get_event_notify_assert_bitmap(self):
        """
        Returns the Event Notification Assert Status (S89) bitmap as an integer value.
        See :py:func:`event notification bitmap <at_get_event_notify_control_bitmap>`

        .. note::
           This operation clears the S89 register upon reading.

        :return: (integer) event notification assert status (e.g. 2 = new Mobile Terminated Message received)

        """
        log = self.log
        with self.thread_lock:
            response = self.at_get_response('ATS89?')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    register_value = int(response['response'][0])
                else:
                    log.error("Error querying S89")
        return register_value

    def get_event_notification_assertions(self):
        """
        Returns a ``dictionary`` with the events that triggered the notification assert pin, by calling
        :py:func:`at_get_event_notify_assert_bitmap <at_get_event_notify_assert_bitmap>`.

        .. note::
           This function is meant to be called only after the physical modem has asserted its notification output.
           Calling this function clears all status conditions upon reading.

        .. todo::
           Testing incomplete.

        :return: an ``OrderedDict`` with Boolean values against event keys

           - ``newGnssFix`` the modem has acquired new time/position from GNSS (e.g. GPS, GLONASS)
           - ``newMtMsg`` a new Mobile-Terminated (aka Forward) message has been received over-the-air
           - ``moMsgComplete`` a Mobile-Originated (aka Return) message has completed sending over-the-air
           - ``modemRegistered`` the modem has registered on the satellite network
           - ``modemReset`` the modem has just reset
           - ``jamCutState`` the modem has detected antenna cut or GNSS signal jamming
           - ``modemResetPending`` a modem reset has been requested (typically received over-the-air)
           - ``lowPowerChange`` the modem's low power wakeup interval has been changed
           - ``utcUpdate`` the modem has received a system time update/correction
           - ``fixTimeout`` the latest GNSS location request has timed out (unable to acquire GNSS/location)
           - ``eventCached`` a requested event has been cached for retrieval using capture trace S-registers

        """
        register_value = self.at_get_event_notify_assert_bitmap()
        event_dict = self.event_notifications
        format_str = '{0:' + str(len(event_dict)) + 'b}'
        bitmap = format_str.format(register_value)
        index = 0
        for event in reversed(event_dict):
            event_dict[event] = True if bitmap[index] == '1' else False
            index += 1
        return event_dict

    def _at_sreg_write(self, register, value, save=False):
        """
        Writes a pre-validated value to an s-register.

        :param register: string value e.g. 'S50'
        :param value: (integer) to write
        :param save: (Boolean) store to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        with self.thread_lock:
            response = self.at_get_response('AT' + register + '=' + str(value))
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    if save:
                        self.at_save_config()
                else:
                    log.error("Write %d to %s failed (%s)" % (value, register, err_str))
        return success

    def _at_sreg_read(self, register):
        """
        Writes a pre-validated value to an s-register.

        :param register: string value e.g. 'S50'
        :return: integer value held in the requested S-register

        """
        log = self.log
        value = None
        with self.thread_lock:
            response = self.at_get_response('AT' + register + '?')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0 and len(response['response']) > 0:
                    value = int(response['response'][0])
                else:
                    log.error("Read %s failed (%s)" % (register, err_str))
        return value

    def set_wakeup_interval(self, value, save=False):
        """
        Sets the wakeup interval (S51, default 0).

        .. _wakeup-interval:

        ``wakeup_interval`` is an enumerated type:

           - ``5 seconds``: 0
           - ``30 seconds``: 1
           - ``1 minute``: 2
           - ``3 minute``: 3
           - ``10 minute``: 4
           - ``30 minute``: 5
           - ``60 minute``: 6
           - ``2 minute``: 7
           - ``5 minute``: 8
           - ``15 minute``: 9
           - ``20 minute``: 10

        :param value: an enumerated type
        :param save: if writing to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        if value in self.wakeup_intervals:
            success = self._at_sreg_write(register='S51', value=value, save=save)
            if success:
                self.wakeup_interval = self.wakeup_intervals[value]
        else:
            log.error("Invalid value %s for S51 wakeup interval" % value)
        return success

    def get_wakeup_interval(self):
        """Gets the wakeup interval (S51, default 0) and stores in the Modem instance."""
        log = self.log
        value = self._at_sreg_read(register='S51')
        if value is not None:
            self.wakeup_interval = value
        else:
            log.error("Error reading S51 wakeup interval")

    def set_power_mode(self, value, save=False):
        """
        Sets the power mode (S50, default 0).  ``power_mode`` is an enumerated type with:

           - ``Mobile Powered``: 0
           - ``Fixed Powered``: 1
           - ``Mobile Battery``: 2
           - ``Fixed Battery``: 3
           - ``Mobile Minimal``: 4
           - ``Mobile Stationary``: 5

        :param value: an enumerated type
        :param save: if writing to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        if value in self.power_modes:
            success = self._at_sreg_write(register='S50', value=value, save=save)
            if success:
                self.power_mode = self.power_modes[value]
        else:
            log.error("Invalid value %s for S50 power mode" % value)
        return success

    def get_power_mode(self):
        """Gets the power mode (S50, default 0) and stores in the Modem instance."""
        log = self.log
        value = self._at_sreg_read(register='S50')
        if value is not None:
            self.power_mode = value
        else:
            log.error("Error reading S50 power mode")

    def set_gnss_continuous(self, value, save=False):
        """
        Sets the GNSS continuous mode refresh interval (S55, default 0).

        :param value: (integer) seconds between refresh from 0..30.  0=disabled/on-demand.
        :param save: if writing to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        if 0 <= value <= 30 and isinstance(value, int):
            success = self._at_sreg_write(register='S55', value=value, save=save)
            if success:
                self.gnss_continuous = value
        else:
            log.error("Invalid value %s for S55 GNSS continuous mode" % value)
        return success

    def get_gnss_continuous(self):
        """Gets the GNSS continuous interval (S55, default 0) and stores in the Modem instance."""
        log = self.log
        value = self._at_sreg_read(register='S55')
        if value is not None:
            self.gnss_continuous = value
        else:
            log.error("Error reading S55 gnss continuous")

    def set_gnss_mode(self, value, save=False):
        """
        Sets the GNSS mode (S39) (GPS, GLONASS, Beidou).  ``gnss_mode`` is an enumerated type:

           - ``GPS``: 0
           - ``GLONASS``: 1
           - ``BEIDOU``: 2
           - ``GPS+GLONASS``: 10
           - ``GPS+BEIDOU``: 11
           - ``GLONASS+BEIDOU``: 12

        .. note::
           Check with modem manufacturer to confirm available GNSS settings on your hardware variant.

        :param value: an enumerated type
        :param save: if writing to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        if value in self.gnss_modes:
            success = self._at_sreg_write(register='S39', value=value, save=save)
            if success:
                self.gnss_mode = self.gnss_modes[value]
        else:
            log.error("Invalid value %s for S39 GNSS mode" % value)
        return success

    def get_gnss_mode(self):
        """Gets the GNSS mode (S39, default 0) and stores in the Modem instance."""
        log = self.log
        value = self._at_sreg_read(register='S39')
        if value is not None:
            self.gnss_mode = value
        else:
            log.error("Error reading S39 GNSS mode")

    def at_set_gnss_dpm(self, value, save=False):
        """
        Sets the GNSS Dynamic Platform model (S33, default 0).  ``gnss_dpm_mode`` is an enumerated type:

           - ``Portable``: 0
           - ``Stationary``: 2
           - ``Pedestrian``: 3
           - ``Automotive``: 4
           - ``Sea``: 5
           - ``Air 1g``: 6
           - ``Air 2g``: 7
           - ``Air 4g``: 8

        .. note::
           Check with modem manufacturer to confirm available GNSS settings on your hardware variant.

        :param value: an enumerated type
        :param save: if writing to NVM
        :return: Boolean success

        """
        log = self.log
        success = False
        if value in self.gnss_dpm_modes:
            success = self._at_sreg_write(register='S33', value=value, save=save)
            if success:
                self.gnss_dpm_mode = self.gnss_dpm_modes[value]
        else:
            log.error("Invalid value %s for S33 GNSS DPM" % value)
        return success

    def get_gnss_dpm(self):
        """Gets the GNSS Dynamic Platform Model (S33, default 0) and stores in Modem instance."""
        log = self.log
        value = self._at_sreg_read(register='S33')
        if value is not None:
            self.gnss_dpm_mode = value
        else:
            log.error("Error reading S33 GNSS DPM mode")

    def log_at_config(self):
        """Logs/displays the current AT configuration options (e.g. CRC, Verbose, Echo, Quiet) on the console."""
        self.log.info("*** Modem AT Configuration ***")
        for k in self.at_config:
            self.log.info("*  %s=%d" % (k, 1 if self.at_config[k] else 0))

    def log_sat_status(self):
        """Logs/displays the current satellite status on the console."""
        self.log.info("*** Satellite Status ***")
        for stat in self.sat_status:
            self.log.info("*  %s: %s" % (stat, str(self.sat_status[stat])))

    def get_statistics(self):
        """
        Returns a ``dictionary`` of operating statistics for the modem/network.

        :return: ``dictionary`` of strings and KPI values containing key statistics

        """
        stat_list = [
            ('GNSS control (network) fixes', self.system_stats['nGNSS']),
            ('Average GNSS (network) time to fix [s]', self.system_stats['avgGNSSFixDuration']),
            ('Registrations', self.system_stats['nRegistration']),
            ('Average Registration time [s]', self.system_stats['avgRegistrationDuration']),
            ('BB acquisitions', self.system_stats['nBBAcquisition']),
            ('Average BB acquisition time [s]', self.system_stats['avgBBReacquireDuration']),
            ('Blockages', self.system_stats['nBlockage']),
            ('Average Blockage duration [s]', self.system_stats['avgBlockageDuration']),
            ('GNSS application fixes', self.gnss_stats['nGNSS']),
            ('Average GNSS (application) time to fix [s]', self.gnss_stats['avgGNSSFixDuration']),
            ('GNSS request timeouts', self.gnss_stats['timeouts']),
            ('Average AT response time [ms]', self.system_stats['avgATResponseTime_ms']),
            ('Total AT non-responses', self.at_timeouts_total),
            ('Total Mobile-Originated messages', self.mo_msg_count),
            ('Average Mobile-Originated message size [bytes]', self.system_stats['avgMOMsgSize']),
            ('Average Mobile-Originated message latency [s]', self.system_stats['avgMOMsgLatency_s']),
            ('Total Mobile-Terminated messages', self.mt_msg_count),
            ('Average Mobile-Terminated message size [bytes]', self.system_stats['avgMTMsgSize']),
            ('Average C/N0 [dB]', self.system_stats['avgCN0'])
        ]
        return stat_list

    def log_statistics(self):
        """Logs the modem/network statistics."""
        self.log.info("*" * 26 + " IDP MODEM STATISTICS " + "*" * 26)
        self.log.info("* Mobile ID: %s" % self.mobile_id)
        self.log.info("* Hardware version: %s" % self.hardware_version)
        self.log.info("* Firmware version: %s" % self.software_version)
        self.log.info("* AT version: %s" % self.at_version)
        for stat in self.get_statistics():
            self.log.info("* %s: %s" % (stat[0], str(stat[1])))
        self.log.info("*" * 75)


class Message(object):
    """Class intended for abstracting message characteristics."""
    # TODO: future use
    
    class Priority:
        HIGH = 1
        MIDH = 2
        MIDL = 3
        LOW = 4

        def __init__(self):
            pass

    class DataFormat:
        TEXT = 1
        HEX = 2
        BASE64 = 3

        def __init__(self):
            pass

    data_types = ['bool', 'int_8', 'uint_8', 'int_16', 'uint_16', 'int_32', 'uint_32', 'int_64', 'uint_64',
                  'float', 'double', 'string']

    def __init__(self, name=None, msg_sin=255, msg_min=255, payload_b64=None):
        """
        Initialize a message.  Messages also have ``size`` determined, and can optionally include ``fields``.

        :param name: (optional)
        :param msg_sin: integer (0..255)
        :param msg_min: integer (0..255)
        :param priority: (1=high, 4=low)
        :param payload_b64: base64 encoded payload (not including SIN, MIN)

        """
        self.priorities = self.Priority()
        self.data_formats = self.DataFormat()
        self.name = name
        self.sin = msg_sin
        self.min = msg_min
        self.payload_b64 = payload_b64
        self.fields = []
        # self.use_min_as_field = False     # TODO: future feature
        self.size = 0

    def _get_size(self):
        """Updates the message size attribute."""
        if self.sin is not None:
            self.size = 1
        if self.payload_b64 != '':
            if self.data_format == 1:
                if self.min is None:
                    self.min = ord(self.payload_b64[1:1])
                else:
                    self.size += 1
                self.size += len(self.payload_b64)
            elif self.data_format == 2:
                if self.min is None:
                    self.min = self.payload_b64[1:2]
                else:
                    self.size += 1
                self.size += int(len(self.payload_b64) / 2)
            elif self.data_format == 3:
                if self.min is None:
                    self.min = ord(base64.b64decode(self.payload_b64)[1:1])
                else:
                    self.size += 1
                self.size += len(base64.b64decode(self.payload_b64))

    def add_field(self, name, data_type, value, bit_size):
        """
        Add a field to the message.

        :param name: (string)
        :param data_type: (string) from supported types
        :param value: the value (compliant with data_type)
        :param bit_size: string formatter '0nb' where n is number of bits
        :return:
           - error code
           - error string

        """
        # TODO: make it so fields cannot be added/deleted/modified without explicit class methods
        field = {}
        if isinstance(name, str):
            field['name'] = name
            if data_type in self.data_types:
                field['data_type'] = data_type
                if data_type == 'bool' and isinstance(value, bool) \
                        or 'int' in data_type and isinstance(value, int) \
                        or data_type == 'string' and isinstance(value, str) \
                        or (data_type == 'float' or data_type == 'double') and isinstance(value, float):

                    field['value'] = value
                    if bit_size[0] == '0' and bit_size[len(bit_size) - 1] == 'b':
                        # TODO: some risk that value range may not fit in bit_size
                        if bit_size[1:len(bit_size) - 1] > 0:
                            err_code = 0
                            err_str = 'OK'
                            field['bit_size'] = bit_size
                            self.fields.append(field)
                        else:
                            err_code = 5
                            err_str = "Value exceeds specified number of bits"
                    else:
                        err_code = 4
                        err_str = "Invalid bit_size definition"
                else:
                    err_code = 3
                    err_str = "Value type does not match data type"
            else:
                err_code = 2
                err_str = "Invalid data type"
        else:
            err_code = 1
            err_str = "Invalid name of field (not string)"
        return err_code, err_str

    def delete_field(self, name):
        """
        Remove a field from the message.

        :param name: of field (string)
        :returns:
           - error code (0 = no error)
           - error string description (0 = "OK")

        """
        err_code = 1
        err_str = "Field not found in message"
        for i, field in enumerate(self.fields):
            if field['name'] == name:
                err_code = 0
                err_str = "OK"
                del self.fields[i]
        return err_code, err_str

    def encode_idp(self, data_format=2):
        """
        Encodes the message using the specified data format (Text, Hex, base64).

        :param data_format: 1=Text, 2=ASCII-Hex, 3=base64
        :returns: encoded_payload (string) to pass into AT%MGRT

        """
        encoded_payload = ''
        bin_str = ''
        for field in self.fields:
            name = field['name']
            data_type = field['data_type']
            value = field['value']
            bit_size = field['bit_size']
            bin_field = ''
            if 'int' in data_type and isinstance(value, int):
                if value < 0:
                    inv_bin_field = format(-value, bit_size)
                    comp_bin_field = ''
                    i = 0
                    while len(comp_bin_field) < len(inv_bin_field):
                        comp_bin_field += '1' if inv_bin_field[i] == '0' else '0'
                        i += 1
                    bin_field = format(int(comp_bin_field, 2) + 1, bit_size)
                else:
                    bin_field = format(value, bit_size)
            elif data_type == 'bool' and isinstance(value, bool):
                bin_field = '1' if value else '0'
            elif data_type == 'float' and isinstance(value, float):
                f = '{0:0%db}' % bit_size
                bin_field = f.format(int(hex(struct.unpack('!I', struct.pack('!f', value))[0]), 16))
            elif data_type == 'double' and isinstance(value, float):
                f = '{0:0%db}' % bit_size
                bin_field = f.format(int(hex(struct.unpack('!Q', struct.pack('!d', value))[0]), 16))
            elif data_type == 'string' and isinstance(value, str):
                bin_field = bin(int(''.join(format(ord(c), '02x') for c in value), 16))[2:]
                if len(bin_field) < bit_size:
                    # TODO: be careful on padding strings...this should pad with NULL
                    bin_field += ''.join('0' for pad in range(len(bin_field), bit_size))
            else:
                pass
                # TODO: handle other cases
                # raise
            bin_str += bin_field
        payload_pad_bits = len(bin_str) % 8
        while payload_pad_bits > 0:
            bin_str += '0'
            payload_pad_bits -= 1
        hex_str = ''
        index_byte = 0
        while len(hex_str) / 2 < len(bin_str) / 8:
            hex_str += format(int(bin_str[index_byte:index_byte + 8], 2), '02X').upper()
            index_byte += 8
        self.size = len(hex_str) / 2 + 2
        self.payload_b64 = hex_str.decode('hex').encode('base64')
        if data_format == 2:
            encoded_payload = hex_str
        elif data_format == 3:
            encoded_payload = self.payload_b64
        return encoded_payload

    '''
    def decode_idp_json(self):
        """
        Decodes the message received to JSON from the modem based on data format retrieved from IDP modem.
        For future use with Message Definition Files
        
        :return: JSON-formatted string
        
        """
        if self.size > 0:
            json_str = '{"name":%s,"SIN":%d,"MIN":%d,"size":%d,"Fields":[' \
                       % (str(self.name), self.sin, self.min, self.size)
            for i, field in enumerate(self.fields):
                json_str += '{"name":"%s","data_type":"%s","value":' \
                            % (field['name'], field['data_type'])
                if isinstance(field['value'], int):
                    json_str += '%d}' % field['value']
                elif isinstance(field['value'], float):
                    json_str += '%f}' % field['value']
                elif isinstance(field['value'], bool):
                    json_str += '%s}' % str(field['value']).lower()
                elif isinstance(field['value'], str):
                    json_str += '"%s"}' % field['value']
                json_str += ',' if i < len(self.fields) else ']'
            json_str += '}'
        else:
            json_str = ''
        return json_str
    '''


class MobileOriginatedMessage(Message):
    """
    Class containing Mobile Originated (aka Return) message properties.
    Initializes Mobile Originated (Return) Message with state = ``UNAVAILABLE``
    Return States enumeration (per modem documentation):

       - ``UNAVAILABLE``: 0
       - ``READY``: 4
       -  ``SENDING``: 5
       - ``COMPLETE``: 6
       - ``FAILED``: 7

    :param name: identifier for the message (tbd limitations)
    :param msg_sin: Service Identification Number (1st byte of payload)
    :param msg_min: Message Identification Number (2nd byte of payload)
    :param payload_b64: (optional) base64 encoded payload (not including SIN/MIN)

    """

    class State:
        # """State enumeration for Mobile Originated (aka Return) messages."""
        UNAVAILABLE = 0
        READY = 4
        SENDING = 5
        COMPLETE = 6
        FAILED = 7

        def __init__(self):
            pass

    def __init__(self, name=None, msg_sin=255, msg_min=255, payload_b64=None):
        """
        Initializes Mobile Originated (Return) Message with state = ``UNAVAILABLE``
        Return States enumeration (per modem documentation):

           - ``UNAVAILABLE``: 0
           - ``READY``: 4
           -  ``SENDING``: 5
           - ``COMPLETE``: 6
           - ``FAILED``: 7

        :param name: identifier for the message (tbd limitations)
        :param msg_sin: Service Identification Number (1st byte of payload)
        :param msg_min: Message Identification Number (2nd byte of payload)
        :param payload_b64: (optional) base64 encoded payload (not including SIN/MIN)

        """
        Message.__init__(self, name=name, msg_sin=msg_sin, msg_min=msg_min, payload_b64=payload_b64)
        self.states = self.State()
        self.state = self.states.UNAVAILABLE
            

class MobileTerminatedMessage(Message):
    """
    Class containing Mobile Terminated (aka Forward) message properties.
    Initializes Mobile Terminated (Forward) Message with state = ``UNAVAILABLE``
    Forward States enumeration (per modem documentation):

       - ``UNAVAILABLE``: 0
       - ``COMPLETE``: 2
       -  ``RETRIEVED``: 3

    :param name:
    :param msg_sin:
    :param msg_min:
    :param payload_b64:

    """

    class State:
        # """State enumeration for Mobile Terminated (aka Return) messages."""
        UNAVAILABLE = 0
        COMPLETE = 2
        RETRIEVED = 3

        def __init__(self):
            pass

    def __init__(self, name=None, msg_sin=255, msg_min=255, payload_b64=None):
        """
        Initializes Mobile Terminated (Forward) Message with state = ``UNAVAILABLE``
        Forward States enumeration (per modem documentation):

           - ``UNAVAILABLE``: 0
           - ``COMPLETE``: 2
           -  ``RETRIEVED``: 3

        :param name:
        :param msg_sin:
        :param msg_min:
        :param payload_b64:

        """
        Message.__init__(self, name=name, msg_sin=msg_sin, msg_min=msg_min, payload_b64=payload_b64)
        self.states = self.State()
        self.state = self.states.UNAVAILABLE
            

if __name__ == "__main__":
    modem = Modem(serial_port=None, debug=True)
    modem.log_at_config()
    modem.log_sat_status()
    modem.log_statistics()
