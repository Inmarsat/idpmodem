"""
Data structure and operations for a SkyWave/ORBCOMM IsatData Pro (**IDP**) modem using AT commands.

IDP is a store-and-forward satellite messaging technology with messages structured as:

.. table::

   +--------------------------------------------+------------------------------------------------+
   | Service Identifier Number (1 byte **SIN**) | Payload (up to 6399 bytes MO or 9999 bytes MT) |
   +--------------------------------------------+------------------------------------------------+

Where the first byte of Payload can optionally be defined as a Message Identifier Number (**MIN**)
to facilitate decoding.

* MO = **Mobile Originated** aka *Return* aka *From-Mobile* message sent from modem to cloud application
* MT = **Mobile Terminated** aka *Forward message* aka *To-Mobile* message sent from cloud application to modem

Upon power-up or reset, the modem first acquires its location using Global Navigation Satellite Systems (GNSS).
After getting its location, the modem tunes to the correct frequency, then registers on the network.
Prolonged obstruction of satellite signal will put the modem into a "blockage" state from which it will
automatically try to recover based on an algorithm influenced by its *power mode* setting.

.. todo::

   * Reference contextual documentation pages for things like event notifications, low power, etc.
   * Internalize _automation_threads for typical monitoring processes to callback to registered external functions
   * Handle unsolicited modem output.  Can this happen while awaiting AT response??

"""

from __future__ import absolute_import

import binascii
from collections import OrderedDict
from datetime import datetime
import sys
# from sys import _getframe
import threading
# from threading import enumerate
from time import time, sleep
from typing import Callable

try:
    import queue
except ImportError:
    import Queue as queue

import serial

try:
    from constants import *
    from at_protocol import ByteReaderThread, IdpModem, AtCrcConfigError, AtTimeout
    from utils import get_caller_name, get_wrapping_logger, is_logger
    from utils import RepeatingTimer
    from utils import validate_serial_port
    import crcxmodem
    from s_registers import SRegisters
    from message import MobileOriginatedMessage, MobileTerminatedMessage
    import nmea
except ImportError:    
    from idpmodem.constants import *
    from idpmodem.at_protocol import ByteReaderThread, IdpModem, AtCrcConfigError, AtTimeout
    from idpmodem.utils import get_caller_name, get_wrapping_logger, is_logger
    from idpmodem.utils import RepeatingTimer
    from idpmodem.utils import validate_serial_port
    import idpmodem.crcxmodem as crcxmodem
    from idpmodem.s_registers import SRegisters
    from idpmodem.message import MobileOriginatedMessage, MobileTerminatedMessage
    import idpmodem.nmea as nmea

__version__ = "1.0.0"


class IdpException(Exception):
    pass


class ConnectionStatistics(object):
    pass


class _Version(object):
    """Encapsulates the modem version information.
    
    Attributes:
        hardware (string): Hardware version M.m.p
        firmware (string): Firmware version M.m.p
        at_protocol (string): AT protocol version
    """
    def __init__(self):
        self.hardware = None
        self.firmware = None
        self.at_protocol = None


class _AtConfiguration(object):
    """Configuration of the AT command handler (echo, crc, verbose, quiet)"""

    def __init__(self):
        self.echo = True
        self.crc = False
        self.verbose = True
        self.quiet = False

    def reset_factory_defaults(self):
        """Resets the modem factory defaults"""
        self.echo = True
        self.crc = False
        self.verbose = True
        self.quiet = False


class _SatStatus(object):
    """Key operating parameters of the modem.
    
    Based on Satellite Control State in Trace Class 3, Subclass 1 Index 23
    
    Attributes:
        registered: Registered on satellite/beam
        blocked: Unable to see satellite
        rx_only: Receive-only
        bb_wait: Waiting on Bulletin Board
        ctrl_state: (str) defaults to 'Stopped'
    
    """

    def __init__(self):
        self.registered = False
        self.blocked = False
        self.rx_only = False
        self.bb_wait = False
        self.ctrl_state = 'Stopped'
        self.low_snr = True


class _Automation(object):
    def __init__(self):
        self.threads = []


class _PendingMoMessage(object):
    """Holds metadata and callback for pending Mobile-Originated message.

    A private class for managing a queue of Mobile-Originated messages submitted via AT command
    Ensures a unique name is assigned based on timestamp submitted

    Attributes:
        message: (MobileOriginatedMessage) to be queued
        q_name: (str) The name of the message used in the modem queue
        submit_time: (int) Timestamp of submission
        complete_time: (int) Estimated (due to polling) timestamp of completion
        failed: (bool)
        callback: (Callable) that will receive notification when the message completes/fails

    """

    def __init__(self, message: object, callback: Callable = None):
        self.message = message
        self.q_name = str(int(time()))[1:9]
        self.submit_time = time()
        self.complete_time = None
        self.failed = False
        self.callback = callback


class _PendingMtMessage(object):
    """Holds metadata and callback for pending Mobile-Terminated message.

    Attributes:
        message: (MobileTerminatedMessage) The message.
        q_name: (string) The name assigned by the modem.
        sin: (int) Service Identifier Number first byte of payload
        size: (int) Bytes in the message.
        retreived_time: (int)
        failed: (bool)
        state:
        callback: (Callable)

    """

    def __init__(self, message: object, q_name: str, sin: int, size: int):
        self.message = message
        self.q_name = q_name
        self.sin = sin
        self.size = size
        self.received_time = time()
        self.retrieved_time = None
        self.failed = False
        self.state = RX_COMPLETE
        self.callback = None


class _PendingLocation(object):
    """Holds metadata and callback for pending location information.
    
    Attributes:
        name: (str) The name of the Location object.
        callback: (Callable) The callback function.
        location: (nmea.Location)

    """
    def __init__(self, name, callback):
        self.name = name
        self.callback = callback
        self.location = nmea.Location()


