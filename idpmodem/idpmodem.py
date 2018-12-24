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
   * Internalize timer_threads for typical monitoring processes to callback to registered external functions
   * Improve threading to minimize need for re-entrant locks
   * Handle unsolicited modem output.  Can this happen while awaiting AT response??

"""
__version__ = "2.0.0"

import crcxmodem
from collections import OrderedDict
from headless import get_wrapping_log
from headless import RepeatingTimer
from headless import validate_serial_port
from headless import is_logger
import time
import datetime
import threading
import serial
import binascii
import base64
import sys
# import struct
# import json

PRIORITY_HIGH, PRIORITY_MEDH, PRIORITY_MEDL, PRIORITY_LOW = (1, 2, 3, 4)
FORMAT_TEXT, FORMAT_HEX, FORMAT_B64 = (1, 2, 3)
# Message States
UNAVAILABLE = 0
RX_COMPLETE = 2
RX_RETRIEVED = 3
TX_READY = 4
TX_SENDING = 5
TX_COMPLETE = 6
TX_FAILED = 7


class Modem(object):
    """
    Abstracts attributes and statistics related to an IDP modem

    :param serial_name: (string) the name of the serial port to use
    :param use_crc: (Boolean) to use CRC for long cable length
    :param log: an optional logger (preferably writing to a wrapping file)
    :param debug: Boolean option for verbose trace

    """
    # ------------- Modem built-in Enumerated Types ----------- #
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
        """
        Tracks the key operating parameters of the modem: registered, blocked, receive-only, waiting on Bulletin Board
        Tracks the current Satellite Control State represented in Trace Class 3, Subclass 1 index 23
        """
        def __init__(self):
            self.registered = False
            self.blocked = False
            self.rx_only = False
            self.bb_wait = False
            self.ctrl_state = 'Stopped'

    class _SRegisters(object):
        # Tuples: (name[0], default[1], read-only[2], range[3], description[4], note[5])
        register_definitions = [
            ('S0', 0, True, [0, 255], 'auto answer', 'unused'),
            ('S3', 13, False, [1, 127], 'command termination character', None),
            ('S4', 10, False, [0, 127], 'response formatting character', None),
            ('S5', 8, False, [0, 127], 'command line editing character', None),
            ('S6', 0, True, [0, 255], 'pause before dial', 'unused'),
            ('S7', 0, True, [0, 255], 'connection completion timeout', 'unused'),
            ('S8', 0, True, [0, 255], 'commia dial modifier time', 'unused'),
            ('S10', 0, True, [0, 255], 'automatic discovery delay', 'unused'),
            ('S31', 80, False, [10, 250], 'DOP threshold (x10)', None),
            ('S32', 25, False, [1, 1000], 'position accuracy threshold [m]', None),
            ('S33', 0, False, [0, 8], 'default dynamic platform model', None),
            ('S34', 7, True, [0, 255], 'Doppler dynamic platform model', 'Reserved'),
            ('S35', 0, False, [0, 255], 'static hold threshold [cm/s]', None),
            ('S36', 0, False, [-1, 480], 'standby timeout [min]', None),
            ('S37', 200, False, [1, 1000], 'speed accuracy threshold', None),
            ('S38', 32, True, [0, 0], 'reserved', None),
            ('S39', 0, False, [0, 2], 'GNSS mode', None),
            ('S40', 0, False, [0, 60], 'GNSS signal satellite detection timeout', None),
            ('S41', 180, False, [60, 1200], 'GNSS fix timeout', None),
            ('S42', 65535, False, [0, 65535], 'GNSS augmentation systems', None),
            ('S50', 0, False, [0, 9], 'power mode', None),
            ('S51', 0, False, [0, 6], 'wakeup interval', None),
            ('S52', 2500, True, [0, 2500], 'reserved', 'undocumented'),
            ('S53', 0, True, [0, 255], 'satcom control', None),
            ('S54', 0, True, [0, 0], 'satcom status', None),
            ('S55', 0, False, [0, 30], 'GNSS continuous mode', None),
            ('S56', 0, True, [0, 255], 'GNSS jamming status', None),
            ('S57', 0, True, [0, 255], 'GNSS jamming indicator', None),
            ('S60', 1, False, [0, 1], 'Echo', None),
            ('S61', 0, False, [0, 1], 'Quiet', None),
            ('S62', 1, False, [0, 1], 'Verbose', None),
            ('S63', 0, False, [0, 1], 'CRC', None),
            ('S64', 42, False, [0, 255], 'prefix character of CRC sequence', None),
            ('S70', 0, True, [0, 0], 'reserved', 'undocumented'),
            ('S71', 0, True, [0, 0], 'reserved', 'undocumented'),
            ('S80', 0, True, [0, 255], 'last error code', None),
            ('S81', 0, True, [0, 255], 'most recent result code', None),
            ('S85', 22, True, [0, 0], 'temperature', None),
            ('S88', 0, False, [0, 65535], 'event notification control', None),
            # ('S89', 0, False, [0, 65535], 'event notification status', None),
            ('S90', 0, False, [0, 7], 'capture trace define - class', None),
            ('S91', 0, False, [0, 31], 'capture trace define - subclass', None),
            ('S92', 0, False, [0, 255], 'capture trace define - initiate', None),
            ('S93', 0, True, [0, 255], 'captured trace property - data size', None),
            ('S94', 0, True, [0, 255], 'captured trace property - signed indicator', None),
            ('S95', 0, True, [0, 255], 'captured trace property - mobile ID', None),
            ('S96', 0, True, [0, 255], 'captured trace property - timestamp', None),
            ('S97', 0, True, [0, 255], 'captured trace property - class', None),
            ('S98', 0, True, [0, 255], 'captured trace property - subclass', None),
            ('S99', 0, True, [0, 255], 'captured trace property - severity', None),
            ('S100', 0, True, [0, 255], 'captured trace data 0', None),
            ('S101', 0, True, [0, 255], 'captured trace data 1', None),
            ('S102', 0, True, [0, 255], 'captured trace data 2', None),
            ('S103', 0, True, [0, 255], 'captured trace data 3', None),
            ('S104', 0, True, [0, 255], 'captured trace data 4', None),
            ('S105', 0, True, [0, 255], 'captured trace data 5', None),
            ('S106', 0, True, [0, 255], 'captured trace data 6', None),
            ('S107', 0, True, [0, 255], 'captured trace data 7', None),
            ('S108', 0, True, [0, 255], 'captured trace data 8', None),
            ('S109', 0, True, [0, 255], 'captured trace data 9', None),
            ('S110', 0, True, [0, 255], 'captured trace data 10', None),
            ('S111', 0, True, [0, 255], 'captured trace data 11', None),
            ('S112', 0, True, [0, 255], 'captured trace data 12', None),
            ('S113', 0, True, [0, 255], 'captured trace data 13', None),
            ('S114', 0, True, [0, 255], 'captured trace data 14', None),
            ('S115', 0, True, [0, 255], 'captured trace data 15', None),
            ('S116', 0, True, [0, 255], 'captured trace data 16', None),
            ('S117', 0, True, [0, 255], 'captured trace data 17', None),
            ('S118', 0, True, [0, 255], 'captured trace data 18', None),
            ('S119', 0, True, [0, 255], 'captured trace data 19', None),
            ('S120', 0, True, [0, 255], 'captured trace data 20', None),
            ('S121', 0, True, [0, 255], 'captured trace data 21', None),
            ('S122', 0, True, [0, 255], 'captured trace data 22', None),
            ('S123', 0, True, [0, 255], 'captured trace data 23', None),
        ]

        class SRegister(object):
            def __init__(self, name, default, read_only, low, high, description, note=None):
                self.name = name
                self.default = default
                self.value = default
                self.read_only = read_only
                self.rng = range(low, high)
                self.description = description
                self.note = note

            def get(self):
                return self.value

            def set(self, value):
                error = None
                if not self.read_only:
                    if value in self.rng:
                        self.value = value
                    else:
                        error = "Attempt to set {} out of range.".format(self.name)
                else:
                    error = "Attempt to write read-only register {}".format(self.name)
                return error if error is not None else value

            def read(self):
                pass

        def __init__(self, parent):
            self.parent = parent
            self.log = parent.log
            self.log.debug("Initializing S-Registers")
            self.s_registers = []
            for tup in self.register_definitions:
                reg = self.SRegister(name=tup[0], default=tup[1], read_only=tup[2], low=tup[3][0], high=tup[3][1],
                                     description=tup[4], note=tup[5])
                self.s_registers.append(reg)

        def register(self, s_register):
            for reg in self.s_registers:
                if reg.name == s_register:
                    return reg

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
    
    class _PendingAtCommand(object):
        """
        A private object to track the sending of AT commands and route responses to a callback

        :param at_command: (string) command being sent, not including checksum
        :param callback: (function) a callback function that will receive:

           * valid_response (boolean)
           * responses (list)

        :param timeout: (integer) seconds to wait for response
        :param retries: (integer) number of retries if unsuccessful

        """
        def __init__(self, at_command, callback, timeout=10, retries=0):
            """

            :param at_command: (string) command being sent, not including checksum
            :param callback: (function) a callback function that will receive
            :param timeout: (integer) seconds to wait for response
            :param retries: (integer) number of retries if unsuccessful
            """
            self.command = at_command
            self.submit_time = time.time()
            self.send_time = None
            self.response_time = None
            self.echo_received = False
            self.result = None
            self.result_code = None
            self.error = None
            self.response_raw = ""
            self.responses = []
            self.response_time = None
            self.response_crc = None
            self.crc_ok = True
            self.timeout = timeout
            self.timed_out = False
            self.callback = callback
            self.retries = retries

    class _PendingMoMessage(object):
        """
        A private class for managing a queue of Mobile-Originated messages submitted via AT command

        :param message: (MobileOriginatedMessage) to be queued
        :param callback: (function) that will receive notification when the message completes/fails

        """
        def __init__(self, message, callback=None):
            """

            :param message:
            :param callback:
            """
            self.message = message
            self.q_name = str(int(time.time()))[1:9]
            self.submit_time = time.time()
            self.complete_time = None
            self.failed = False
            self.callback = callback

    class _PendingMtMessage(object):
        """

        :param message: (MobileTerminatedMessage) object
        :param q_name: (string) the name assigned by the modem
        :param sin: (int) Service Identifier Number
        :param size: (int) bytes in the message

        """
        def __init__(self, message, q_name, sin, size):
            """

            :param message:
            :param q_name:
            :param sin:
            :param size:
            """
            self.message = message
            self.q_name = q_name
            self.sin = sin
            self.size = size
            self.received_time = time.time()
            self.retrieved_time = None
            self.failed = False
            self.state = RX_COMPLETE
            self.callback = None

    class _AtStatistics(object):
        def __init__(self):
            # 'lastResTime': 0,
            # 'avgResTime': 0,
            self.last_response_time = 0
            self.avg_response_time = 0

    def __init__(self, serial_name='/dev/ttyUSB0', auto_monitor=True, use_crc=False, log=None, debug=False):
        """
        Initializes attributes and pointers used by Modem class methods.

        :param serial_name: (string) the name of the serial port to use
        :param auto_monitor: (boolean) enables automatic monitoring of satellite events
        :param use_crc: (Boolean) to use CRC for long cable length
        :param log: (logging.Logger) an optional logger, preferably writing to a wrapping file
        :param debug: (Boolean) option for verbose trace

        """
        self.start_time = str(datetime.datetime.utcnow())
        if is_logger(log):
            self.log = log
        else:
            self.log = get_wrapping_log(logfile=log, debug=debug)
        self.debug = debug
        # serial and connectivity configuration and statistics
        self.serial_port = self._init_serial(serial_name)
        self.is_connected = False
        self.connects = 0
        self.disconnects = 0
        self.at_cmd_stats = self._AtStatistics()
        self.at_connect_attempts = 0
        self.total_at_connect_attempts = 0
        self.at_timeouts = 0
        self.at_timeouts_total = 0
        # modem parameters
        self.mobile_id = 'unknown'
        self.hardware_version = 'unknown'
        self.software_version = 'unknown'
        self.at_version = 'unknown'
        self.is_initialized = False
        self.s_registers = self._SRegisters(parent=self)
        self.at_config = self._AtConfiguration()
        self.use_crc = use_crc
        self.crc_errors = 0
        self.sat_status = self._SatStatus()
        self.hw_event_notifications = self._init_hw_event_notifications()
        self.wakeup_interval = self.wakeup_intervals['5 seconds']
        self.power_mode = self.power_modes['Mobile Powered']
        self.asleep = False
        self.antenna_cut = False
        self.stats_start_time = 0
        self.system_stats = self._init_system_stats()
        self.gnss_mode = self.gnss_modes['GPS']
        self.gnss_continuous = 0
        self.gnss_dpm_mode = self.gnss_dpm_modes['Portable']
        self.gnss_stats = self._init_gnss_stats()
        self.gpio = self._init_gpio()
        self.event_callbacks = self._init_event_callbacks()
        # AT command queue
        self.pending_at_commands = []
        self.active_at_command = None
        # Message queues
        self.mo_msg_count = 0
        self.mo_msg_queue = []
        self.mt_msg_count = 0
        self.mt_msg_queue = []
        # --- Serial processing threads ---
        self._terminate = False
        self.daemon_threads = []
        self.thread_com_listener = threading.Thread(name='com_listener', target=self._listen_serial)
        self.thread_com_listener.daemon = True
        self.daemon_threads.append(self.thread_com_listener.name)
        self.thread_com_listener.start()
        self.thread_com_at_queue = threading.Thread(name='at_queue', target=self._process_pending_at_command)
        self.thread_com_at_queue.daemon = True
        self.daemon_threads.append(self.thread_com_listener.name)
        self.thread_com_at_queue.start()
        # --- Timer threads for communication establishment and monitoring
        # self.thread_lock = threading.RLock()   # TODO: deprecate
        self.timer_threads = []
        self.com_connect_interval = 6
        self.com_monitor_interval = 1
        self.thread_com_connect = RepeatingTimer(seconds=self.com_connect_interval, name='com_connect',
                                                 callback=self._com_connect, defer=False)
        self.timer_threads.append(self.thread_com_connect.name)
        self.thread_com_connect.start_timer()
        self.thread_com_monitor = RepeatingTimer(seconds=self.com_monitor_interval, name='com_monitor',
                                                 callback=self._com_monitor)
        self.timer_threads.append(self.thread_com_monitor.name)
        # --- Timer threads for self-monitoring
        self.autonomous = auto_monitor
        self.sat_status_interval = 5
        self.sat_mt_message_interval = 5
        self.sat_events_interval = 1
        self.sat_mo_message_interval = 5
        if self.autonomous:
            self.thread_sat_status = RepeatingTimer(seconds=self.sat_status_interval,
                                                    name='sat_status_monitor', callback=self._check_sat_status)
            self.timer_threads.append(self.thread_sat_status.name)
            self.thread_mt_monitor = RepeatingTimer(seconds=self.sat_mt_message_interval,
                                                    name='sat_mt_message_monitor', callback=self.check_mt_messages)
            self.timer_threads.append(self.thread_mt_monitor.name)
            self.thread_event_monitor = RepeatingTimer(seconds=self.sat_events_interval,
                                                       name='sat_events_monitor', callback=self.check_events)
            self.timer_threads.append(self.thread_event_monitor.name)
            self.thread_mo_monitor = RepeatingTimer(seconds=self.sat_mo_message_interval,
                                                    name='sat_mo_message_monitor', callback=self.check_mo_messages)
            self.timer_threads.append(self.thread_mo_monitor.name)

    def terminate(self):
        self.log.debug("Terminated by external call {}".format(sys._getframe(1).f_code.co_name))
        end_time = str(datetime.datetime.utcnow())
        self._terminate = True
        if self.autonomous:
            for t in threading.enumerate():
                if t.name in self.timer_threads:
                    self.log.debug("Killing thread {}".format(t.name))
                    t.stop_timer()
                    t.terminate()
                    t.join()
                elif t.name in self.daemon_threads:
                    self.log.debug("Killing thread {}".format(t.name))
                    t.join()
        try:
            self.serial_port.close()
        except serial.SerialException as e:
            self._handle_error(e)
        self.log.info("*** Statistics from {} to {} ***".format(self.start_time, end_time))
        self.log_statistics()

    def _handle_error(self, error_str):
        error_str = error_str.replace(',', ';')
        self.log.error(error_str)
        # TODO: may not be best practice to raise a ValueError in all cases
        raise ValueError(error_str)

    def _init_serial(self, serial_name, baud_rate=9600):
        """
        Initializes the serial port for modem communications
        :param serial_name: (string) the port name on the host
        :param baud_rate: (integer) baud rate, default 9600 (8N1)
        """
        if isinstance(serial_name, str):
            is_valid_serial, details = validate_serial_port(serial_name)
            if is_valid_serial:
                try:
                    serial_port = serial.Serial(port=serial_name, baudrate=baud_rate, bytesize=serial.EIGHTBITS,
                                                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                                timeout=None, write_timeout=0,
                                                xonxoff=False, rtscts=False, dsrdtr=False)
                    serial_port.flush()
                    self.log.info("Connected to {} at {} baud".format(details, baud_rate))
                    return serial_port
                except serial.SerialException as e:
                    self._handle_error("Unable to open {} - {}".format(details, e))
            else:
                self._handle_error("Invalid serial port {} - {}".format(serial_name, details))
        else:
            self._handle_error("Invalid type passed as serial_port - requires string name of port")

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
    def _init_at_stats():
        # TODO: track response times per AT command type
        self.at_cmd_stats = {
            'lastResTime': 0,
            'avgResTime': 0,
        }

    @staticmethod
    def _init_gpio():
        gpio = {
            "event_notification": None,
            "reset_out": None,
            "pps": None,
            "reset_in": None,
        }
        return gpio

    def _init_event_callbacks(self):
        event_callbacks = {}
        for event in self.events:
            event_callbacks[event] = None
        return event_callbacks

    def register_event_callback(self, event, callback):
        """
        TODO: docstring
        :param event:
        :param callback:
        :return:
        """
        if event in self.events:
            if callback is not None:
                self.event_callbacks[event] = callback
                return True, None
            else:
                return False, "No callback defined"
        else:
            self.log.error("Invalid attempt to register callback event {}".format(event))
            return False, "Invalid event"

    def register_mt_push(self, sin, min, data_format=FORMAT_B64, codec=None):
        self.log.warning("register_mt_push FUNCTION NOT IMPLEMENTED")

    def register_mt_notify(self, sin, min):
        self.log.warning("register_mt_notify FUNCTION NOT IMPLEMENTED")

    def _on_initialized(self):
        if self.autonomous:
            # self.thread_sat_status.start_timer()
            self.thread_mt_monitor.start_timer()
            # self.thread_mo_monitor.start_timer()
            # self.thread_event_monitor.start_timer()
        else:
            self.log.info("Automonous mode disabled, user application must query modem actively")

    # ---------------- Connection Management ------------------------- #
    def _com_connect(self):
        """
        Called on a repeating timer on power up or after connection is lost,
        attempts to establish and restore saved config using ATZ.
        """
        if not self.is_connected and self.active_at_command is None:
            self.at_connect_attempts += 1
            self.total_at_connect_attempts += 1
            timeout = int(self.com_connect_interval / 2)
            self.submit_at_command(at_command='ATZ', callback=self._cb_com_connect, timeout=timeout)

    def _cb_com_connect(self, valid_response, responses, request):
        """
        Callback from attempt to establish connection. Sets modem connected state and calls on_connect routine.

        :param valid_response: (boolean) successful command response
        :param responses: (list) string responses to the ATZ command
        """
        # TODO: validate that timeouts and error handling is managed by other functions
        if valid_response:
            self.is_connected = True
            self.connects += 1
            self.log.info("Modem connected after {} attempts: {}".format(self.at_connect_attempts, responses))
            self.at_connect_attempts = 0
            self._on_connect()
        else:
            self.log.debug("Modem connect attempt {} failed".format(self.at_connect_attempts))
            self.at_connect_attempts += 1

    def _on_connect(self):
        """
        Stops trying to establish communications, starts monitoring for communications loss and calls back connect event
        """
        if self.autonomous:
            self.thread_com_connect.stop_timer()
            self.thread_com_monitor.start_timer()
        if not self._terminate:
            self._init_modem()
        if self.event_callbacks['connect'] is not None:
            self.event_callbacks['connect']()

    def _com_monitor(self, disconnect_timeouts=3):
        """
        Periodically called by a timer thread to check if too many timeouts have occurred indicating communication lost.
        Calls on_disconnect routine.

        :param disconnect_timeouts: (integer) number of time-outs before disconnect is declared
        """
        # self.log.debug("Monitoring communication: {} timeouts".format(self.at_timeouts))
        if self.at_timeouts >= disconnect_timeouts and self.is_connected:
            self.is_connected = False
            self.disconnects += 1
            self.log.warning("AT responses timed out {} times - attempting to reconnect".format(self.at_timeouts))
            self._on_disconnect()

    def _on_disconnect(self):
        """
        Stops monitoring modem operations and communications, and starts trying to re-connect.
        Calls back the disconnect event.
        """
        if self.autonomous:
            self.thread_com_monitor.stop_timer()
            self.thread_sat_status.stop_timer()
            self.thread_mt_monitor.stop_timer()
            self.thread_event_monitor.stop_timer()
            self.thread_com_connect.start_timer()
        self.is_initialized = False
        if self.event_callbacks['disconnect'] is not None:
            self.event_callbacks['disconnect']()

    # ----------------------- SERIAL PORT DATA PROCESSING --------------------- #
    def _listen_serial(self):
        self.log.debug("Listening on serial")
        CHAR_WAIT = 0.05
        ser = self.serial_port
        read_str = ""
        parsing_unsolicited = False
        parsing_at_response = False
        at_tick = 0
        while ser.isOpen() and not self._terminate:
            c = None
            if ser.inWaiting() > 0:
                c = ser.read(1)
            else:
                if not parsing_at_response and self.active_at_command is not None:
                    self.log.debug("{} command pending...".format(self.active_at_command.command))
                    parsing_at_response = True
                if parsing_at_response:
                    at_cmd = self.active_at_command
                    if time.time() - at_cmd.submit_time > at_cmd.timeout:
                        parsing_at_response = False
                        at_tick = 0
                        self._on_at_timeout(at_cmd)
                    else:
                        if time.time() - at_cmd.submit_time >= at_tick + 1:
                            at_tick += 1
                            self.log.debug("Waiting for {} response - tick={}".format(at_cmd.command, at_tick))
                time.sleep(CHAR_WAIT)
            if c is not None:
                if parsing_at_response or not parsing_unsolicited and self.active_at_command is not None:
                    if not parsing_at_response:
                        self.log.debug("Parsing started for {}".format(self.active_at_command.command))
                        parsing_at_response = True
                    read_str, complete = self._parse_at_response(read_str, c)
                    if complete:
                        parsing_at_response = False
                        at_tick = 0
                        read_str = ""
                        self._on_at_response(self.active_at_command)
                else:
                    parsing_unsolicited = True
                    read_str, complete = self._parse_unsolicited(read_str, c)
                    if complete:
                        parsing_unsolicited = False
                        read_str = ""
                        self._on_unsolicited_serial(read_str)

    def _parse_at_response(self, read_str, c):
        """
        Parses the next character read from the serial port, as an AT command response.

        :param read_str: (string) the string read thus far
        :param c: the next character read from the serial port
        :returns:

           * read_str: (string) read thus far, updated with c
           * response_complete: (Boolean) indicates if the response is complete for processing
        """
        ser = self.serial_port
        read_str += c
        self.active_at_command.response_raw += c
        response_complete = False
        if c == '\r':
            # cases <echo><cr>
            # or <cr>...
            # or <numeric code><cr> (verbose off, no crc)
            if self.active_at_command.command in read_str:
                # case <echo><cr>
                if self.active_at_command.command.upper() == 'ATE0':
                    self.at_config.echo = False
                    self.log.debug("ATE0 (echo disable) requested - takes effect for next AT command")
                else:
                    self.at_config.echo = True
                if not self.active_at_command.echo_received:
                    self.log.debug("Echo {} received - removing from raw response".format(read_str.strip()))
                    self.active_at_command.echo_received = True
                else:
                    self.log.warning("Echo {} received more than once - removing from raw message"
                                     .format(read_str.strip()))
                self.active_at_command.response_raw = self.active_at_command.response_raw.replace(read_str, '')
                # <echo><cr> will be not be followed by <lf>
                # can be followed by <text><cr><lf>
                # or <cr><lf><text><cr><lf>
                # or <numeric code><cr>
                # or <cr><lf><verbose code><cr><lf>
                read_str = ""  # clear for next line of parsing
            elif ser.inWaiting() == 0 and read_str.strip() != '':
                if read_str.strip() != '0' and self.active_at_command.command != 'ATV0' and self.at_config.verbose:
                    # case <cr><lf><text><cr>...<lf> e.g. delay between NMEA sentences
                    # or Quiet mode? --unsupported, suppresses result codes
                    self.log.debug("Assuming delay between <cr> and <lf> of Verbose response...waiting")
                else:
                    # case <numeric code><cr> since all other alternatives should have <lf> or other pending
                    if not self.at_config.verbose:
                        self.log.debug("Assuming receipt of <numeric code = {}><cr> with Verbose undetected"
                                       .format(read_str.strip()))
                        self.at_config.verbose = False
                    self.active_at_command.result_code = read_str.strip()
                    response_complete = True
                    # else keep parsing next character
        elif c == '\n':
            # case <cr><lf>
            # or <text><cr><lf>
            # or <cr><lf><text><cr><lf>
            # or <cr><lf><verbose code><cr><lf>
            # or <*crc><cr><lf>
            if 'OK' in read_str or 'ERROR' in read_str:
                # <cr><lf><verbose code><cr><lf>
                self.active_at_command.result = read_str.strip()
                if ser.inWaiting() == 0:  # no checksum pending...response complete
                    response_complete = True
                else:
                    read_str = ""  # continue parsing next line (checksum)
            elif '*' in read_str and len(read_str.strip()) == 5:
                # <*crc><cr><lf>
                self.at_config.crc = True
                self.active_at_command.response_crc = read_str.replace('*', '').strip()
                self.log.debug("Found CRC {} - removing from raw response".format(read_str.strip()))
                self.active_at_command.response_raw = self.active_at_command.response_raw.replace(read_str, '')
                response_complete = True
            else:
                # case <cr><lf>
                # or <text><cr><lf>
                # or <cr><lf><text><cr><lf>
                if read_str.strip() == '':
                    # <cr><lf> empty line...not done parsing yet
                    self.at_config.verbose = True
                else:
                    if read_str.strip() != '':  # don't add empty lines
                        self.active_at_command.responses.append(read_str.strip())  # don't include \r\n in callback
                    read_str = ""  # clear for next line parsing
        return read_str, response_complete

    def _parse_unsolicited(self, read_str, c):
        read_str += c
        unsolicited_complete = False
        if c == '\r':
            self.log.info("Received unsolicited serial data: {}"
                          .format(read_str.replace('\n', '<lf>').replace('\r', '<cr>')))
            if self.event_callbacks['unsolicited_serial'] is not None:
                self.event_callbacks['unsolicited_serial'](read_str)
            read_str = ""
            unsolicited_complete = True
        return read_str, unsolicited_complete

    def _on_unsolicited_serial(self, read_str):
        self.event_callbacks['unsolicited_serial'](read_str)

    # ---------------------- AT Command handling -------------------------------------------------- #
    @staticmethod
    def get_crc(at_cmd):
        """
        Returns the CRC-16-CCITT (initial value 0xFFFF) checksum using crcxmodem module.

        :param at_cmd: the AT command to calculate CRC on
        :return: the CRC for the AT command

        """
        return '{:04X}'.format(crcxmodem.crc(at_cmd, 0xffff))

    def submit_at_command(self, at_command, callback, timeout=10, retries=0, jump_queue=False):
        """
        Creates and enqueues an AT command with a defined callback for the response

        :param at_command: properly formatted AT command
        :param callback: the function to call back with the response
        :param timeout: optional integer seconds to wait for the command response
        :param retries: optional number of retries on timeout or error
        :param jump_queue: optional Boolean if the message should be placed at front of queue
        """
        command = self._PendingAtCommand(at_command=at_command, callback=callback, timeout=timeout, retries=retries)
        self.log.debug("Submitting command {} at {} with timeout {}s calling back to {}"
                       .format(at_command, command.submit_time, timeout,
                               callback.__name__ if callback is not None else None))
        if jump_queue:
            self.pending_at_commands.insert(0, command)
        else:
            self.pending_at_commands.append(command)

    def _process_pending_at_command(self):
        """Checks the queue of pending AT commands and sends on serial if one is pending and none are active"""
        while self.serial_port.isOpen() and not self._terminate:
            if len(self.pending_at_commands) > 0:
                if self.active_at_command is None:
                    # for cmd in self.pending_at_commands:
                    #     self.log.debug("[{}]: {}".format(self.pending_at_commands.index(cmd), cmd.command))
                    at_cmd = self.pending_at_commands[0]
                    self.log.debug("{} Pending commands - processing: {}"
                                   .format(len(self.pending_at_commands), at_cmd.command))
                    if self.at_config.crc:
                        to_send = at_cmd.command + ('*'+self.get_crc(at_cmd.command) if self.at_config.crc else '')
                    else:
                        to_send = at_cmd.command
                    if "AT%CRC=1" in at_cmd.command.upper():
                        self.at_config.crc = True
                        self.log.debug("CRC enabled for next command")
                    elif "AT%CRC=0" in at_cmd.command.upper():
                        self.at_config.crc = False
                        self.log.debug("CRC disabled for next command")
                    at_cmd.send_time = time.time()
                    self.log.debug("Sending {} at {} with timeout {} seconds"
                                   .format(to_send, at_cmd.send_time, at_cmd.timeout))
                    self.serial_port.write(to_send + '\r')
                    self.active_at_command = at_cmd
                # else:
                #     self.log.debug("Processing AT command: {}".format(self.active_at_command.command))

    def _on_at_response(self, response):
        """
        Called when a response parsing completes. Validates CRC if present.
        If a response ERROR is detected, requests the result code immediately jumping the AT queue.
        Updates debug statistics and sends the final completed response for processing.

        :param response: (_PendingAtCommand) the current pending command
        """
        self.log.debug("AT response received (timeouts reset): {}".format(vars(response)))
        self.at_timeouts = 0
        response.response_time = time.time()
        if response.response_crc is not None:
            if not self.at_config.crc:
                self.log.warning("Unexpected CRC response received, setting CRC flag True")
                self.at_config.crc = True
            self.log.debug("Raw response to validate CRC: {}"
                           .format(response.response_raw.replace('\r', '<cr>').replace('\n', '<lf>')))
            expected_crc = self.get_crc(response.response_raw)
            self.log.debug("Expected CRC: *{}".format(expected_crc))
            if response.response_crc != expected_crc:
                response.crc_ok = False
                self.crc_errors += 1
                self.log.warning("Bad CRC received: *{} - expected: *{}".format(response.response_crc, expected_crc))
                if response.result_code == '100' or not self.at_config.crc and response.response_crc is not None:
                    self.log.info("CRC found on response but not explicitly configured...capturing config")
                    self.at_config.crc = True
        if response.result == 'ERROR' or response.result_code == '4':
            self.log.warning("Error detected on response, checking last error code")
            self.submit_at_command(at_command='ATS80?', callback=self._cb_get_result_code, jump_queue=True)
        self._update_stats_at_response(response)
        self._complete_pending_command(response)

    def _cb_get_result_code(self, valid_response, responses, request):
        """
        Called back by a request for last error code (ATS80?), populates the result code and human readable
        :param valid_response: (boolean) was the ATS80? response valid
        :param responses: (_PendingAtCommand) the current pending command
        :return:
        """
        if valid_response:
            result_code = responses[0]
            self.pending_at_commands[1].result_code = result_code
            self.pending_at_commands[1].error = self.at_err_result_codes[str(result_code)]
        else:
            self.log.error("Unhandled exception: {} {}".format(request, responses))

    def _on_at_timeout(self, response):
        """
        Called if a timeout happens while waiting on AT command response. Increments a timeout counter and
        immediately checks if enough timeouts have happened to imply a disconnect.
        Then passes the command for final handling.

        :param response: (_PendingAtCommand) the current pending command
        """
        self.log.warning("Command {} timed out after {} seconds - data may be present in serial adapter buffer".
                         format(response.command, response.timeout))
        if self.serial_port.out_waiting > 0:
            self.log.debug("Found data in serial output buffer, resetting")
            self.serial_port.reset_output_buffer()
        response.timed_out = True
        self.at_timeouts += 1
        self.at_timeouts_total += 1
        self._com_monitor()
        self._complete_pending_command(response)

    def _complete_pending_command(self, response):
        """
        Handles final processing of a pending command, either re-attempting or calling back with response details

        :param response: (_PendingAtCommand) the current pending command
        :return: calls back with:

           * (boolean) valid_response flag
           * (string) error message or (list) of valid response strings
           * (string) the AT command request to correlate to

        """
        complete = False
        if response in self.pending_at_commands:
            discard = self.pending_at_commands.pop(0)
            if discard is not None:
                self.log.debug("Pending AT command buffer FIFO popped ({}) - {} pending AT commands"
                               .format(discard.command, len(self.pending_at_commands)))
            else:
                self.log.error("Tried to pop pending command from AT queue but got nothing")
            if (response.timed_out or not response.crc_ok) and response.retries > 0:
                self.log.info("Retrying command {}".format(response.command))
                response.retries -= 1
                self.submit_at_command(at_command=response.command, callback=response.callback,
                                       timeout=response.timeout, retries=response.retries - 1, jump_queue=True)
            else:
                complete = True
        else:
            self.log.warning("Did not find {} in pending_at_commands".format(response.command))
        if len(self.pending_at_commands) > 0:
            self.log.debug("Next pending command: {}".format(self.pending_at_commands[0].command))
            # self.active_at_command = self.pending_at_commands[0]
        else:
            self.log.debug("No pending AT commands")
        self.log.debug("Clearing active command")
        self.active_at_command = None
        if complete:
            if response.callback is not None:
                self.log.debug("Calling back to {}".format(response.callback.__name__))
                if response.timed_out:
                    response.callback(False, "TIMED_OUT", response.command)
                elif not response.crc_ok:
                    response.callback(False, "RESPONSE_CRC_ERROR", response.command)
                elif response.error is not None:
                    response.callback(False, response.error, response.command)
                else:
                    response.callback(True, response.responses, response.command)
            else:
                self.log.warning("No callback defined for command {}".format(response.command))

    def _update_stats_at_response(self, response):
        """
        Updates the last and average AT command response time statistics.

        :param response: (object) the response to the AT command that was sent

        """
        at_response_time_ms = int(response.response_time - response.send_time) * 1000
        self.system_stats['lastATResponseTime_ms'] = at_response_time_ms
        self.log.debug("Response time for {}: {} [ms]".format(response.command, at_response_time_ms))
        if self.system_stats['avgATResponseTime_ms'] == 0:
            self.system_stats['avgATResponseTime_ms'] = at_response_time_ms
        else:
            self.system_stats['avgATResponseTime_ms'] = \
                int((self.system_stats['avgATResponseTime_ms'] + at_response_time_ms) / 2)
        # TODO: categorize AT commands for characterization
        if 'AT%GPS' in response.command.upper():
            self.log.debug("Get GNSS information processed")
        elif 'AT%MGFG' in response.command.upper():
            self.log.debug("Get To-Mobile message processed")
        elif 'ATS' in response.command.upper() and '?' in response.command:
            self.log.debug("S-register query {} processed".format(response.command[4:].replace('?', '')))
        elif 'AT%EVMON' in response.command.upper():
            self.log.debug("Event Log Monitor {} processed".format(response.command[10:]))
        elif 'AT%EVNT' in response.command.upper():
            self.log.debug("Event Log Get {} processed".format(response.command[9:]))

    # --------------- Modem initialization & twinning ----------------------------- #
    def _init_modem(self, step=1):
        # TODO: optimize to a single command query/response
        self.log.debug("Initializing modem...step {}".format(step))
        if step == 1:   # Restore default configuration
            # TODO: consider using Factory defaults (AT&F) instead of NVM for first initialization?
            self.submit_at_command(at_command='AT&V', callback=self._cb_get_config, timeout=3)
        elif step == 2:   # enable CRC if explicitly during object creation (used for long serial cable)
            if self.use_crc and not self.at_config.crc:
                self.submit_at_command(at_command="AT%CRC=1", callback=self._cb_configure_crc, timeout=3)
            elif not self.use_crc and self.at_config.crc:
                self.submit_at_command(at_command='AT%CRC=0', callback=self._cb_configure_crc, timeout=3)
            else:
                self.log.info("CRC already {} in configuration".format("enabled" if self.use_crc else "disabled"))
                self._init_modem(step=3)
        elif step == 3:   # enable Verbose since response codes are only OK (0) or ERROR (4)
            if not self.at_config.verbose:
                self.submit_at_command(at_command='ATV1', callback=self._cb_configure_verbose, timeout=3)
            else:
                self._init_modem(step=4)
        elif step == 4:   # get mobileID, versions
            self.submit_at_command(at_command='AT+GSN;+GMR', callback=self._cb_get_modem_info, timeout=3)
        elif step == 5:   # get key parameters from S-registers
            self.log.warning("TODO: get S-register values for notifications, wakeup, power mode, gnss")
            # TODO: get S registers - ideally generic handling rather than individual callbacks
            self.get_event_notification_control(init=True)
            # self.get_wakeup_interval()
            # self.get_power_mode()
            # self.get_gnss_mode()
            # self.get_gnss_continuous()
            # self.get_gnss_dpm()
            self._init_modem(step=6)
        elif step == 6:   # save config to NVM
            self.submit_at_command(at_command='AT&W', callback=None, timeout=3)
            self.log.info("Initialization complete")
            self.is_initialized = True
            self._on_initialized()
        else:
            self.log.warning("Modem initialization called with invalid step {}".format(step))

    def _cb_get_config(self, valid_response, responses, request):
        """
        Called back by initialization reading AT&V get current and saved configuration.
        Configures the AT mode parameters (Echo, Verbose, CRC, etc.) and S-registers twin.
        If successful, increments and calls the next initialization step.

        :param valid_response: (boolean)
        :param responses: (_PendingAtCommand)

        """
        step = 1
        success = False
        if valid_response:
            success = True
            self.log.debug("Processing AT&V response: {}".format(responses))
            # active_header = responses[0]
            at_config = responses[1].split(" ")
            for param in at_config:
                if param[0].upper() == 'E':
                    echo = bool(int(param[1]))
                    if self.at_config.echo != echo:
                        self.log.warning("Configured Echo setting does not match target {}"
                                         .format(self.at_config.echo))
                        self.at_config.echo = echo
                elif param[0].upper() == 'Q':
                    quiet = bool(int(param[1]))
                    if self.at_config.quiet != quiet:
                        self.log.warning("Configured Quiet setting does not match target {}"
                                         .format(self.at_config.quiet))
                        self.at_config.quiet = quiet
                elif param[0].upper() == 'V':
                    verbose = bool(int(param[1]))
                    if self.at_config.verbose != verbose:
                        self.log.warning("Configured Verbose setting does not match target {}"
                                         .format(self.at_config.verbose))
                        self.at_config.verbose = verbose
                elif param[0:4].upper() == 'CRC=':
                    crc = bool(int(param[4]))
                    if self.at_config.crc != crc:
                        self.log.warning("Configured CRC setting does not match target {}"
                                         .format(self.at_config.crc))
                        self.at_config.crc = crc
                else:
                    self.log.warning("Unknown config parameter: {}".format(param))
            reg_config = responses[2].split(" ")
            for c in reg_config:
                name = c.split(":")[0]
                reg = self.s_registers.register(name)
                value = int(c.split(":")[1])
                if value != reg.default:
                    self.log.warning("{} value {} does not match target {}".format(name, value, reg.default))
                    reg.set(value)
        self._init_modem(step=step+1) if success else self._init_modem(step=step)

    def _cb_get_modem_info(self, valid_response, responses, request):
        """
        Called back by initialization reading AT+GSN;+GMR get mobile ID and versions.
        If successful, increments and calls the next initialization step.

        :param valid_response: (boolean)
        :param responses: (_PendingAtCommand)

        """
        step = 4
        success = False
        if valid_response:
            mobile_id = responses[0].lstrip('+GSN:').strip()
            if mobile_id != '':
                self.log.info("Mobile ID: %s" % mobile_id)
                self.mobile_id = mobile_id
                success = True
            else:
                self.log.warning("Mobile ID not returned...retrying")
            fw_ver, hw_ver, at_ver = responses[1].lstrip('+GMR:').strip().split(",")
            self.log.info("Versions - Hardware: {} | Firmware: {} | AT: {}".format(hw_ver, fw_ver, at_ver))
            self.hardware_version = hw_ver if hw_ver != '' else 'unknown'
            self.software_version = fw_ver if fw_ver != '' else 'unknown'
            self.at_version = at_ver if at_ver != '' else 'unknown'
        self._init_modem(step=step+1) if success else self._init_modem(step=step)

    def _cb_configure_crc(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(sys._getframe().f_code.co_name))

    def _cb_configure_verbose(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(sys._getframe().f_code.co_name))

    # ----------------------- SATELLITE STATUS MONITORING ----------------------------------------- #
    def _check_sat_status(self):
        self.log.debug("Monitoring satellite status - current status: {}".format(self.sat_status.ctrl_state))
        # S122: satellite trace status
        # S116: C/N0
        self.submit_at_command('ATS90=3 S91=1 S92=1 S122? S116?', callback=self._cb_check_sat_status)

    def _cb_check_sat_status(self, valid_response, responses, request):   # TODO: not implemented
        if valid_response:
            self.log.debug("Current satellite status: {}".format(self.ctrl_states[int(responses[0])]))
            # first response S122 = satellite trace status
            old_sat_ctrl_state = self.sat_status.ctrl_state
            new_sat_ctrl_state = self.ctrl_states[int(responses[0])]
            if new_sat_ctrl_state != old_sat_ctrl_state:
                sat_status_change = new_sat_ctrl_state
                self.log.info("Satellite control state change: OLD={} NEW={}"
                              .format(old_sat_ctrl_state, new_sat_ctrl_state))
                self.sat_status.ctrl_state = new_sat_ctrl_state
                # Key events for relevant state changes and statistics tracking
                if new_sat_ctrl_state == 'Waiting for GNSS fix':
                    self.system_stats['lastGNSSStartTime'] = int(time.time())
                    self.system_stats['nGNSS'] += 1
                elif new_sat_ctrl_state == 'Registration in progress':
                    if self.sat_status.registered:
                        self.sat_status.registered = False
                    self.system_stats['lastRegStartTime'] = int(time.time())
                elif new_sat_ctrl_state == 'Downloading Bulletin Board':
                    self.sat_status.bb_wait = True
                    self.system_stats['lastBBStartTime'] = time.time()
                    # TODO: Is prior registration now invalidated?
                    sat_status_change = 'bb_wait'
                elif new_sat_ctrl_state == 'Active':
                    if self.sat_status.blocked:
                        self.log.info("Blockage cleared")
                        blockage_duration = int(time.time() - self.system_stats['lastBlockStartTime'])
                        if self.system_stats['avgBlockageDuration'] > 0:
                            self.system_stats['avgBlockageDuration'] \
                                = int((blockage_duration + self.system_stats['avgBlockageDuration']) / 2)
                        else:
                            self.system_stats['avgBlockageDuration'] = blockage_duration
                            sat_status_change = 'unblocked'
                    if not self.sat_status.registered:
                        self.log.debug("Modem registered")
                        self.sat_status.registered = True
                        self.system_stats['nRegistration'] += 1
                        if self.system_stats['lastRegStartTime'] > 0:
                            registration_duration = int(time.time() - self.system_stats['lastRegStartTime'])
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
                    self.system_stats['lastBlockStartTime'] = time.time()
                    self.log.info("Blockage started")
                    sat_status_change = 'blocked'
                # Other transitions for statistics tracking:
                if old_sat_ctrl_state == 'Waiting for GNSS fix':
                    gnss_duration = int(time.time() - self.system_stats['lastGNSSStartTime'])
                    self.log.info("GNSS acquired in {} seconds".format(gnss_duration))
                    if self.system_stats['avgGNSSFixDuration'] > 0:
                        self.system_stats['avgGNSSFixDuration'] \
                            = int((gnss_duration + self.system_stats['avgGNSSFixDuration']) / 2)
                    else:
                        self.system_stats['avgGNSSFixDuration'] = gnss_duration
                    if new_sat_ctrl_state not in ['Stopped', 'Blocked', 'Active']:
                        sat_status_change = 'new_gnss_fix'
                    else:
                        self.log.debug("GNSS fix implied by state transition to {}".format(new_sat_ctrl_state))
                if old_sat_ctrl_state == 'Downloading Bulletin Board' \
                        and new_sat_ctrl_state not in ['Stopped', 'Blocked']:
                    bulletin_duration = int(time.time() - self.system_stats['lastBBStartTime'])
                    self.log.info("Bulletin Board downloaded in {} seconds".format(bulletin_duration))
                    if self.system_stats['avgBBReacquireDuration'] > 0:
                        self.system_stats['avgBBReacquireDuration'] \
                            = int((bulletin_duration + self.system_stats['avgBBReacquireDuration']) / 2)
                    else:
                        self.system_stats['avgBBReacquireDuration'] = bulletin_duration
                self._on_sat_status_change(sat_status_change)
            # second response S116 = C/No
            c_n0 = int(responses[1]) / 100.0
            if self.system_stats['avgCN0'] == 0:
                self.system_stats['avgCN0'] = c_n0
            else:
                self.system_stats['avgCN0'] = round((self.system_stats['avgCN0'] + c_n0) / 2.0, 2)

    def _on_sat_status_change(self, event):
        if event in self.events:
            if self.event_callbacks[event] is not None:
                self.log.info("Calling back for {} to {}".format(event, self.event_callbacks[event].__name__))
                self.event_callbacks[event](event)
            else:
                self.log.info("No callback defined for {}".format(event))
        else:
            if self.event_callbacks['satellite_status_change'] is not None:
                self.log.info("Calling back for satellite_status_change to {}"
                              .format(self.event_callbacks[event].__name__))
                self.event_callbacks['satellite_status_change'](event)
            else:
                self.log.info("No callback defined for satellite_status_change")

    # --------------------- MESSAGE HANDING -------------------------------------------------------- #
    @staticmethod
    def get_msg_state(state):
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
        """
        Submits a message on the AT command interface and calls back when complete.

        :param mo_message: (MobileOriginatedMessage)
        :param callback: (function)
        :param priority: (int(
        :return: (string) a unique 8-character name for the message based on the time it was submitted

        """
        self.log.debug("User submitted message name: {}".format(mo_message.name))
        if isinstance(mo_message, MobileOriginatedMessage):
            mo_message.priority = priority if priority is not None else mo_message.priority
            p_msg = self._PendingMoMessage(message=mo_message, callback=callback)
            self.mo_msg_queue.append(p_msg)
            self.submit_at_command(at_command='AT%MGRT={name},{priority},{sin}{min},{data_format},{data}'
                                   .format(name='\"{}\"'.format(p_msg.q_name),
                                           priority=mo_message.priority,
                                           sin=mo_message.sin,
                                           min='.{}'.format(mo_message.min) if min is not None else '',
                                           data_format=mo_message.data_format,
                                           data=mo_message.payload),
                                   callback=self._cb_send_message)
            return p_msg.q_name
        else:
            self._handle_error("Message submitted must be type MobileOriginatedMessage")

    def _cb_send_message(self, valid_response, responses, request):
        if valid_response:
            self.log.debug("Mobile-Originated message submitted {}".format(request))
        else:
            self.log.error(responses)
            # TODO: de-queue failed message?
            for p_msg in self.mo_msg_queue:
                if p_msg.name == request.split('\"')[1]:
                    self.mo_msg_queue.remove(p_msg)
                    break

    def check_mo_messages(self, msg_name=None, user_callback=None):
        """
        Checks the state of messages in the modem queue, triggering a callback with the responses

        :param msg_name:
        :param user_callback:
        :return:
        """
        if len(self.mo_msg_queue) > 0:
            self.log.debug("{} MO messages queued ({})".format(len(self.mo_msg_queue), self.mo_msg_queue))
            callback = self._cb_check_mo_messages if user_callback is None else user_callback
            self.submit_at_command(at_command='AT%MGRS{}'
                                   .format('={}'.format(msg_name) if msg_name is not None else ''),
                                   callback=callback)
        else:
            self.log.debug("No MO messages queued")

    def _cb_check_mo_messages(self, valid_response, responses, request):
        """
        Callback from a Mobile Originated message check AT%MGRS

        :param valid_response: (boolean) True if the response had no error and did not time out
        :param responses: (list) responses to the AT%MGRS command, which may include several messages state info
        :return: if complete/failed, calls back to the pending message callback with the following parameters:

           * (string or None) name of the message, submitted by user
           * (string) q_name the unique name assigned for queueing in the modem
           * (int) state either TX_COMPLETE=6 or TX_FAILED=7
           * (int) size of the message Over-The-Air, in bytes

        """
        if valid_response:
            # Format of responses should be: %MGRS: "<name>",<msg_no>,<priority>,<sin>,<state>,<size>,<sent_bytes>
            for res in responses:
                self.log.debug("Processing response: {}".format(res))
                if len(res.replace('%MGRS:', '').strip()) > 0:
                    name, msg_no, priority, sin, state, size, sent_bytes = res.replace('%MGRS:', '').strip().split(',')
                    name = name.replace('\"', '')
                    priority = int(priority)
                    sin = int(sin)
                    state = int(state)
                    size = int(size)
                    sent_bytes = int(sent_bytes)
                    for p_msg in self.mo_msg_queue:
                        if p_msg.q_name == name:
                            msg = p_msg.message
                            self.log.debug("Processing MO message: {} ({}) state: {}"
                                           .format(msg.name, p_msg.q_name, self.get_msg_state(state)))
                            if state != msg.state:
                                msg.state = state
                                if state in (TX_COMPLETE, TX_FAILED):
                                    p_msg.complete_time = time.time()
                                    p_msg.failed = True if state == TX_FAILED else False
                                    self.log.debug("Removing {} from pending message queue".format(p_msg.q_name))
                                    self.mo_msg_queue.remove(p_msg)
                                    # TODO: calculate statistics for MO message transmission times
                                    if p_msg.callback is not None:
                                        p_msg.callback(msg.name, p_msg.q_name, msg.state, msg.size)
                                    else:
                                        self.log.warning("No callback defined for {}".format(p_msg.q_name))
                                else:
                                    self.log.debug("MO message {} state changed to: {}"
                                                   .format(p_msg.q_name, self.get_msg_state(state)))
                            break
                else:
                    self.log.debug("Empty MO message queue returned")
        else:
            self.log.warning("Invalid response to AT%MGRS: {}".format(responses))

    def check_mt_messages(self, user_callback=None):
        callback = self._cb_check_mt_messages if user_callback is None else user_callback
        self.submit_at_command(at_command='AT%MGFN', callback=callback)

    def _cb_check_mt_messages(self, valid_response, responses, request):
        if valid_response:
            self.log.warning("Processing AT%MGFN {}".format(responses))
            for res in responses:
                # Format of responses should be: %MGFN: "<name>",<msg_no>,<priority>,<sin>,<state>,<size>,<bytes_rcvd>
                msg_pending = res.replace('%MGFN:', '').strip()
                if msg_pending:
                    name, number, priority, sin, state, size, bytes_read = msg_pending.split(',')
                    name = name.replace('\"', '')
                    priority = int(priority)
                    sin = int(sin)
                    state = int(state)
                    size = int(size)
                    if state == RX_COMPLETE:  # Complete and not read
                        # TODO: assign data_format based on size?
                        queued = False
                        for p_msg in self.mt_msg_queue:
                            if p_msg.q_name == name:
                                queued = True
                                self.log.debug("Pending message {} already in queue".format(name))
                                break
                        if not queued:
                            p_msg = self._PendingMtMessage(message=None, q_name=name, sin=sin, size=size)
                            self.mt_msg_queue.append(p_msg)
                            if self.event_callbacks['new_mt_message'] is not None:
                                self.log.debug("Calling back to {}"
                                               .format(self.event_callbacks['new_mt_message'].__name__))
                                self.event_callbacks['new_mt_message'](self.mt_msg_queue)
                            else:
                                self.log.warning("No callback registered for new MT messages")
                    else:
                        self.log.debug("Message {} not complete ({}/{} bytes)".format(name, bytes_read, size))
            # if len(self.mt_msg_queue) > 0:
            #     if self.event_callbacks['new_mt_message'] is not None:
            #         self.event_callbacks['new_mt_message'](self.mt_msg_queue)
            #     else:
            #         self.log.warning("No callback registered for new MT messages")

    def get_mt_message(self, msg_name, callback, data_format=None):
        found = False
        for m in self.mt_msg_queue:
            if m.q_name == msg_name:
                found = True
                m.callback = callback
                if data_format is None:
                    data_format = FORMAT_HEX if m.size <= 100 else FORMAT_B64
                self.log.debug("Retrieving MT message {}".format(msg_name))
                self.submit_at_command(at_command='AT%MGFG=\"{}\",{}'.format(msg_name, data_format),
                                       callback=self._cb_get_mt_message)
                break
        return found, "Message not found in MT queue" if not found else None

    def _cb_get_mt_message(self, valid_response, responses, request):
        if valid_response:
            # Response format: "<fwdMsgName>",<msgNum>,<priority>,<sin>,<state>,<length>,<dataFormat>,<data>
            if len(responses) > 1:
                self.log.warning("Unexpected responses {}".format(responses))
            response = responses[0].replace('%MGFG:', '').strip()
            q_name, msg_num, priority, sin, state, length, data_format, data = response.split(',')
            q_name = q_name.replace('\"', '')
            sin = int(sin)
            size = int(length)
            data_format = int(data_format)
            msg_min = None
            if data_format == FORMAT_TEXT:
                b_payload = bytearray(b'{}'.format(data[1:len(data)-1]))   # remove only quotes at ends, not in middle
                msg_min = int(b_payload[0])
                payload = str(b_payload[1:])
            elif data_format == FORMAT_HEX:
                payload = _hex_to_bytearray(data)
            elif data_format == FORMAT_B64:
                payload = _b64_to_bytearray(b'{}'.format(data))
            mt_msg = MobileTerminatedMessage(payload=payload, name=q_name, msg_sin=sin, msg_min=msg_min,
                                             priority=PRIORITY_LOW, data_format=data_format, size=size,
                                             debug=self.debug)
            for m in self.mt_msg_queue:
                if m.q_name == q_name:
                    self.mt_msg_queue.remove(m)
                    if m.callback is not None:
                        self.log.debug("Calling back to {}".format(m.callback.__name__))
                        m.callback(mt_msg)
                    else:
                        self.log.error("No callback defined for message {}".format(q_name))
                    break
        else:
            self.log.error("Invalid response ({})".format(responses))

    # ----------------------- HARDWARE EVENT NOTIFICATIONS ----------------------------------------- #
    def get_event_notification_control(self, init=False):
        """
        Updates the ``hw_event_notifications`` attribute

        :param init: (boolean) flag set if the register value has just been read during initialization
        :return: An ``OrderedDict`` corresponding to the ``hw_event_notifications`` attribute.

        """
        if init:
            self._update_event_notifications(self.s_registers.register('S88').get())
        else:
            self.submit_at_command('ATS88?', callback=self._cb_get_event_notification_control)

    def _cb_get_event_notification_control(self, valid_response, responses):
        if valid_response:
            reg_value = int(responses[0])
            self.s_registers.register('S88').set(reg_value)
            self._update_event_notifications(reg_value)
        else:
            self.log.warning("Invalid response to ATS88? command: {}".format(responses))

    def _update_event_notifications(self, value):
        """
        Sets the proxy bitmap values for event_notification in the modem object.

        :param value: the event bitmap (integer)

        """
        event_notify_bitmap = bin(value)[2:]
        if len(event_notify_bitmap) > len(self.hw_event_notifications):
            event_notify_bitmap = event_notify_bitmap[:len(self.hw_event_notifications) - 1]
        while len(event_notify_bitmap) < len(self.hw_event_notifications):  # pad leading zeros
            event_notify_bitmap = '0' + event_notify_bitmap
        i = 0
        for key in reversed(self.hw_event_notifications):
            self.hw_event_notifications[key] = True if event_notify_bitmap[i] == '1' else False
            i += 1
        self.log.debug("Updated event notifications: {}".format(self.hw_event_notifications))

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
                    self.log.info("{} event notification {}".format(key, "enabled" if value else "disabled"))
                else:
                    self.log.debug("No change to {} event notification {}"
                                   .format(key, "enabled" if value else "disabled"))
            else:
                self.log.error("Event {} not defined".format(key))
        else:
            self.log.error("Value {} must be type Boolean".format(value))

    def _cb_set_event_notification_control(self, valid_response, responses):
        if not valid_response:
            # TODO: read S88 and update self.hw_event_notifications and twin
            self.log.warning("Failed to update event notifications control S88: {}".format(responses))
            self.get_event_notification_control()

    def check_events(self):
        self.log.warning("EVENT MONITOR ATS89? NOT IMPLEMENTED")
        # self.submit_at_command('ATS89?', callback=None)

    def _cb_check_events(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(sys._getframe().f_code.co_name))

    def _on_hw_event(self, event):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(sys._getframe().f_code.co_name))

    # --------------------- NMEA OPERATIONS --------------------------------------------------- #
    def get_nmea(self, callback, rmc=True, gga=True, gsa=True, gsv=True, refresh=0):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def _cb_get_nmea(self, valid_response, responses, request):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def get_location(self, callback):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    # --------------------- S-REGISTER OPERATIONS ---------------------------------------------- #
    def set_s_register(self, register, value, callback=None, save=False):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def _cb_set_s_register(self, valid_response, responses, request):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def get_s_register(self, register, callback):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def _cb_get_s_register(self, valid_response, responses, request):
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    def _update_twin_parameters(self, register, value):
        # TODO: check which IdpModem parameters are affected by the read/write of s-Registers and update twin
        self.log.warning("FUNCTION NOT IMPLEMENTED")

    # --------------------- Generic functions that might be useful ----------------------------------- #
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

    # --------------------- Broken functions that need update ----------------------------------- #
    def set_wakeup_interval(self, value, callback=None, save=False):
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
        if value in self.wakeup_intervals:
            # TODO: make sure value is of correct type (integer)
            self.set_s_register(register='S51', value=value, callback=callback, save=save)
        else:
            self.log.error("Invalid value {} for S51 wakeup interval".format(value))

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

    # ********************** TODO: DEPRECATE THESE OLD BLOCKING FUNCTIONS ****************************
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

        if self.at_config.crc:
            to_send = at_cmd + '*' + self.get_crc(at_cmd)
        else:
            to_send = at_cmd
        if "AT%CRC=1" in at_cmd.upper():
            self.at_config.crc = True
            log.debug("CRC enabled for next command")
        elif "AT%CRC=0" in at_cmd.upper():
            self.at_config.crc = False
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
                            self.at_config.echo = False
                            log.debug("ATE0 (echo disable) requested. Takes effect for next AT command.")
                        else:
                            self.at_config.echo = True
                        at_echo = res_line.strip()  # copy the echo into a function return
                        # <echo><cr> will be not be followed by <lf>
                        # can be followed by <text><cr><lf>
                        # or <cr><lf><text><cr><lf>
                        # or <numeric code><cr>
                        # or <cr><lf><verbose code><cr><lf>
                        res_line = ''  # clear for next line of parsing
                    elif ser.inWaiting() == 0 and res_line.strip() != '':
                        # or <text><cr>...with delay for <lf> between multi-line responses e.g. GNSS?
                        if self.at_config.verbose:
                            # case <cr><lf><text><cr>...<lf>
                            # or Quiet mode? --unsupported, suppresses result codes
                            log.debug("Assuming delay between <cr> and <lf> of Verbose response...waiting")
                        else:
                            # case <numeric code><cr> since all other alternatives should have <lf> or other pending
                            log.debug("Assuming receipt <numeric code><cr> with Verbose off: " + res_line.strip())
                            # self.at_config.verbose = False
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
                        self.at_config.crc = True
                        at_res_crc = res_line.replace('*', '').strip()
                        at_rx_complete = True
                        break
                    else:
                        # case <cr><lf>
                        # or <text><cr><lf>
                        # or <cr><lf><text><cr><lf>
                        if res_line.strip() == '':
                            # <cr><lf> empty line...not done parsing yet
                            self.at_config.verbose = True
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
                self.at_config.quiet = False
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
                self.at_config.crc = False
            else:
                self.at_config.crc = True
                if len(at_response) == 0 and at_result_code != '':
                    str_to_validate = at_result_code
                else:
                    str_to_validate = ''
                    for res_line in at_response:
                        str_to_validate += res_line
                    if at_result_code != '':
                        str_to_validate += at_result_code
                if self.get_crc(str_to_validate) == at_res_crc:
                    checksum_ok = True
                else:
                    expected_checksum = self.get_crc(str_to_validate)
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
        success = False
        with self.thread_lock:
            response = self.at_get_response('AT', at_timeout=at_timeout)
            if response['timeout']:
                self.log.debug("Failed attempt to establish AT response")
            elif response['result'] != '':
                success = True
                self.log.info("AT attach confirmed")
            else:
                self.log.warning("Unexpected response from AT command")
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
                self.at_config.crc = True
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
            elif err_code == 100 and self.at_config.crc:
                log.info("Attempted to set CRC when already set")
            else:
                log.error("CRC enable failed (" + err_str + ")")
        elif self.at_config.crc:
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
        self.at_config.quiet = False

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
            self.at_config.verbose = verbose
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

        ..todo:
           Interface with relevant callbacks

        :returns: A ``dictionary`` with:

           - ``success`` Boolean
           - ``changed`` Boolean
           - ``state`` (string from ctrl_states)

        """
        log = self.log
        success = False
        changed = False
        with self.thread_lock:
            log.debug("Checking satellite status. Last known state: {}".format(self.sat_status.ctrl_state))

            # S122: satellite trace status
            # S116: C/N0
            response = self.at_get_response('ATS90=3 S91=1 S92=1 S122? S116?')

            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
                    old_sat_ctrl_state = self.sat_status.ctrl_state
                    new_sat_ctrl_state = self.ctrl_states[int(response['response'][0])]
                    if new_sat_ctrl_state != old_sat_ctrl_state:
                        changed = True
                        log.info("Satellite control state change: OLD={} NEW={}".format(old_sat_ctrl_state,
                                                                                        new_sat_ctrl_state))
                        self.sat_status.ctrl_state = new_sat_ctrl_state

                        # Key events for relevant state changes and statistics tracking
                        if new_sat_ctrl_state == 'Waiting for GNSS fix':
                            self.system_stats['lastGNSSStartTime'] = int(time.time())
                            self.system_stats['nGNSS'] += 1
                        elif new_sat_ctrl_state == 'Registration in progress':
                            self.system_stats['lastRegStartTime'] = int(time.time())
                            self.system_stats['nRegistration'] += 1
                        elif new_sat_ctrl_state == 'Downloading Bulletin Board':
                            self.sat_status.bb_wait = True
                            self.system_stats['lastBBStartTime'] = time.time()
                            # TODO: callback for BB_WAIT
                        elif new_sat_ctrl_state == 'Active':
                            if self.sat_status.blocked:
                                log.info("Blockage cleared")
                                # TODO: callback for Unblocked
                                blockage_duration = int(time.time() - self.system_stats['lastBlockStartTime'])
                                if self.system_stats['avgBlockageDuration'] > 0:
                                    self.system_stats['avgBlockageDuration'] \
                                        = int((blockage_duration + self.system_stats['avgBlockageDuration']) / 2)
                                else:
                                    self.system_stats['avgBlockageDuration'] = blockage_duration
                            self.sat_status.registered = True
                            # TODO: callback for Registered
                            self.sat_status.blocked = False
                            self.sat_status.bb_wait = False
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
                            self.sat_status.blocked = True
                            self.system_stats['lastBlockStartTime'] = time.time()
                            log.info("Blockage started")
                            # TODO: callback for Blocked

                        # Other transitions for statistics tracking:
                        if old_sat_ctrl_state == 'Waiting for GNSS fix' \
                                and new_sat_ctrl_state != 'Stopped' and new_sat_ctrl_state != 'Blocked':
                            gnss_duration = int(time.time() - self.system_stats['lastGNSSStartTime'])
                            log.info("GNSS acquired in {} seconds".format(gnss_duration))
                            # TODO: callback for NewGNSS
                            if self.system_stats['avgGNSSFixDuration'] > 0:
                                self.system_stats['avgGNSSFixDuration'] \
                                    = int((gnss_duration + self.system_stats['avgGNSSFixDuration']) / 2)
                            else:
                                self.system_stats['avgGNSSFixDuration'] = gnss_duration
                        if old_sat_ctrl_state == 'Downloading Bulletin Board' \
                                and new_sat_ctrl_state != 'Stopped' and new_sat_ctrl_state != 'Blocked':
                            bulletin_duration = int(time.time() - self.system_stats['lastBBStartTime'])
                            log.info("Bulletin Board downloaded in {} seconds".format(bulletin_duration))
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
                    log.error("Bad response to satellite status query ({})".format(err_str))
            else:
                log.warning("Timeout occurred on satellite status query")

        return {'success': success, 'changed': changed, 'state': self.sat_status.ctrl_state}

    def at_check_mt_messages(self):
        """
        Checks for Mobile-Terminated messages in modem queue and retrieves if present.
        Logs a record of the receipt, and handles supported messages.

        :returns:
           - Calls back to a registered callback with:
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

        # return True if len(messages) > 0 else False, messages
        if len(messages) > 0:
            if self.event_callbacks is not None:
                self.event_callbacks['new_mt_message'](messages)
            else:
                self.log.warning("No callback registered for new MT messages")

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
              - ``payload`` including MIN byte, structure depends on data_type (text, hex, base64)
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
                        if data_type == 1:      # text
                            message['min'] = int(msg_content.replace('"', '')[1:3], 16)
                            msg_content_str = msg_content.replace('"', '')[3:]
                            message['payload'] = msg_content_str
                            message['size'] = len(msg_content_str) + 2
                        elif data_type == 2:      # hex-string
                            message['min'] = int(msg_content[0:2], 16)
                            msg_content_str = '0x' + str(msg_content)
                            message['payload'] = bytearray(msg_content.decode("hex"))
                            message['size'] = len(message['payload']) + 1
                        elif data_type == 3:      # base64-string
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

    def at_submit_message(self, data_string, data_format=1, msg_sin=128, msg_min=1, priority=4):
        """
        Transmits a Mobile-Originated message. If ASCII-Hex format is used, 0-pads to nearest byte boundary.

        :param data_string: data to be transmitted
        :param data_format: 1=Text (default), 2=ASCII-Hex, 3=base64
        :param msg_sin: first byte of message (default 128 "user")
        :param msg_min: second byte of message (default 1 "user")
        :param priority: 1(high) through 4(low, default)
        :return:

           * Boolean result
           * String message name
           * Integer submit time

        """
        log = self.log
        self.mo_msg_count += 1
        mo_msg_name = str(int(time.time()))[:8]
        mo_msg_priority = priority
        mo_msg_sin = msg_sin
        mo_msg_min = msg_min
        mo_msg_format = data_format
        msg_submitted = False
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
                    msg_submitted = True
                    # TODO: convert to a thread with callback on completion
                    '''
                    status_poll_count = 0
                    msg_complete = False
                    while not msg_complete:
                        time.sleep(1)
                        status_poll_count += 1
                        log.debug("MGRS queries: {count}".format(count=status_poll_count))
                        success, msg_complete = self.at_check_mo_status(mo_msg_name)
                    '''
                    '''
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
                                msg_submitted = True
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
                    '''
                else:
                    log.error("Message submit error (%s)" % err_str)
            else:
                log.warning("Timeout attempting to submit MO message")

        return msg_submitted, mo_msg_name, mo_submit_time

    def at_check_mo_status(self, mo_msg_name, mo_submit_time=None):
        """
        TODO: docstring

        :param mo_msg_name:
        :param mo_submit_time:
        :return:

           * Boolean success of operation
           * Boolean message complete

        """
        log = self.log
        success = False
        msg_complete = False
        with self.thread_lock:
            # pending_mo_message['status_poll_count'] += 1
            # status_poll_count = pending_mo_message['status_poll_count']
            # mo_msg_sin = pending_mo_message['mo_msg_sin']
            # mo_msg_min = pending_mo_message['mo_msg_min']
            # log.debug("MGRS queries: %d" % status_poll_count)
            response = self.at_get_response('AT%MGRS="' + mo_msg_name + '"')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    success = True
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
                        if mo_submit_time is not None:
                            msg_latency = int(time.time() - mo_submit_time)
                        else:
                            msg_latency = "??"
                        msg_min = "??"
                        if res_state == 6:
                            log.info("MO message SIN={sin} MIN={min} ({size} bytes) "
                                     "completed in {time} seconds".format(sin=res_sin, min=msg_min, size=res_size,
                                                                          time=msg_latency))
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
                            log.warning("MO message SIN={sin} MIN={min} ({size} bytes) "
                                        "failed after {time} seconds".format(sin=res_sin, min=msg_min, size=res_size,
                                                                             time=msg_latency))
                elif err_code == 109:
                    success = True
                    log.debug("Message complete, Unavailable")
                    # TODO: is this the right place for this?
                    # self.pending_mo_messages.remove(pending_mo_message)
                    msg_complete = True
                else:
                    log.error("Error getting message state: {err}".format(err=err_str))
                    # TODO: this may require the message to be "parked" rather than stay pending
            else:
                log.error("Message status check timed out")
        return success, msg_complete

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
        and updates the hw_event_notifications attribute of the Modem instance.

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
        for event in reversed(self.hw_event_notifications):
            binary += '1' if self.hw_event_notifications[event] else '0'
        value = int(binary, 2)
        with self.thread_lock:
            response = self.at_get_response('ATS88?')
            if not response['timeout']:
                err_code, err_str = self.at_get_result_code(response['result'])
                if err_code == 0:
                    register_value = int(response['response'][0])
                    if value != register_value:
                        log.warning("S88 register value mismatch")
                        self._update_event_notifications(register_value)
                else:
                    log.error("Error querying S88")
        return register_value

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
                    self._update_event_notifications(value)
                    if save:
                        self.at_save_config()
                else:
                    log.error("Write %d to S88 failed (%s)" % (value, err_str))

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
        event_dict = self.hw_event_notifications
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

    # --------------------- Logging operations ---------------------------------------------- #
    def log_at_config(self):
        """Logs/displays the current AT configuration options (e.g. CRC, Verbose, Echo, Quiet) on the console."""
        self.log.info("*** Modem AT Configuration ***")
        for attr, value in self.at_config.__dict__.iteritems():
            self.log.info("*  {}={}".format(attr, 1 if value else 0))

    def log_sat_status(self):
        """Logs/displays the current satellite status on the console."""
        self.log.info("*** Satellite Status ***")
        for attr, value in self.sat_status.__dict__.iteritems():
            self.log.info("*  {}={}".format(attr, value))

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
        self.log.info("* Mobile ID: {}".format(self.mobile_id))
        self.log.info("* Hardware version: {}".format(self.hardware_version))
        self.log.info("* Firmware version: {}".format(self.software_version))
        self.log.info("* AT version: {}".format(self.at_version))
        for stat in self.get_statistics():
            self.log.info("* {}: {}".format(stat[0], str(stat[1])))
        self.log.info("*" * 75)


def _is_hex_string(s):
    hex_chars = '0123456789abcdefABCDEF'
    return all(c in hex_chars for c in s)


def _bytearray_to_hex(arr):
    return binascii.hexlify(bytearray(arr))


def _hex_to_bytearray(h):
    return bytearray.fromhex(h)


def _bytearray_to_b64(arr):
    return base64.b64encode(bytearray(arr))


def _b64_to_bytearray(b):
    return binascii.b2a_base64(b)


class Message(object):
    """
    Class intended for abstracting message characteristics.

    :param payload: one of the following:

       * (bytearray) including SIN and MIN bytes as first 2 in the array if not explicitly set in the call
       * (list) of integer bytes (0..255) including SIN and MIN if not specified explicitly in the call
       * (string) ASCII-HEX which includes SIN and MIN if not specified explicitly in the call
       * (string) Text which requires both SIN and MIN explictly specified in the call

    :param name: (string) optional up to 8 characters. A message name will be generated if not supplied
    :param msg_sin: integer (0..255)
    :param msg_min: integer (0..255)
    :param priority: (1=high, 4=low)
    :param data_format: (optional) 1=FORMAT_TEXT, 2=FORMAT_HEX, 3=FORMAT_B64
    :param log: (optional) logger object
    :param debug: (optional) sets logging level to DEBUG

    """

    MAX_HEX_SIZE = 100

    def __init__(self, payload, name="user", msg_sin=None, msg_min=None, priority=PRIORITY_LOW,
                 data_format=None, size=None, log=None, debug=False):
        if is_logger(log):
            self.log = log
        else:
            self.log = get_wrapping_log(logfile=log, debug=debug)
        # if isinstance(payload, str):
        #     if _is_hex_string(payload):
        #         payload = bytearray.fromhex(payload)
        #     else:
        #         if msg_sin is not None and msg_min is not None:
        #             if data_format is None or data_format != FORMAT_TEXT:
        #                 payload = bytearray(payload)
        #         else:
        #             raise ValueError("Function call with text string payload must include SIN and MIN")
        # elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255)) for i in payload):
        #     payload = bytearray(payload)
        # elif not isinstance(payload, bytearray):
        #     raise ValueError("Invalid payload type, must be text or hex string, integer list or bytearray")
        if msg_min is not None:
            if msg_sin is None:
                raise ValueError("msg_sin must be specified if msg_min is specified")
            if isinstance(msg_min, int) and msg_min in range(0, 255):
                self.min = msg_min
                # if payload is not None:
                #     raw_payload = bytearray(b'{}'.format(msg_min)) + bytearray(payload)
            else:
                self.log.warning("Invalid MIN value {} must be integer in range 0..255".format(msg_min))
        elif payload is not None:
            self.min = bytearray(payload)[1]
        else:
            self.min = None
        if msg_sin is not None:
            if isinstance(msg_sin, int) and msg_sin in range(16, 255):
                self.sin = msg_sin
                # if payload is not None:
                #     raw_payload = bytearray(b'{}'.format(msg_sin)) + payload
            else:
                raise ValueError("Invalid SIN value {}, must be integer in range 16..255".format(msg_sin))
        elif payload is not None:
            if bytearray(payload)[0] > 15:
                self.sin = bytearray(payload)[0]
                self.log.debug("Received bytearray with implied SIN={}".format(self.sin))
                # raw_payload = payload
            else:
                raise ValueError("Invalid payload, first byte (SIN) must be integer in range 16..255")
        else:
            self.sin = None
        # self.payload = payload
        self.raw_payload = bytearray(0)
        if payload is not None:
            if isinstance(payload, str):
                if _is_hex_string(payload) and data_format != FORMAT_TEXT:
                    payload = bytearray.fromhex(payload)
                else:
                    if msg_sin is not None and msg_min is not None:
                        payload = bytearray(payload)
                    else:
                        raise ValueError("Function call with text string payload must include SIN and MIN")
            elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255)) for i in payload):
                payload = bytearray(payload)
            elif not isinstance(payload, bytearray):
                raise ValueError("Invalid payload type, must be text or hex string, integer list or bytearray")
            self.raw_payload = bytearray(payload)
            if msg_min is not None:
                self.raw_payload = bytearray(b'{}'.format(msg_min)) + self.raw_payload
            if msg_sin is not None:
                self.raw_payload = bytearray(b'{}'.format(msg_sin)) + self.raw_payload
            self.size = len(self.raw_payload)
        else:
            self.size = size
        # TODO: simplify this so that the raw payload is always a bytearray
        if data_format is None:
            if self.size is not None and self.size <= self.MAX_HEX_SIZE:
                self.data_format = FORMAT_HEX
                self.payload = _bytearray_to_hex(self.raw_payload[1:])
            else:
                self.data_format = FORMAT_B64
                self.payload = _bytearray_to_b64(self.raw_payload[1:])
        elif data_format in (FORMAT_TEXT, FORMAT_HEX, FORMAT_B64):
            self.data_format = FORMAT_TEXT
            self.payload = '\"{}\"'.format(payload)
        else:
            raise ValueError("Unsupported data format: {}".format(data_format))
        self.priority = priority
        self.name = name

    def data(self, data_format=FORMAT_HEX):
        if len(self.raw_payload) > 0:
            if data_format == FORMAT_TEXT:
                return '\"{}\"'.format(self.raw_payload[2:])
            elif data_format == FORMAT_HEX:
                return _bytearray_to_hex(self.raw_payload[1:])
            elif data_format == FORMAT_B64:
                return _bytearray_to_b64(self.raw_payload[1:])
            else:
                raise ValueError("Invalid data format")
        else:
            return None


class MobileOriginatedMessage(Message):
    """
    Class containing Mobile Originated (aka Return) message properties.
    Mobile-Originated states enumeration (per modem documentation):

       - ``UNAVAILABLE``: 0
       - ``TX_READY``: 4
       - ``TX_SENDING``: 5
       - ``TX_COMPLETE``: 6
       - ``TX_FAILED``: 7

    :param name: identifier for the message (tbd limitations)
    :param msg_sin: Service Identification Number (1st byte of payload)
    :param msg_min: Message Identification Number (2nd byte of payload)
    :param payload_b64: (optional) base64 encoded payload (not including SIN/MIN)

    """

    def __init__(self, payload, data_format=None, msg_sin=None, msg_min=None, **kwargs):
        """

        :param name: identifier for the message (tbd limitations)
        :param msg_sin: Service Identification Number (1st byte of payload)
        :param msg_min: Message Identification Number (2nd byte of payload)
        :param payload_b64: (optional) base64 encoded payload (not including SIN/MIN)

        """
        if isinstance(payload, str):
            if _is_hex_string(payload):
                payload = bytearray.fromhex(payload)
            else:
                if msg_sin is not None and msg_min is not None:
                    if data_format is None or data_format != FORMAT_TEXT:
                        payload = bytearray(payload)
                else:
                    raise ValueError("Function call with text string payload must include SIN and MIN")
        elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255)) for i in payload):
            payload = bytearray(payload)
        elif not isinstance(payload, bytearray):
            raise ValueError("Invalid payload type, must be text or hex string, integer list or bytearray")
        super(MobileOriginatedMessage, self).__init__(payload, msg_sin=msg_sin, msg_min=msg_min,
                                                      data_format=data_format, **kwargs)
        self.state = None


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

    def __init__(self, *args, **kwargs):
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
        super(MobileTerminatedMessage, self).__init__(*args, **kwargs)
        self.state = RX_COMPLETE
        self.number = None


class Location(object):
    def __init__(self):
        self.lat = None
        self.lng = None
        self.alt = None
        self.spd = None
        self.hdg = None
        self.pdop = None


if __name__ == "__main__":
    modem = Modem(serial_port=None, debug=True)
    modem.log_at_config()
    modem.log_sat_status()
    modem.log_statistics()
