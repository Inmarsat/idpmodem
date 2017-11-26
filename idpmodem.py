"""
Data structure and operations for a SkyWave/ORBCOMM IDP modem using AT commands
"""

from collections import OrderedDict
import time
import crcxmodem
import threading
import binascii
import base64


class Modem(object):
    """Abstracts attributes and statistics related to an IDP modem"""

    ctrlStates = [
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

    atErrResultCodes = {
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

    wakeupIntervals = {
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
    
    def __init__(self, serial_port, log=None, debug=False):
        """Initializes attributes and pointers used by class methods
        :param  serial_port a pySerial.serial object
        :param  log an optional logger
        :param  debug Boolean option for verbose trace
        """
        self.mobile_id = ''
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
        self.wakeup_interval = 0
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
        self.mo_queue = []
        self.mt_queue = []
        self.hardware_version = '0'
        self.software_version = '0'
        self.at_version = '0'
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
        """ Returns the CRC-16-CCITT (initial value 0xFFFF) checksum
        :param at_cmd the AT command to calculate CRC on
        :return the CRC for the command
        """
        return '{:04X}'.format(crcxmodem.crc(at_cmd, 0xffff))

    def _update_stats_at_response(self, at_send_time, at_cmd):
        """ Updates the last and average AT command response time statistics
        :param at_send_time the reference time the AT command was sent
        :param at_cmd the command that was sent
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
        """ Takes a single AT command, applies CRC if enabled, sends to the modem and waits for response completion
          Parses the response, line by line, until a result code is received or at_timeout is exceeded
          Assumes Quiet mode is disabled, and will not pass 'Quiet enable' (ATQ1) to the modem
          Sets modem object properties (Echo, CRC, Verbose, Quiet) by inference from AT response
        :param  at_cmd       the AT command to send
        :param  at_timeout   the time in seconds to wait for a response
        :param  debug        optional verbose runtime trace
        :return a dictionary containing:
                echo        - the AT command sent (including CRC if applied) or empty string if Echo disabled
                response    - a list of (stripped) strings representing multi-line response
                result      - a string returned after the response when Quiet mode is disabled
                            'OK' or 'ERROR' if Verbose is enabled on the modem,
                            or a numeric error code that can be looked up in idpmodem.atErrorResultCodes
                checksum    - the CRC (if enabled) or None
                error       - Boolean if CRC is correct
                timeout     - Boolean if AT response timed out
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
        """ Queries the details of an error response on the AT command interface
        :param result_code: the value returned by the AT command response
        :returns: error_code - the specific error code
                 error_desc - the interpretation of the error code
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

        if str(error_code) in self.atErrResultCodes:
            error_desc = self.atErrResultCodes[str(error_code)]

        return error_code, error_desc

    def at_attach(self, at_timeout=1):
        """Attempts to attach using basic AT command
        :returns    Boolean success
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
        """ Initializes the modem after new connection. Restores saved defaults, disables Quiet mode,

        :param  use_crc  - optionally enables CRC on AT commands (e.g. if using long serial cable)
        :param  verbose  - optionally use verbose mode for results (OK/ERROR)
        :return Boolean success
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

        # Get event notification bitmap
        time.sleep(AT_WAIT)
        self.at_get_event_notify_bitmap()
        self.set_event_notification('modemRegistered', True)

        success = self.at_save_config()

        return success

    def at_check_sat_status(self):
        """Checks satellite status and updates state and statistics
        :returns    Dictionary with:
                    'success' Boolean
                    'changed' Boolean
                    'state' (string from ctrlStates)
        """
        log = self.log
        success = False
        changed = False
        with self.thread_lock:
            log.debug("Checking satellite status. Previous control state: " + self.sat_status['CtrlState'])

            # S122: satellite trace status
            # S116: C/N0
            response = self.at_get_response('ATS90=3 S91=1 S92=1 S122? S116?')

            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    old_sat_ctrl_state = self.sat_status['CtrlState']
                    new_sat_ctrl_state = self.ctrlStates[int(response['response'][0])]
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
        """ Checks for Mobile-Terminated messages in modem queue and retrieves if present.
         Logs a record of the receipt, and handles supported messages
         :returns   Boolean True if message(s) have been received/completed and ready for retrieval
                    list of dictionary messages consisting of
                        'name' used for retrieval
                        'priority' 0 for mobile-terminated messages
                        'num' number assigned by modem
                        'sin' Service Identifier Number (decimal)
                        'state' where 2 = complete and ready to retrieve
                        'size' including SIN and MIN bytes
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
        """Retrieves a pending completed mobile-terminated message
        :param      msg_name to be retrieved
        :param      msg_sin to be retrieved
        :param      msg_size to be retrieved
        :param      data_type 1 = Text, 2 = Hex, 3 = base64
        :returns    Boolean success
                    dictionary message consisting of:
                        'sin' Service Identifier Number
                        'min' Message Identifier Number
                        'payload' including MIN byte, structure depends on data_type
                        'size' total in bytes including SIN, MIN
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
        """ Transmits a Mobile-Originated message. If ASCII-Hex format is used, 0-pads to nearest byte boundary
        :param  data_string: data to be transmitted
        :param  data_format: 1=Text (default), 2=ASCII-Hex, 3=base64
        :param  msg_sin: first byte of message (default 128 "user")
        :param  msg_min: second byte of message (default 1 "user")
        :param  priority 1(high) through 4(low, default)
        :return Boolean result
        """
        log = self.log

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
        """ Queries GPS NMEA strings from the modem and returns an array.
        :param      gga essential fix data
        :param      rmc recommended minimum
        :param      gsa dilution of precision (DOP) and satellites
        :param      gsv satellites in view
        :param      refresh the update rate being used, in seconds
        :returns    Boolean success
                    array of NMEA sentences requested
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
        """Store the current configuration including all S registers"""
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

    def at_get_event_notify_bitmap(self):
        """Returns the event notification bitmap as an integer value
        :return integer event notification bitmap
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
        return value

    def _set_event_notify_bitmap_proxy(self, value):
        """Sets the proxy bitmap values in the modem object
        :param  value the event bitmap (integer)
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

    def at_set_event_notifications(self, value, save=False):
        """Sets the event notification bitmap using an integer mask. Truncates the bitmap if too large.
        :param  value integer to set the S88 register bitmap
        :param  save writes to NVM
        :return Boolean result
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

    def set_event_notification(self, key, value, save=False):
        """Sets a particular event monitoring status in the event notification bitmap
        :param  key event name as defined in the ordered dictionary
        :param  value Boolean to set/clear the bit
        :param  save the configuration to NVM
        :return Boolean success result
        """
        log = self.log
        success = False
        if isinstance(value, bool):
            if key in self.event_notifications:
                binary = '0b'
                register_bitmap = self.at_get_event_notify_bitmap()
                for event in reversed(self.event_notifications):
                    bit = '1' if self.event_notifications[event] else '0'
                    if key == event:
                        if self.event_notifications[event] != value:
                            bit = '1' if value else '0'
                    binary += bit
                new_bitmap = int(binary, 2)
                if new_bitmap != register_bitmap:
                    success = self.at_set_event_notifications(value=new_bitmap, save=save)
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

    def display_at_config(self):
        """Displays the current AT options on the console"""
        print('Modem AT Configuration: ' + self.mobile_id)
        print(' %CRC=' + str(int(self.at_config['CRC'])))
        print('  ATE=' + str(int(self.at_config['Echo'])))
        print('  ATV=' + str(int(self.at_config['Verbose'])))
        print('  ATQ=' + str(int(self.at_config['Quiet'])))

    def display_sat_status(self):
        """Displays the current satellite status on the console"""
        print('Satellite Status: ' + self.mobile_id)
        for stat in self.sat_status:
            print('  ' + stat + ": " + str(self.sat_status[stat]))

    def get_statistics(self):
        """Returns a dictionary of operating statistics for the modem/network
        :return list of strings containing key statistics
        """
        stat_list = {
            'GNSS control (network) fixes': str(self.system_stats['nGNSS']),
            'Average GNSS (network) time to fix [s]': str(self.system_stats['avgGNSSFixDuration']),
            'Registrations': str(self.system_stats['nRegistration']),
            'Average Registration time [s]': str(self.system_stats['avgRegistrationDuration']),
            'BB acquisitions': str(self.system_stats['nBBAcquisition']),
            'Average BB acquisition time [s]': str(self.system_stats['avgBBReacquireDuration']),
            'Blockages': str(self.system_stats['nBlockage']),
            'Average Blockage duration [s]': str(self.system_stats['avgBlockageDuration']),
            'GNSS application fixes': str(self.gnss_stats['nGNSS']),
            'Average GNSS (application) time to fix [s]': str(self.gnss_stats['avgGNSSFixDuration']),
            'GNSS request timeouts': str(self.gnss_stats['timeouts']),
            'Average AT response time [ms]': str(self.system_stats['avgATResponseTime_ms']),
            'Total AT non-responses': str(self.at_timeouts_total),
            'Average Mobile-Originated message size [bytes]': str(self.system_stats['avgMOMsgSize']),
            'Average Mobile-Originated message latency [s]': str(self.system_stats['avgMOMsgLatency_s']),
            'Average Mobile-Terminated message size [bytes]': str(self.system_stats['avgMTMsgSize']),
            'Average C/N0': str(self.system_stats['avgCN0'])
        }
        return stat_list

    def display_statistics(self):
        """Prints the modem/network statistics to the console"""
        print("*" * 30 + " MODEM STATISTICS " + "*" * 30)
        print("* Mobile ID: %s" % self.mobile_id)
        print("* Hardware version: %s" % self.hardware_version)
        print("* Firmware version: %s" % self.software_version)
        print("* AT version: %s" % self.at_version)
        stats_list = self.get_statistics()
        for stat in stats_list:
            print("* " + stat + ":" + str(stats_list[stat]))
        print("*" * 75)

    def log_statistics(self):
        """Logs the modem/network statistics"""
        log = self.log
        log.info("*" * 30 + " MODEM STATISTICS " + "*" * 30)
        log.info("* Mobile ID: %s" % self.mobile_id)
        log.info("* Hardware version: %s" % self.hardware_version)
        log.info("* Firmware version: %s" % self.software_version)
        log.info("* AT version: %s" % self.at_version)
        stats_list = self.get_statistics()
        for stat in stats_list:
            log.info("* " + stat + ":" + str(stats_list[stat]))
        log.info("*" * 75)


class Message(object):
    """ Class intended for abstracting message characteristics """
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

    def __init__(self, name=None, msg_sin=None, msg_min=None, priority=4, data_format=2, payload=''):
        """TODO"""
        self.priorities = self.Priority()
        self.data_formats = self.DataFormat()
        self.name = name
        self.sin = msg_sin
        self.min = msg_min
        self.priority = priority
        self.data_format = data_format
        self.payload = payload
        self.size = 0
        if self.sin is not None:
            self._get_size()

    def _get_size(self):
        if self.sin is not None:
            self.size = 1
        if self.payload != '':
            if self.data_format == 1:
                if self.min is None:
                    self.min = ord(self.payload[1:1])
                else:
                    self.size += 1
                self.size += len(self.payload)
            elif self.data_format == 2:
                if self.min is None:
                    self.min = self.payload[1:2]
                else:
                    self.size += 1
                self.size += int(len(self.payload)/2)
            elif self.data_format == 3:
                if self.min is None:
                    self.min = ord(base64.b64decode(self.payload)[1:1])
                else:
                    self.size += 1
                self.size += len(base64.b64decode(self.payload))


class MobileOriginated(Message):
    """Class containing Mobile Originated (aka Forward) message properties"""

    class State:
        UNAVAILABLE = 0
        READY = 4
        SENDING = 5
        COMPLETE = 6
        FAILED = 7

        def __init__(self):
            pass

    def __init__(self):
        Message.__init__(self)
        self.states = self.State()
        self.state = self.states.UNAVAILABLE
            

class MobileTerminated(Message):
    """Class containing Mobile Originated (aka Forward) message properties"""

    class State:
        UNAVAILABLE = 0
        COMPLETE = 2
        RETRIEVED = 3

        def __init__(self):
            pass

    def __init__(self):
        Message.__init__(self)
        self.states = self.State()
        self.state = self.states.UNAVAILABLE
            

if __name__ == "__main__":
    modem = Modem('DUMMY')
    modem.mobile_id = '00000000SKYEE3D'
    modem.display_at_config()
    modem.display_sat_status()
    modem.display_statistics()