class Modem(object):
    """
    Abstracts attributes and statistics related to an IDP modem

    Attributes:
        serial_port (object): The serial connection being used.
    """
    # ----------------------- Modem built-in Enumerated Types ------------------------------ #
    ctrl_states = (
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
    )

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

    # ----------------------- Twinning and helper objects ----------------------------------- #
    def __init__(self,
                 port='/dev/ttyUSB0',
                 use_crc=False, 
                 sat_status_interval=0,
                 sat_events_interval=0,
                 sat_mt_message_interval=15,
                 sat_mo_message_interval=5,
                 log=None,
                 logfile=False,
                 debug=False,
                 **kwargs
                 ):
        """Initializes attributes and pointers used by Modem class methods.

        Args:
            serial_name: (string) the name of the serial port to use
            use_crc: (bool) to use CRC for long cable length
            auto_monitor: (bool) enables automatic monitoring of satellite events
            log: (logging.Logger) an optional logger, preferably writing to a wrapping file
            logfile: (Boolean) save log to file
            debug: (Boolean) option for verbose trace

        """
        self._start_time = str(datetime.utcnow())
        self._debug = debug
        self._log = self._init_log(caller_name=get_caller_name(depth=1),
                                  log=log,
                                  logfile=logfile,
                                  debug=self._debug)
        # NOTICE
        self._log.warning('*** THIS CODE IS NOT COMPLETE ***')
        # serial and connectivity configuration and statistics
        self.serial_port = self._init_serial(port, **kwargs)
        self._reader_thread = ByteReaderThread(self.serial_port, IdpModem)
        self._reader_thread.start()
        _transport, self.modem = self._reader_thread.connect()
        del _transport  #: Not used
        self.modem.crc = use_crc
        self.modem.unsolicited_callback = self._on_unsolicited_serial
        self.at_timeouts = 0
        self.at_timeouts_total = 0
        # modem parameters
        self.mobile_id = None
        self.version = _Version()
        self.is_initialized = False
        self.s_registers = SRegisters()
        self.at_config = _AtConfiguration()
        self.crc = use_crc
        self.crc_errors = 0
        self.sat_status = _SatStatus()
        self.hw_event_notifications = self._init_hw_event_notifications()
        self.wakeup_interval = wakeup_intervals['5 seconds']
        self.power_mode = power_modes['Mobile Powered']
        self.asleep = False
        self.antenna_cut = False
        self.system_stats = self._init_system_stats()
        self.gnss_mode = gnss_modes['GPS']
        self.gnss_continuous = 0
        self.gnss_dpm_mode = gnss_dpm_modes['Portable']
        self.gnss_stats = self._init_gnss_stats()
        self.gpio = self._init_gpio()
        self.event_callbacks = self._init_event_callbacks()
        # Message queues -----------------------------------
        self.mo_msg_count = 0
        self.mo_msg_failed = 0
        self.mo_msg_queue = []
        self.mt_msg_count = 0
        self.mt_msg_failed = 0
        self.mt_msg_queue = []
        self._mt_message_callbacks = []   # (sin, min, callback)
        # Location Based Service ---------------------------
        self.location_pending = None        # active _PendingLocation
        self.on_location = None             # calls back with nmea.Location
        self.tracking_interval = 0
        # Satellite Status/Quality --------------------------
        self.sat_status_pending_callback = None
        # --- Serial processing threads ---------------------
        self._terminate = False
        # --- Timer threads for communication establishment and monitoring
        self._automation_threads = []
        # TODO: Low Power override of the above timer intervals
        self._automation_threads.append(RepeatingTimer(
            seconds=sat_mt_message_interval, name='sat_mt_message_monitor',
            callback=self.message_receive_queue_get))
        self._automation_threads.append(RepeatingTimer(
            seconds=sat_mo_message_interval, name='sat_mo_message_monitor',
            callback=self.mo_message_status))
        self._automation_threads.append(RepeatingTimer(
            seconds=sat_status_interval, name='sat_status_monitor', 
            callback=self._check_sat_status, defer=False))
        self._automation_threads.append(RepeatingTimer(
            seconds=sat_events_interval, name='sat_events_monitor',
            callback=self.check_events))
        self._automation_threads.append(RepeatingTimer(
            seconds=self.tracking_interval, name='tracking',
            callback=self._tracking, defer=False))
        self._init_modem()

    def _init_log(self, caller_name, log=None, logfile=None, debug=False):
        if is_logger(log):
            self._log = log
        else:
            log_name = get_caller_name(depth=1)
            log_file_name = (log_name + '.log') if logfile else None
            return get_wrapping_logger(
                name=log_name, filename=log_file_name, debug=debug)

    def terminate(self):
        self._log.debug("Terminated by external call {}".format(
            sys._getframe(1).f_code.co_name))
        end_time = str(datetime.utcnow())
        self._terminate = True
        try:
            self.modem.stop()
            self._reader_thread.close()
            for t in threading.enumerate():
                if t.name in self._automation_threads:
                    self._log.debug("Terminating thread {}".format(t.name))
                    t.stop_timer()
                    t.terminate()
                    t.join()
                # elif t.name in self.daemon_threads:
                #     self._log.debug("Terminating thread {}".format(t.name))
                #     t.join()
        except serial.SerialException as e:
            self._handle_error(e)
        self._log.info(
            "*** Statistics from {} to {} ***".format(self._start_time, end_time))
        self.log_statistics()

    def _handle_error(self, error_str):
        error_str = error_str.replace(',', ';')
        self._log.error(error_str)
        # TODO: may not be best practice to raise a ValueError in all cases
        raise ValueError(error_str)

    def _init_serial(self, serial_name, **kwargs):
        """
        Initializes the serial port for modem communications
        kwargs: baudrate, bytesize, parity, stopbits, timeout,
        write_timeout, xonxoff, rtscts, dsrdtr
        :param serial_name: (string) the port name on the host
        :param baud_rate: (integer) baud rate, default 9600 (8N1)

        """
        if isinstance(serial_name, str):
            is_valid_serial, details = validate_serial_port(serial_name)
            if is_valid_serial:
                try:
                    settings = {
                        'baudrate': 9600,
                        'bytesize': serial.EIGHTBITS,
                        'parity': serial.PARITY_NONE,
                        'stopbits': serial.STOPBITS_ONE,
                        'timeout': None,
                        'write_timeout': 0
                    }
                    for key in kwargs:
                        if key in settings: settings[key] = kwargs[key]
                    serial_port = serial.Serial(port=serial_name,
                                                baudrate=settings['baudrate'],
                                                bytesize=settings['bytesize'],
                                                parity=settings['parity'],
                                                stopbits=settings['stopbits'],
                                                timeout=settings['timeout'],
                                                write_timeout=settings['write_timeout'],
                                                xonxoff=False, rtscts=False, dsrdtr=False)
                    serial_port.flush()
                    self._log.info("Connected to {} at {} baud".format(details,
                                    settings['baudrate']))
                    return serial_port
                except serial.SerialException as e:
                    self._handle_error(
                        "Unable to open {} - {}".format(details, e))
            else:
                self._handle_error(
                    "Invalid serial port {} - {}".format(serial_name, details))
        else:
            self._handle_error(
                "Invalid type passed as serial_port - requires string name of port")

    @staticmethod
    def _init_hw_event_notifications():
        hw_event_notifications = OrderedDict({
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
        return hw_event_notifications

    @staticmethod
    def _init_system_stats():
        system_stats = {
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
        return system_stats

    @staticmethod
    def _init_gnss_stats():
        gnss_stats = {
            'nGNSS': 0,
            'lastGNSSReqTime': 0,
            'avgGNSSFixDuration': 0,
            'timeouts': 0,
        }
        return gnss_stats

    @staticmethod
    def _init_gpio():
        gpio = {
            "event_notification": None,
            "reset_out": None,
            "pps": None,
            "reset_in": None,
        }
        return gpio

    # ----------------------- Event Handling Setup -------------------------------------------------- #
    events = (
        'connect',
        'disconnect',
        'satellite_status_change',
        'registered',
        'blocked',
        'unblocked',
        'bb_wait',
        'new_mt_message',
        'mo_message_complete',
        'wakeup_interval_change',
        'new_gnss_fix',
        'event_trace',
        'unsolicited_serial',
    )

    def _init_event_callbacks(self):
        """Initializes an event callbacks list"""
        event_callbacks = {}
        for event in self.events:
            event_callbacks[event] = None
        return event_callbacks

    def register_event_callback(self, event, callback):
        """
        Registers a callback function against a particular event
        TODO: does not check for callback function validity

        :param event: (string)
        :param callback:
        :returns:

           * (Boolean) success
           * (string) failure reason if success=False

        """
        if event in self.events:
            if callback is not None:
                if self.event_callbacks[event] is not None:
                    self._log.warning("{} event callback overwritten - old:{} new{}"
                                     .format(event, self.event_callbacks[event].__name__, callback.__name__))
                self.event_callbacks[event] = callback
                return True, None
            else:
                return False, "No callback defined"
        else:
            self._log.error("Invalid attempt to register callback event {}, must be in {}".format(
                event, self.events))
            return False, "Invalid event"

    def _on_event(self):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # ------------------------ Connection Management ------------------------------------------------ #
    def _on_connect(self):
        """
        Stops trying to establish communications, starts monitoring for communications loss and calls back connect event
        """
        if not self._terminate:
            self._init_modem()
        if self.event_callbacks['connect'] is not None:
            self.event_callbacks['connect']()

    def _on_disconnect(self):
        """
        Stops monitoring modem operations and communications, and starts trying to re-connect.
        Calls back the disconnect event.
        """
        for t in self._automation_threads:
            t.stop_timer()
        self.is_initialized = False
        if self.event_callbacks['disconnect'] is not None:
            self.event_callbacks['disconnect']()

    def _on_unsolicited_serial(self, read_str):
        if self.event_callbacks['unsolicited_serial'] is not None:
            self.event_callbacks['unsolicited_serial'](read_str)
        else:
            self._log.warning(
                "No callback defined for unsolicited serial: {}".format(read_str))

    # ---------------------- Modem initialization & twinning ---------------------------------------- #
    def _init_modem(self, step=1):
        # TODO: handle AtTimeout
        self._log.debug("Initializing modem...step {}".format(step))
        try:
            # TODO: check settings restored from nvm are what we want
            connected = False
            while not connected:
                connected = self.modem.config_restore_nvm()
                sleep(1)
        except AtCrcConfigError:
            if self.crc:
                self.modem.crc = True
            else:
                if not self.modem.crc_enable(False):
                    raise IdpException('Could not disable CRC')
            self.modem.config_restore_nvm()
        if self.crc and not self.modem.crc:
            if not self.modem.crc_enable(True):
                raise IdpException('Could not enable CRC')
        # TODO: return (None, None) or check if None is ok for unpack
        atv = self.modem.config_nvm_report()
        if atv is not None:
            self._cb_get_config(atv)
        else:
            raise IdpException('Could not retrieve configuration from AT&V')
        # TODO: check to ensure Verbose is enabled, should be from nvm default
        self.mobile_id = self.modem.device_mobile_id()
        if self.mobile_id is None:
            raise IdpException('Could not retreive Mobile ID')
        versions = self.modem.device_version()
        if versions is not None:
            self.version.hardware = versions[0]
            self.version.firmware = versions[1]
            self.version.at_protocol = versions[2]
        else:
            raise IdpException('Could not retreive version information')
        key_sregisters = {
            'S39': 'GNSS Mode',
            'S41': 'GNSS Fix Timeout',
            'S42': 'GNSS Augmentation Systems',   # returns ERROR from Modem Simulator
            'S51': 'Wakeup Interval',
            'S55': 'GNSS Continuous',
            # 'S56': 'GNSS Jamming Status',   # Volatile not config...request elsewhere
            # 'S57': 'GNSS Jamming Indicator',   # Volatile not config
        }
        register_query = 'AT'
        for k in key_sregisters:
            reg = self.s_registers.register(k)
            actual = self.modem.s_register_get(k)
            if reg.default != actual:
                name = reg.description
                self._log.warning('{}:{} value {} does not match default {}'
                                  .format(name, k, actual, reg.default))
                reg.set(actual)
        # self.get_event_notification_control(init=True)
        # self.get_wakeup_interval()
        # self.get_power_mode()
        # self.get_gnss_mode()
        # self.get_gnss_continuous()
        # self.get_gnss_dpm()
        # self._init_modem(step=6)
        if not self.modem.config_nvm_save():
            raise IdpException('Could not save configuration to NVM')
        else:
            self._log.info('Modem initialization complete')
            self.is_initialized = True
            self._on_initialized()

    def _cb_get_config(self, response):
        """
        Called back by initialization reading AT&V get current and saved configuration.
        Configures the AT mode parameters (Echo, Verbose, CRC, etc.) and S-registers twin.
        If successful, increments and calls the next initialization step.

        :param valid_response: (boolean)
        :param responses: (_PendingAtCommand)

        """
        # TODO REMOVE step = 1
        success = False
        if response is not None:
            success = True
            self._log.debug('Processing AT&V response')
            # active_header = responses[0]
            at_config, reg_config = response
            # at_config = responses[1].split(" ")
            for param in at_config:
                warning = None
                if param == 'echo':
                    target = self.at_config.echo
                elif param == 'quiet':
                    target = self.at_config.quiet
                elif param == 'verbose':
                    target = self.at_config.verbose
                elif param == 'crc':
                    target = self.at_config.crc
                else:
                    # TODO: generic error handling?
                    error = 'Unknown AT config {}'.format(param)
                    self._log.error(error)
                    raise IdpException(error)
                if target != at_config[param]:
                    warning = (param, target)
                if warning is not None:
                    self._log.warning(
                        'Configured {} setting does not match target {}'
                        .format(warning[0], warning[1]))
                target = at_config[param]
            for name in reg_config:
                reg = self.s_registers.register(name)
                value = reg_config[name]
                if value != reg.default:
                    self._log.warning('Updating {}:{} value={} (default={})'.format(
                        name, reg.description, value, reg.default))
                    reg.set(value)
        return success

    def _on_initialized(self):
        autonomous = len(self._automation_threads) > 0
        for t in self._automation_threads:
            t.start()
            t.start_timer()
        if self.tracking_interval > 0:
            self.tracking_setup(
                interval=self.tracking_interval, on_location=self.on_location)
        if not autonomous:
            self._log.info(
                "Automonous mode disabled, user application must query modem actively")

    # ---------------------- SATELLITE STATUS MONITORING -------------------------------------------- #
    def _check_sat_status(self):
        self._log.debug(
            'Monitoring satellite status - current status: {}'.format(
            self.sat_status.ctrl_state))
        response = self.modem.sat_status_snr()
        if response is None:
            raise IdpException('Could not retreive satellite status trace')
        ctrl_state, c_n0 = response
        LOW_SNR_THRESHOLD = 38.0
        old_sat_ctrl_state = self.sat_status.ctrl_state
        new_sat_ctrl_state = self.ctrl_states[ctrl_state]
        self._log.debug("Current satellite status: {}".format(
                        new_sat_ctrl_state))
        if new_sat_ctrl_state != old_sat_ctrl_state:
            sat_status_change = new_sat_ctrl_state
            self._log.info("Satellite control state change: OLD={} NEW={}"
                            .format(old_sat_ctrl_state, new_sat_ctrl_state))
            self.sat_status.ctrl_state = new_sat_ctrl_state
            # Key events for relevant state changes and statistics tracking
            if new_sat_ctrl_state == 'Waiting for GNSS fix':
                self.system_stats['lastGNSSStartTime'] = int(time())
                self.system_stats['nGNSS'] += 1
            elif new_sat_ctrl_state == 'Registration in progress':
                if self.sat_status.registered:
                    self.sat_status.registered = False
                self.system_stats['lastRegStartTime'] = int(time())
            elif new_sat_ctrl_state == 'Downloading Bulletin Board':
                self.sat_status.bb_wait = True
                self.system_stats['lastBBStartTime'] = time()
                # TODO: Is prior registration now invalidated?
                sat_status_change = 'bb_wait'
            elif new_sat_ctrl_state == 'Active':
                if self.sat_status.blocked:
                    self._log.info("Blockage cleared")
                    blockage_duration = int(
                        time() - self.system_stats['lastBlockStartTime'])
                    if self.system_stats['avgBlockageDuration'] > 0:
                        self.system_stats['avgBlockageDuration'] \
                            = int((blockage_duration + self.system_stats['avgBlockageDuration']) / 2)
                    else:
                        self.system_stats['avgBlockageDuration'] = blockage_duration
                        sat_status_change = 'unblocked'
                if not self.sat_status.registered:
                    self.sat_status.registered = True
                    if old_sat_ctrl_state != 'Stopped':
                        self._log.debug("Modem registered")
                        self.system_stats['nRegistration'] += 1
                        if self.system_stats['lastRegStartTime'] > 0:
                            registration_duration = int(
                                time() - self.system_stats['lastRegStartTime'])
                        else:
                            registration_duration = 0
                        if self.system_stats['avgRegistrationDuration'] > 0:
                            self.system_stats['avgRegistrationDuration'] \
                                = int((registration_duration + self.system_stats['avgRegistrationDuration']) / 2)
                        else:
                            self.system_stats['avgRegistrationDuration'] = registration_duration
                        sat_status_change = 'registered'
                self.sat_status.blocked = False
                self.sat_status.bb_wait = False
            elif new_sat_ctrl_state == 'Blocked':
                self.sat_status.blocked = True
                self.system_stats['lastBlockStartTime'] = time()
                self._log.info("Blockage started")
                sat_status_change = 'blocked'
            # Other transitions for statistics tracking:
            if old_sat_ctrl_state == 'Waiting for GNSS fix':
                gnss_duration = int(
                    time() - self.system_stats['lastGNSSStartTime'])
                self._log.info(
                    "GNSS acquired in {} seconds".format(gnss_duration))
                if self.system_stats['avgGNSSFixDuration'] > 0:
                    self.system_stats['avgGNSSFixDuration'] \
                        = int((gnss_duration + self.system_stats['avgGNSSFixDuration']) / 2)
                else:
                    self.system_stats['avgGNSSFixDuration'] = gnss_duration
                if new_sat_ctrl_state not in ['Stopped', 'Blocked', 'Active']:
                    sat_status_change = 'new_gnss_fix'
                else:
                    self._log.debug(
                        "GNSS fix implied by state transition to {}".format(new_sat_ctrl_state))
            if old_sat_ctrl_state == 'Downloading Bulletin Board' \
                    and new_sat_ctrl_state not in ['Stopped', 'Blocked']:
                bulletin_duration = int(
                    time() - self.system_stats['lastBBStartTime'])
                self._log.info(
                    "Bulletin Board downloaded in {} seconds".format(bulletin_duration))
                if self.system_stats['avgBBReacquireDuration'] > 0:
                    self.system_stats['avgBBReacquireDuration'] \
                        = int((bulletin_duration + self.system_stats['avgBBReacquireDuration']) / 2)
                else:
                    self.system_stats['avgBBReacquireDuration'] = bulletin_duration
            self._on_sat_status_change(sat_status_change)
        if c_n0 <= LOW_SNR_THRESHOLD and not self.sat_status.low_snr:
            # TODO: generate event
            self._log.warning('Low SNR {} dB detected'.format(c_n0))
            self.sat_status.low_snr = True
        elif c_n0 > LOW_SNR_THRESHOLD and self.sat_status.low_snr:
            self._log.info('Adequate SNR {} dB recovered'.format(c_n0))
            self.sat_status.low_snr = False
        self._log.debug('SNR: {} dB'.format(c_n0))
        if self.system_stats['avgCN0'] == 0:
            self.system_stats['avgCN0'] = c_n0
        else:
            self.system_stats['avgCN0'] = round(
                (self.system_stats['avgCN0'] + c_n0) / 2.0, 2)

    def _on_sat_status_change(self, event):
        if event in self.events:
            if self.event_callbacks[event] is not None:
                self._log.info("Calling back for {} to {}".format(
                    event, self.event_callbacks[event].__name__))
                self.event_callbacks[event](event)
            else:
                self._log.info("No callback defined for {}".format(event))
        else:
            if self.event_callbacks['satellite_status_change'] is not None:
                self._log.info("Calling back for satellite_status_change to {}"
                              .format(self.event_callbacks[event].__name__))
                self.event_callbacks['satellite_status_change'](event)
            else:
                self._log.info(
                    "No callback defined for satellite_status_change")

    # ---------------------- MESSAGE HANDING -------------------------------------------------------- #
    # TODO: delete MT messages, cancel MO message(s)
    @staticmethod
    def message_state_get(state):
        state_str = ""
        if state == UNAVAILABLE:
            state_str = "Unavailable"
        elif state == RX_COMPLETE:
            state_str = "MT (Rx) Complete"
        elif state == RX_RETRIEVED:
            state_str = "MT (Rx) Retrieved"
        elif state == TX_READY:
            state_str = "MO (Tx) Ready"
        elif state == TX_SENDING:
            state_str = "MO (Tx) Sending"
        elif state == TX_COMPLETE:
            state_str = "MO (Tx) Complete"
        elif state == TX_FAILED:
            state_str = "MO (Tx) Failed"
        else:
            state_str = "UNKNOWN"
        return "{} ({})".format(state_str, state)

    def send_message(self, mo_message, callback=None, priority=None):
        return self.message_send(mo_message, callback, priority)

    def message_send(self, mo_message, callback=None, priority=None):
        """
        Submits a message on the AT command interface and calls back when complete.

        :param mo_message: (MobileOriginatedMessage)
        :param callback: (function)
        :param priority: (int(
        :return: (string) a unique 8-character name for the message based on the time it was submitted

        """
        if isinstance(mo_message, MobileOriginatedMessage):
            p_msg = _PendingMoMessage(message=mo_message, callback=callback)
            self._log.debug("User submitted message name: {} mapped to {}".format(
                            mo_message.name, p_msg.q_name))
            include_min = False if mo_message.min is None else True
            data_format = mo_message.data_format
            data = mo_message.data(data_format, include_min)
            response = self.modem.message_mo_send(
                data=data,
                data_format=data_format,
                name=p_msg.q_name,
                priority=mo_message.priority,
                sin=mo_message.sin,
                min=mo_message.min,
            )
            if response is not None:
                self.mo_msg_queue.append(p_msg)
                return response
        else:
            self._handle_error(
                "Message submitted must be type MobileOriginatedMessage")

    def _update_stats_mo_messages(self, size, latency):
        self.mo_msg_count += 1
        if self.system_stats['avgMOMsgSize'] == 0:
            self.system_stats['avgMOMsgSize'] = size
        else:
            self.system_stats['avgMOMsgSize'] = int((self.system_stats['avgMOMsgSize'] + size) / 2)
        if self.system_stats['avgMOMsgLatency_s'] == 0:
            self.system_stats['avgMOMsgLatency_s'] = latency
        else:
            self.system_stats['avgMOMsgLatency_s'] = int((self.system_stats['avgMOMsgLatency_s'] + latency) / 2)

    def mo_message_status(self, msg_name=None, user_callback=None):
        """
        Checks the state of messages in the modem queue, triggering a callback with the responses

        :param msg_name:
        :param user_callback:
        :return:
        """
        if len(self.mo_msg_queue) > 0:
            msg_list = []
            for m in self.mo_msg_queue:
                msg_list.append(m.q_name)
            self._log.debug("{} MO messages queued ({})".format(
                len(self.mo_msg_queue), msg_list))
            response = self.modem.message_mo_state(msg_name)
            if response is None:
                raise Exception("Unable to retrieve MO message status")
            if msg_name is not None:
                return response
            for msg_status in response:
                for pending_msg in self.mo_msg_queue:
                    if pending_msg.name == msg_status.name:
                        self._log.debug('Processing message {} state {}'.format(
                            msg_status.name, msg_status.status))
                        if pending_msg.state != msg_status.state:
                            if msg_status.state in (TX_COMPLETE, TX_FAILED):
                                pending_msg.complete_time = time()
                                mo_msg_latency = int(pending_msg.complete_time
                                                    - pending_msg.submit_time)
                                if msg_status.state == TX_FAILED:
                                    pending_msg.failed = True
                                    self.mo_msg_failed += 1
                                self._log.debug(
                                    "Removing {} from pending message queue"
                                    .format(pending_msg.q_name))
                                self.mo_msg_queue.remove(pending_msg)
                                self._update_stats_mo_messages(msg_status.size,
                                                               mo_msg_latency)
                                # TODO: calculate statistics for MO message transmission times
                                if pending_msg.callback is not None:
                                    pending_msg.callback(success=True, message=(
                                        msg_status.name,
                                        pending_msg.q_name,
                                        msg_status.state,
                                        msg_status.size))
                                else:
                                    self._log.warning(
                                        "No callback defined for {}".format(pending_msg.q_name))
        else:
            self._log.debug("No MO messages queued")

    def message_receive_queue_get(self, user_callback=None):
        response = self.modem.message_mt_waiting()
        if response is None:
            raise Exception('No response to MT message check')
        new_messages = []
        for msg_waiting in response:
            if msg_waiting.state == RX_COMPLETE:  # Complete and not read
                # TODO: assign data_format based on size?
                queued = False
                for p_msg in self.mt_msg_queue:
                    if p_msg.q_name == msg_waiting.name:
                        queued = True
                        self._log.debug(
                            "Pending message {} already in queue".format(
                            msg_waiting.name))
                        break
                if not queued:
                    new_messages.append(msg_waiting.name)
                    p_msg = _PendingMtMessage(message=None,
                                              q_name=msg_waiting.name,
                                              sin=msg_waiting.sin,
                                              size=msg_waiting.size)
                    self.mt_msg_queue.append(p_msg)
                    self._update_stats_mt_messages(msg_waiting.size)
            else:
                self._log.debug(
                    "Message {} not complete ({}/{} bytes)".format(
                    msg_waiting.name, msg_waiting.received, msg_waiting.size))
        if len(new_messages) > 0:
            if self.event_callbacks['new_mt_message'] is not None:
                #: TODO (Geoff) TEST!
                self._log.debug("Calling back to {}".format(
                    self.event_callbacks['new_mt_message'].__name__))
                self.event_callbacks['new_mt_message'](self.mt_msg_queue)
            else:
                self._log.warning(
                    "No callback registered for new MT messages")
            if len(self._mt_message_callbacks) > 0:
                for msg_name in new_messages:
                    for msg in self.mt_msg_queue:
                        if msg.q_name == msg_name:
                            for tup in self._mt_message_callbacks:
                                if tup[0] == msg.sin:
                                    self.mt_message_get(
                                        name=msg_waiting.name, callback=tup[1])
                                    break  # for tup
                            break  # for msg

    def _update_stats_mt_messages(self, size):
        self.mt_msg_count += 1
        if self.system_stats['avgMTMsgSize'] == 0:
            self.system_stats['avgMTMsgSize'] = size
        else:
            self.system_stats['avgMTMsgSize'] = int((self.system_stats['avgMTMsgSize'] + size) / 2)

    def message_receive(self, name, callback, data_format=FORMAT_B64):
        return self.mt_message_get(name, callback, data_format)

    def mt_message_get(self, name, callback, data_format=FORMAT_B64):
        found = False
        for m in self.mt_msg_queue:
            if m.q_name == name:
                found = True
                m.callback = callback
                self._log.info("Retrieving MT message {}".format(name))
                break
        response = self.modem.message_mt_get(name, data_format)
        if response is None:
            raise Exception('No response to message retrieval request')
        # TODO: is SIN byte always included in data returned?
        data = response
        if data_format == FORMAT_TEXT:
            # remove quotes but only at both ends, not in the middle
            text = data[1:len(data)-1]
            if text[0] == '\\':
                msg_min = int(text[1:3], 16)
                payload = str(text[3:])
            else:
                payload = text
        elif data_format == FORMAT_HEX:
            payload = bytearray.fromhex(data)
            msg_sin = payload[0]
            msg_min = payload[1]
        else:
            payload = binascii.b2a_base64(b'{}'.format(data))
            msg_sin = payload[0]
            msg_min = payload[1]
        mt_msg = MobileTerminatedMessage(
            payload=payload,
            name=name,
            msg_sin=msg_sin,
            msg_min=msg_min,
            # msg_num=msg_num,   #: Probably not required, maybe for logging
            priority=PRIORITY_MT,
            data_format=data_format,
            # size=size,
            debug=self._debug)
        return mt_msg

    def _cb_get_mt_message(self, valid_response, responses, request):
        if valid_response:
            # Response format: "<fwdMsgName>",<msgNum>,<priority>,<sin>,<state>,<length>,<dataFormat>,<data>
            #  where <data> is surrounded by quotes if dataFormat is text
            if len(responses) > 1:
                self._log.warning("Unexpected responses {}".format(responses))
            response = responses[0].replace('%MGFG:', '').strip()
            q_name, msg_num, priority, sin, state, length, data_format, data = response.split(
                ',')
            del state  # unused
            q_name = q_name.replace('\"', '')
            priority = int(priority)
            if priority != PRIORITY_MT:
                # T203 states that priority is always 0 for Mobile-Terminated messages
                self._log.warning(
                    "MT Message {} priority non-zero: {}".format(msg_num, priority))
            sin = int(sin)
            size = int(length)
            data_format = int(data_format)
            msg_min = None
            if data_format == FORMAT_TEXT:
                # remove quotes but only at both ends, not in the middle
                text = data[1:len(data)-1]
                if text[0] == '\\':
                    msg_min = int(text[1:3], 16)
                    payload = str(text[3:])
                else:
                    payload = text
            elif data_format == FORMAT_HEX:
                payload = bytearray.fromhex(data)
            else:
                payload = binascii.b2a_base64(b'{}'.format(data))
            mt_msg = MobileTerminatedMessage(payload=payload, name=q_name, msg_sin=sin, msg_min=msg_min,
                                             msg_num=msg_num, priority=PRIORITY_MT, data_format=data_format, size=size,
                                             debug=self._debug)
            for m in self.mt_msg_queue:
                if m.q_name == q_name:
                    self.mt_msg_queue.remove(m)
                    if m.callback is not None:
                        self._log.debug(
                            "Calling back to {}".format(m.callback.__name__))
                        m.callback(mt_msg)
                    else:
                        self._log.error(
                            "No callback defined for message {}".format(q_name))
                    break
        else:
            self._log.error("Invalid response ({})".format(responses))

    def mt_message_callback_add(self, sin, callback):
        """
        Intended to override generic event notification for new MT messages, retrieves and checks against list
        of SIN.
        Should pass in a set of: callback, SIN, MIN, data_format, codec, callback
        ..todo::
           Allow for filtering beyond SIN byte (processed after retrieval from modem queue)

        :param callback: (function) will be called back with callback(sin, message)
        :param sin: (int) Service Identifier Number

        """
        if isinstance(sin, int) and sin in range(0, 255+1):
            self._mt_message_callbacks.append((sin, callback))
        else:
            self._log.error("SIN must be integer in range 0..255")

    def mt_message_callback_remove(self, sin):
        """Removes the specified SIN from the callback list"""
        for tup in self._mt_message_callbacks:
            if tup[0] == sin:
                self._mt_message_callbacks.remove(tup)

    def mt_message_remove(self, name):
        if name in self.mt_msg_queue:
            self._log.debug("Removing MT message {}".format(name))
            self.submit_at_command(at_command='AT%MGFM=\"{}\"'.format(name),
                                   callback=self._cb_mt_message_remove)
            pass

    def _cb_mt_message_remove(self, valid_response, responses, request):
        # TODO: test
        self._log.warning("Message remove callback not implemented")
        msg_name = request.split('=')[1]
        if valid_response:
            self._log.debug(
                "Mobile-Terminated message removed {}".format(request))
            for p_msg in self.mt_msg_queue:
                if p_msg.q_name == msg_name:
                    self.mt_msg_queue.remove(p_msg)
                    break
        else:
            msg_name = request.split('\"')[1]
            self._log.error(
                "MT Message {} removal Failed: {}".format(msg_name, responses))

    def mt_messages_flush(self):
        for m in self.mt_msg_queue:
            self.mt_message_remove(m.q_name)

    # ----------------------- HARDWARE EVENT NOTIFICATIONS ----------------------------------------- #
    # TODO: %EVMON, %EVNT, %EVSTR, %EXIT, %SYSL
    def get_event_notification_control(self, init=False):
        """
        Updates the ``hw_event_notifications`` attribute

        :param init: (boolean) flag set if the register value has just been read during initialization
        :return: An ``OrderedDict`` corresponding to the ``hw_event_notifications`` attribute.

        """
        if init:
            self._update_event_notifications(
                self.s_registers.register('S88').get())
        else:
            self.submit_at_command(
                'ATS88?', callback=self._cb_get_event_notification_control)

    def _cb_get_event_notification_control(self, valid_response, responses):
        if valid_response:
            reg_value = int(responses[0])
            self.s_registers.register('S88').set(reg_value)
            self._update_event_notifications(reg_value)
        else:
            self._log.warning(
                "Invalid response to ATS88? command: {}".format(responses))

    def _update_event_notifications(self, value):
        """
        Sets the proxy bitmap values for event_notification in the modem object.

        :param value: the event bitmap (integer)

        """
        event_notify_bitmap = bin(value)[2:]
        if len(event_notify_bitmap) > len(self.hw_event_notifications):
            event_notify_bitmap = event_notify_bitmap[:len(
                self.hw_event_notifications) - 1]
        while len(event_notify_bitmap) < len(self.hw_event_notifications):  # pad leading zeros
            event_notify_bitmap = '0' + event_notify_bitmap
        i = 0
        for key in reversed(self.hw_event_notifications):
            self.hw_event_notifications[key] = True if event_notify_bitmap[i] == '1' else False
            i += 1
        self._log.debug("Updated event notifications: {}".format(
            self.hw_event_notifications))

    def set_event_notification_control(self, key, value):
        """
        Sets a particular event monitoring status in the
        :py:func:`event notification bitmap <at_get_event_notify_control_bitmap>`.

        :param key: (string) event name as defined in the ``OrderedDict``
        :param value: (Boolean) to set/clear the bit

        """
        if isinstance(value, bool):
            if key in self.hw_event_notifications:
                bitmap = '0b'
                for event in reversed(self.hw_event_notifications):
                    bit = '1' if self.hw_event_notifications[event] else '0'
                    if key == event:
                        if self.hw_event_notifications[event] != value:
                            bit = '1' if value else '0'
                    bitmap += bit
                new_bitmap_value = int(bitmap, 2)
                if new_bitmap_value != self.s_registers.register('S88').get():
                    self._update_event_notifications(new_bitmap_value)
                    self.submit_at_command('ATS88={}'.format(new_bitmap_value),
                                           callback=self._cb_set_event_notification_control)
                    self._log.info("{} event notification {}".format(
                        key, "enabled" if value else "disabled"))
                else:
                    self._log.debug("No change to {} event notification {}"
                                   .format(key, "enabled" if value else "disabled"))
            else:
                self._log.error("Event {} not defined".format(key))
        else:
            self._log.error("Value {} must be type Boolean".format(value))

    def _cb_set_event_notification_control(self, valid_response, responses):
        if not valid_response:
            # TODO: read S88 and update self.hw_event_notifications and twin
            self._log.warning(
                "Failed to update event notifications control S88: {}".format(responses))
            self.get_event_notification_control()

    def check_events(self, user_callback=None, events=['ALL']):
        """
        Function to be called by microcontroller when event pin is asserted.

        NOT implemented.  TODO: set up event callbacks
        """
        self._log.warning("CHECK ATS89? NOT IMPLEMENTED")
        if user_callback is None:
            callback = self._cb_check_events
        else:
            callback = user_callback
        self.submit_at_command('ATS89?', callback=callback)

    def _cb_check_events(self, valid_response, responses, request):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))
        if valid_response:
            self._log.warning("FUNCTION SHOULD BE TRIGGERED BY GPIO ASSERTION")
            # TODO: parse bitmap for the various events, check against registered notifications & act
        else:
            self._log.error("Error checking S89 events: {}".format(responses))

    def _on_hw_event(self, event):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # --------------------- NMEA/LBS OPERATIONS --------------------------------------------------- #
    # TODO: set up periodic tracking pushed with callbacks, and using %TRK
    def set_gnss_mode(self, gnss_mode=GNSS_MODE_GPS):
        if gnss_mode in GNSS_MODES:
            if int(self.s_registers.register('S39').get()) == gnss_mode:
                self._log.debug("GNSS mode already set to {}".format(gnss_mode))
            else:
                self.submit_at_command(
                    'ATS39={}'.format(gnss_mode), callback=None)
                # TODO: some risk that write fails and twin is no longer sychronized
                self.s_registers.register('S39').set(gnss_mode)
        else:
            self._log.error("Invalid GNSS mode {}".format(gnss_mode))

    def get_gnss_mode(self):
        """Gets the GNSS mode (S39, default 0) and stores in the Modem instance."""
        return int(self.s_registers.register('S39').get())

    def set_gnss_dynamic_mode(self, dpm_mode=GNSS_DPM_PORTABLE):
        if dpm_mode in GNSS_DPM_MODES:
            if int(self.s_registers.register('S33').get()) == dpm_mode:
                self._log.debug("GNSS mode already set to {}".format(dpm_mode))
            else:
                self.submit_at_command(
                    'ATS33={}'.format(dpm_mode), callback=None)
                # TODO: some risk that write fails and twin is no longer sychronized
                self.s_registers.register('S33').set(dpm_mode)
        else:
            self._log.error(
                "Invalid GNSS Dynamic Platform Model {}".format(dpm_mode))

    def get_gnss_dynamic_model(self):
        """Gets the GNSS Dynamic Platform Model (S33, default 0) and stores in Modem instance."""
        return int(self.s_registers.register('S33').get())

    def get_gnss_continuous_interval(self):
        return int(self.s_registers.register('S55').get())

    def set_gnss_continuous_interval(self, seconds, doppler=None):
        if isinstance(seconds, int) and seconds in range(0, 30+1):
            if doppler is None:
                doppler_str = ''
            else:
                doppler_str = ',1' if doppler else ',0'
            if int(self.s_registers.register('S55').get()) == seconds:
                self._log.debug(
                    "GNSS refresh interval already set to {}".format(seconds))
            else:
                self.submit_at_command(at_command='AT%TRK={}{}'.format(
                                            seconds, doppler_str),
                                       callback=self._cb_set_gnss_continuous)
        else:
            self._log.error(
                "Invalid GNSS refresh interval - must be integer in range 0..30 (seconds)")

    def _cb_set_gnss_continuous(self, valid_response, responses, request):
        if valid_response:
            seconds = int(request.split('=')[1])
            self.s_registers.register('S55').set(seconds)
        else:
            self._log.error("Error setting GNSS continuous {}".format(request))
            # TODO: infer/revert tracking interval?

    def get_location(self, callback, name=None, fix_age=30, nmea=['RMC', 'GGA', 'GSA', 'GSV']):
        """
        Queries GNSS NMEA strings from the modem and returns a list of sentences (assuming no fix timeout).

        :param callback: the function to which location will be passed back
        :param name: (optional) identifier string or None
        :param fix_age: (int) maximum age of GNSS fix to use
        :param gga: essential fix data
        :param rmc: recommended minimum
        :param gsa: dilution of precision (DOP) and satellites
        :param gsv: satellites in view

        """
        MIN_STALE_SECS = 1
        MAX_STALE_SECS = 600
        MIN_WAIT_SECS = 1
        MAX_WAIT_SECS = 600
        NMEA_SUPPORTED = ['RMC', 'GGA', 'GSA', 'GSV']

        # TODO: get fix age from Trace Class 4 Subclass 2 Index 7
        refresh = self.get_gnss_continuous_interval()
        if 0 < refresh < fix_age:
            fix_age = refresh
        stale_secs = min(MAX_STALE_SECS, max(MIN_STALE_SECS, fix_age))
        wait_secs = min(MAX_WAIT_SECS, max(
            MIN_WAIT_SECS, int(max(45, stale_secs - 1))))
        # example sentence string: '"GGA","RMC","GSA","GSV"'
        req_sentences = ''
        for sentence in nmea:
            sentence = sentence.upper()
            if sentence in NMEA_SUPPORTED:
                if len(req_sentences) > 0:
                    req_sentences += ','
                req_sentences += '"{}"'.format(sentence)
            else:
                self._log.error(
                    "Unsupported NMEA sentence: {}".format(sentence))
        # TODO: manage multiple _PendingLocation using a queue
        if self.location_pending is None:
            self._log.debug("New Location request pending")
            self.location_pending = self._PendingLocation(
                name=name, callback=callback)
            self.submit_at_command(at_command='AT%GPS={},{},{}'.format(stale_secs, wait_secs, req_sentences),
                                   callback=self._cb_get_nmea, timeout=wait_secs+5)
            self.gnss_stats['nGNSS'] += 1
            self.gnss_stats['lastGNSSReqTime'] = int(time())
        else:
            self._log.warning(
                "Prior location request pending - discarding request")

    def _cb_get_nmea(self, valid_response, responses, request):
        if valid_response:
            # TODO: update GNSS stats
            nmea_data_set = []
            # responses[0] should just be %GPS header
            for nmea_sentence in responses:
                nmea_sentence = nmea_sentence.replace('%GPS:', '').strip()
                if nmea_sentence.startswith('$G'):
                    nmea_data_set.append(nmea_sentence)
            if len(nmea_data_set) > 0:
                gnss_fix_duration = int(time()) - \
                    self.gnss_stats['lastGNSSReqTime']
                if self.gnss_stats['avgGNSSFixDuration'] > 0:
                    self.gnss_stats['avgGNSSFixDuration'] = int((gnss_fix_duration +
                                                                 self.gnss_stats['avgGNSSFixDuration']) / 2)
                else:
                    self.gnss_stats['avgGNSSFixDuration'] = gnss_fix_duration
                success, errors = nmea.parse_nmea_to_location(nmea_data_set=nmea_data_set,
                                                              loc=self.location_pending.location)
                if success:
                    if self.location_pending is not None and self.location_pending.callback is not None:
                        self.location_pending.callback(
                            self.location_pending.location)
                    else:
                        self._log.warning(
                            "No callback defined for pending location")
                    self.location_pending = None
                else:
                    self._log.error(errors)
        else:
            self._log.error("Error getting location: {}".format(responses))
            if 'TIMEOUT' in responses:
                # TODO: set up heuristic/backoff on timed out responses
                self.gnss_stats['timeouts'] += 1
                sleep(5)
                self.submit_at_command(
                    at_command=request, callback=self._cb_get_nmea)

    def tracking_setup(self, interval=0, on_location=None):
        """
        Sets up tracking interval in seconds with optional callback on_location.
        GNSS refresh is done at half the tracking interval.
        """
        if on_location is not None:
            self.on_location = on_location
        if isinstance(interval, int) and interval in range(0, 86400*7+1):
            if interval == 0:
                self._log.info("Tracking disabled")
                self.thread_tracking.stop_timer()
                self.tracking_interval = 0
                self.set_gnss_continuous_interval(seconds=0)
            else:
                if interval <= 30:
                    refresh = int(interval/2)
                    self._log.debug(
                        "Setting GNSS continuous mode at {} seconds refresh".format(refresh))
                else:
                    refresh = 0
                    self._log.debug(
                        "Disabling GNSS continuous mode for interval {}s".format(interval))
                self.set_gnss_continuous_interval(seconds=refresh)
                self._log.info(
                    "Tracking interval set to {} seconds".format(interval))
                self.thread_tracking.change_interval(interval)

    def _tracking(self):
        self.get_location(callback=self._cb_tracking,
                          name='tracking', fix_age=self.tracking_interval)

    def _cb_tracking(self, loc):
        if self.on_location is not None:
            self._log.debug("Tracking calling back to {} with Location".format(
                self.on_location.__name__))
            self.on_location(loc)
        else:
            self._log.warning("No on_location callback defined")

    # --------------------- LOW POWER OPERATIONS ----------------------------------------------- #
    # TODO: manage GNSS settings on entry/exit to LPM, collect garbage, etc.
    def get_wakeup_interval(self, init=False):
        if not init:
            self._log.warning(
                "S51 value twin may be out of date requiring follow-up query")
            self.submit_at_command(at_command='ATS51?',
                                   callback=self._cb_get_wakeup_interval)
        return self.s_registers.register('S51').get()

    def _cb_get_wakeup_interval(self, valid_response, responses, request):
        if valid_response:
            value = int(responses[0])
            self._log.info("Updating S51 register value: {}".format(value))
            self.s_registers.register('S51').set(value)

    def set_wakeup_interval(self, wakeup_interval=WAKEUP_5_SEC):
        if wakeup_interval in WAKEUP_INTERVALS:
            self.submit_at_command(at_command='ATS51={}'.format(
                wakeup_interval), callback=self._cb_set_wakeup_interval)
            # TODO: some risk that write fails and twin is no longer synchronized
            self.s_registers.register('S51').set(wakeup_interval)
        else:
            self._log.error(
                "Invalid wakeup interval {}".format(wakeup_interval))

    def _cb_set_wakeup_interval(self, valid_response, responses, request):
        if valid_response:
            value = int(request.replace('ATS51=', ''))
            self._log.info("Updating S51 register value: {}".format(value))
            self.s_registers.register('S51').set(value)

    def set_power_mode(self, power_mode=POWER_MODE_MOBILE_POWERED):
        if power_mode in POWER_MODES:
            self.submit_at_command(at_command='ATS50={}'.format(
                power_mode), callback=self._cb_set_power_mode)
            # TODO: some risk that write fails and twin is no longer synchronized
            self.s_registers.register('S50').set(power_mode)
        else:
            self._log.error("Invalid power mode {}".format(power_mode))

    def _cb_set_power_mode(self, valid_response, responses, request):
        if valid_response:
            value = int(request.replace('ATS50=', ''))
            self._log.info("Updating S50 register value: {}".format(value))
            self.s_registers.register('S50').set(value)

    def get_power_mode(self, init=False):
        if not init:
            self._log.warning(
                "S50 value twin may be out of date requiring follow-up query")
            self.submit_at_command(at_command='ATS50?',
                                   callback=self._cb_get_power_mode)
        return self.s_registers.register('S50').get()

    def _cb_get_power_mode(self, valid_response, responses, request):
        if valid_response:
            value = int(responses[0])
            self._log.info("Updating S50 register value: {}".format(value))
            self.s_registers.register('S50').set(value)

    # ---------------------- S-REGISTER OPERATIONS -------------------------------------------------- #
    def set_s_register(self, register, value, callback=None, save=False):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _cb_set_s_register(self, valid_response, responses, request):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def get_s_register(self, register, callback):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _cb_get_s_register(self, valid_response, responses, request):
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _update_twin_parameters(self, register, value):
        # TODO: check which IdpModem parameters are affected by the read/write of s-Registers and update twin
        self._log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # --------------------- Generic functions that might be useful ----------------------------------- #
    def send_raw(self, command, callback):
        self._at_command_active_user_callback = callback
        self.submit_at_command(command, callback=self._cb_send_raw)

    def _cb_send_raw(self, valid_response, responses, request):
        if valid_response:
            self._at_command_active_user_callback(responses)
        else:
            self._log.error('Raw command {} response failed'.format(request))
        self._at_command_active_user_callback = None

    @staticmethod
    def get_bitmap(ordered_dict, key=None, value=None, binary=False):
        # TODO: Return the bitmap as either a binary string or integer
        if key is not None:
            if key not in ordered_dict:
                raise ValueError("key not found in OrderedDict")
        bitmap = '0b'
        for k in reversed(ordered_dict):
            bitmap += '1' if ordered_dict[k] else '0'
            if key is not None and key in ordered_dict and value is not None and isinstance:
                if k == key:
                    pass
        return bitmap if binary else int(bitmap, 2)

    @staticmethod
    def set_bitmap(ordered_dict, key, value, binary=False):
        # TODO: accept a list of key/value pairs to cycle through and return the bitmap binary string or integer value
        pass

    # --------------------- Logging operations ---------------------------------------------- #
    def log_at_config(self):
        """Logs/displays the current AT configuration options (e.g. CRC, Verbose, Echo, Quiet) on the console."""
        self._log.info("*** Modem AT Configuration ***")
        for (attr, value) in vars(self.at_config).items():
            self._log.info("*  {}={}".format(attr, 1 if value else 0))

    def log_sat_status(self):
        """Logs/displays the current satellite status on the console."""
        self._log.info("*** Satellite Status ***")
        for (attr, value) in vars(self.sat_status).items():
            self._log.info("*  {}={}".format(attr, value))

    def get_statistics(self):
        """
        Returns a ``dictionary`` of operating statistics for the modem/network.

        :return: ``dictionary`` of strings and KPI values containing key statistics

        """
        stat_list = [
            ('GNSS control (network) fixes', self.system_stats['nGNSS']),
            ('Average GNSS (network) time to fix [s]',
                self.system_stats['avgGNSSFixDuration']),
            ('Registrations', self.system_stats['nRegistration']),
            ('Average Registration time [s]',
                self.system_stats['avgRegistrationDuration']),
            ('BB acquisitions', self.system_stats['nBBAcquisition']),
            ('Average BB acquisition time [s]',
                self.system_stats['avgBBReacquireDuration']),
            ('Blockages', self.system_stats['nBlockage']),
            ('Average Blockage duration [s]',
                self.system_stats['avgBlockageDuration']),
            ('GNSS application fixes', self.gnss_stats['nGNSS']),
            ('Average GNSS (application) time to fix [s]',
                self.gnss_stats['avgGNSSFixDuration']),
            ('GNSS request timeouts', self.gnss_stats['timeouts']),
            ('Average AT response time [ms]',
                self.system_stats['avgATResponseTime_ms']),
            ('Total AT non-responses', self.at_timeouts_total),
            ('Total Mobile-Originated messages', self.mo_msg_count),
            ('Average Mobile-Originated message size [bytes]',
                self.system_stats['avgMOMsgSize']),
            ('Average Mobile-Originated message latency [s]',
                self.system_stats['avgMOMsgLatency_s']),
            ('Total Mobile-Terminated messages', self.mt_msg_count),
            ('Average Mobile-Terminated message size [bytes]',
                self.system_stats['avgMTMsgSize']),
            ('Average C/N0 [dB]', self.system_stats['avgCN0'])
        ]
        return stat_list

    def log_statistics(self):
        """Logs the modem/network statistics."""
        self._log.info("*" * 26 + " IDP MODEM STATISTICS " + "*" * 26)
        self._log.info("* Mobile ID: {}".format(self.mobile_id))
        self._log.info("* Hardware version: {}".format(self.hardware_version))
        self._log.info("* Firmware version: {}".format(self.software_version))
        self._log.info("* AT version: {}".format(self.at_version))
        for stat in self.get_statistics():
            self._log.info("* {}: {}".format(stat[0], str(stat[1])))
        self._log.info("*" * 75)


if __name__ == "__main__":
    SELFTEST_PORT = '/dev/ttyUSB0'
    modem = None
    try:
        modem = Modem(port=SELFTEST_PORT, debug=True)
        while (not modem.is_initialized or 
                modem.sat_status.ctrl_state == 'Stopped'):
            sleep(1)
        modem.tracking_setup(interval=30)
        textformat='Hello World'
        b64format='SGVsbG8gV29ybGQ='
        hexformat='48656c6c6f20576f726c64'
        test_msg = MobileOriginatedMessage(payload=b64format, 
                                            name='TEST', 
                                            data_format=FORMAT_B64, 
                                            msg_sin=255, msg_min=255)
        modem.message_send(test_msg)
        sleep(60)
        print('Test time completed')
    except Exception as e:
        print('*** EXCEPTION: {}'.format(e))
    finally:
        if modem is not None:
            modem.terminate()
