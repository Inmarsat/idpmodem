#!/usr/bin/env python
# TODO: OBSOLETE
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
   * Handle unsolicited modem output.  Can this happen while awaiting AT response??

"""

from __future__ import absolute_import

import base64
import binascii
from collections import OrderedDict
import datetime
from string import printable
import sys
import threading
import time

import serial

try:
    from utils import get_caller_name, get_wrapping_logger, is_logger
    from utils import RepeatingTimer
    from utils import validate_serial_port
    import crcxmodem
    import nmea
except ImportError:    
    from idpmodem.utils import get_caller_name, get_wrapping_logger, is_logger
    from idpmodem.utils import RepeatingTimer
    from idpmodem.utils import validate_serial_port
    import idpmodem.crcxmodem as crcxmodem
    import idpmodem.nmea as nmea


__version__ = "2.0.0"


# Message Priorities and Data Formats
PRIORITY_MT, PRIORITY_HIGH, PRIORITY_MEDH, PRIORITY_MEDL, PRIORITY_LOW = (
    0, 1, 2, 3, 4)
FORMAT_TEXT, FORMAT_HEX, FORMAT_B64 = (1, 2, 3)
# Message States
UNAVAILABLE = 0
RX_COMPLETE = 2
RX_RETRIEVED = 3
TX_READY = 4
TX_SENDING = 5
TX_COMPLETE = 6
TX_FAILED = 7
# Wakeup Intervals
WAKEUP_5_SEC = 0
WAKEUP_30_SEC = 1
WAKEUP_1_MIN = 2
WAKEUP_3_MIN = 3
WAKEUP_10_MIN = 4
WAKEUP_30_MIN = 5
WAKEUP_60_MIN = 6
WAKEUP_2_MIN = 7
WAKEUP_5_MIN = 8
WAKEUP_15_MIN = 9
WAKEUP_20_MIN = 10
WAKEUP_INTERVALS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
# Power Mode settings (S50)
POWER_MODE_MOBILE_POWERED = 0
POWER_MODE_FIXED_POWERED = 1
POWER_MODE_MOBILE_BATTERY = 2
POWER_MODE_FIXED_BATTERY = 3
POWER_MODE_MOBILE_MINIMAL = 4
POWER_MODE_MOBILE_STATIONARY = 5
POWER_MODES = (0, 1, 2, 3, 4, 5)
# GNSS Modes
GNSS_MODES = (0, 1, 2, 10, 11, 12)
GNSS_MODE_GPS = 0
GNSS_MODE_GLONASS = 1
GNSS_MODE_BEIDOU = 2
GNSS_MODE_GPS_GLONASS = 10
GNSS_MODE_GPS_BEIDOU = 11
GNSS_MODE_GLONASS_BEIDOU = 12
# GNSS Dynamic Platform Models
GNSS_DPM_MODES = (0, 2, 3, 4, 5, 6, 7, 8)
GNSS_DPM_PORTABLE = 0
GNSS_DPM_STATIONARY = 2
GNSS_DPM_PEDESTRIAN = 3
GNSS_DPM_AUTOMOTIVE = 4
GNSS_DPM_SEA = 5
GNSS_DPM_AIR_1G = 6
GNSS_DPM_AIR_2G = 7
GNSS_DPM_AIR_4G = 8


class Modem(object):
    """Creates a twin of the IDP modem with common methods.
    
    Attributes:
        serial_port: The name of the serial port e.g. /dev/ttyUSB0
        mobile_id: The unique serial number e.g. 00000000SKYEE3B
        initialized: Boolean configuration status
        hardware_version:
        firmware_version:
        registered:
        blocked:
        snr:
        wakeup_period:
        power_mode:
        gnss_mode:
        gnss_refresh_seconds:
        message_queue_mt:
        message_queue_mo:
        event_callbacks:
        crc:
        s_registers:
        antenna_cut:
        gnss_jamming:
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

    WAKEUP_PERIODS = {
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

    POWER_MODES = {
        'Mobile Powered': 0,
        'Fixed Powered': 1,
        'Mobile Battery': 2,
        'Fixed Battery': 3,
        'Mobile Minimal': 4,
        'Mobile Stationary': 5
    }

    GNSS_MODES = {
        'GPS': 0,               # HW v4
        'GLONASS': 1,           # HW v5
        'BEIDOU': 2,            # HW v5.2
        'GPS+GLONASS': 10,      # UBX-M80xx
        'GPS+BEIDOU': 11,       # UBX-M80xx
        'GLONASS+BEIDOU': 12    # UBX-M80xx
    }

    GNSS_DPM_MODES = {
        'Portable': 0,
        'Stationary': 2,
        'Pedestrian': 3,
        'Automotive': 4,
        'Sea': 5,
        'Air 1g': 6,
        'Air 2g': 7,
        'Air 4g': 8
    }

    # ----------------------- Twinning and helper objects ----------------------------------- #
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
        """
        A private class to manage twin of the modem's S registers

        :param object: [description]
        :type object: [type]
        :return: [description]
        :rtype: [type]
        """
        # Tuples: (name[0], default[1], read-only[2], range[3], description[4], note[5])
        register_definitions = [
            ('S0', 0, True, [0, 255], 'auto answer', 'unused'),
            ('S3', 13, False, [1, 127], 'command termination character', None),
            ('S4', 10, False, [0, 127], 'response formatting character', None),
            ('S5', 8, False, [0, 127], 'command line editing character', None),
            ('S6', 0, True, [0, 255], 'pause before dial', 'unused'),
            ('S7', 0, True, [0, 255],
                'connection completion timeout', 'unused'),
            ('S8', 0, True, [0, 255], 'commia dial modifier time', 'unused'),
            ('S10', 0, True, [0, 255], 'automatic discovery delay', 'unused'),
            ('S31', 80, False, [10, 250], 'DOP threshold (x10)', None),
            ('S32', 25, False, [1, 1000],
                'position accuracy threshold [m]', None),
            ('S33', 0, False, [0, 8], 'default dynamic platform model', None),
            ('S34', 7, True, [0, 255],
                'Doppler dynamic platform model', 'Reserved'),
            ('S35', 0, False, [0, 255], 'static hold threshold [cm/s]', None),
            ('S36', 0, False, [-1, 480], 'standby timeout [min]', None),
            ('S37', 200, False, [1, 1000], 'speed accuracy threshold', None),
            ('S38', 1, True, [0, 0], 'reserved', None),
            ('S39', 0, False, [0, 2], 'GNSS mode', None),
            ('S40', 0, False, [0, 60],
                'GNSS signal satellite detection timeout', None),
            ('S41', 180, False, [60, 1200], 'GNSS fix timeout', None),
            ('S42', 65535, False, [0, 65535],
                'GNSS augmentation systems', 'Query fails'),
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
            ('S64', 42, False, [0, 255],
                'prefix character of CRC sequence', None),
            ('S70', 0, True, [0, 0], 'reserved', 'undocumented'),
            ('S71', 0, True, [0, 0], 'reserved', 'undocumented'),
            ('S80', 0, True, [0, 255], 'last error code', None),
            ('S81', 0, True, [0, 255], 'most recent result code', None),
            ('S85', 22, True, [0, 0], 'temperature', None),
            ('S88', 0, False, [0, 65535], 'event notification control', None),
            # ('S89', 0, False, [0, 65535], 'event notification status', None),
            ('S90', 0, False, [0, 7], 'capture trace define - class', None),
            ('S91', 0, False, [0, 31],
                'capture trace define - subclass', None),
            ('S92', 0, False, [0, 255],
                'capture trace define - initiate', None),
            ('S93', 0, True, [0, 255],
                'captured trace property - data size', None),
            ('S94', 0, True, [0, 255],
                'captured trace property - signed indicator', None),
            ('S95', 0, True, [0, 255],
                'captured trace property - mobile ID', None),
            ('S96', 0, True, [0, 255],
                'captured trace property - timestamp', None),
            ('S97', 0, True, [0, 255],
                'captured trace property - class', None),
            ('S98', 0, True, [0, 255],
                'captured trace property - subclass', None),
            ('S99', 0, True, [0, 255],
                'captured trace property - severity', None),
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
            """
            Twin of a modem S register
            """

            def __init__(self, name, default, read_only, low, high, description, note=None):
                """
                Initializes an S register twin

                :param name: (string) name of the S register e.g. 'S50'
                :param default: (int) default value of the register
                :param read_only: (Boolean)
                :param low: (int) lowest value allowed
                :param high: (int) highest value allowed
                :param description: (string)
                :param note: (string), defaults to None
                """
                self.name = name
                self.default = default
                self.value = default
                self.read_only = read_only
                self.rng = range(low, high+1)
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
                        error = "Attempt to set {} out of range.".format(
                            self.name)
                else:
                    error = "Attempt to write read-only register {}".format(
                        self.name)
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

        def register(self, name):
            """
            Returns the register object based on its name

            :param name: (string) name of the register e.g. 'S50'
            :return: the register object
            :rtype: (SRegister)
            """
            for reg in self.s_registers:
                if reg.name == name:
                    return reg

    class _AtStatistics(object):
        def __init__(self):
            self.connect_attempts = 0
            self.total_connect_attempts = 0
            self.timeouts = 0
            self.total_timeouts = 0
            self.response_times_ms = {
                'gnss': 0,
                'non_gnss': 0,
            }
            self.crc_error_count = 0
            self.timeout_count = 0
        
        def update(self, command, submit_time):
            latency = int((time.time() - submit_time) * 1000)
            if '%GPS' in command:
                category = 'gnss'
            else:
                category = 'non_gnss'
            prior_latency = self.response_times_ms[category]
            if prior_latency == 0:
                average_latency = latency
            else:
                average_latency = int((prior_latency + latency) / 2)
            self.response_times_ms[category] = average_latency

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
            # import time   # TODO: why?
            self.command = at_command
            self.submit_time = time.time()
            self.send_time = None
            self.response_time = None
            self.response_raw = ""
            self.responses = []
            self.response_processed_time = None
            self.response_crc = None
            self.echo_received = False
            self.result = None
            self.result_code = None
            self.error = False
            self.crc_ok = True
            self.timeout = timeout
            self.timed_out = False
            self.callback = callback
            self.retries = retries

    class _PendingMoMessage(object):
        """
        A private class for managing a queue of Mobile-Originated messages submitted via AT command
        Ensures a unique name is assigned based on timestamp submitted

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

    class _PendingLocation(object):
        def __init__(self, name, callback):
            self.name = name
            self.callback = callback
            self.location = nmea.Location()

    def __init__(self,
                 serial_name: str = '/dev/ttyUSB0',
                 use_crc: bool = False,
                 auto_monitor: bool = True,
                 com_connect_interval: int = 6,
                 com_monitor_interval: int = 0,
                 sat_status_interval: int = 15,
                 sat_events_interval: int = 0,
                 sat_mt_message_interval: int = 15,
                 sat_mo_message_interval: int = 5,
                 log: object = None,
                 debug: bool = False,
                 **kwargs):
        """Initializes default attributes.

        Args:
            serial_name: The name of the serial port to use.
            use_crc: Boolean flag set true for long serial cable.
            log: An optional logger, preferably writing to a wrapping file.
            debug: Boolean flag for verbose trace.
            com_connect_interval: The number of seconds between attempts
                to connect to the modem on the serial port.
            sat_status_interval: The number of seconds between queries
                for the control state and SNR.
            sat_mt_message_interval: The number of seconds between queries
                of the Forward/Mobile-Terminated message queue.
            sat_mo_message_interval: The number of seconds between queries
                of the Return/Mobile-Originated message queue when any MO
                messages are pending.
            sat_events_interval: The number of seconds between queries of
                the *events* S-register (see Modem Events)
            kwargs: Optional keyword arguments for example fine-tune the
                serial port settings.
        """
        self._start_time = str(datetime.datetime.utcnow())
        if is_logger(log):
            self.log = log
        else:
            log_name = get_caller_name(depth=1)
            self.log = get_wrapping_logger(name=log_name, debug=debug)
        self._debug = debug
        # serial and connectivity configuration and statistics
        self.serial_port = self._init_serial(serial_name, **kwargs)
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
        self.crc = use_crc
        self.crc_errors = 0
        self.sat_status = self._SatStatus()
        self.hw_event_notifications = self._init_hw_event_notifications()
        self.wakeup_interval = self.WAKEUP_PERIODS['5 seconds']
        self.power_mode = self.POWER_MODES['Mobile Powered']
        self.asleep = False
        self.antenna_cut = False
        self._low_snr = False
        self.system_stats = self._init_system_stats()
        self.gnss_mode = self.GNSS_MODES['GPS']
        self.gnss_continuous = 0
        self.gnss_dpm_mode = self.GNSS_DPM_MODES['Portable']
        self.gnss_stats = self._init_gnss_stats()
        self.gpio = self._init_gpio()
        self.event_callbacks = self._init_event_callbacks()
        # AT command queue ---------------------------------
        self.at_commands_pending = []
        self.at_command_active = None
        self.at_command_parked = None
        self._at_command_active_user_callback = None
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
        # TODO (Geoff) REM self._is_autonomous() = auto_monitor
        self._terminate = False
        self.daemon_threads = []
        self.thread_com_listener = threading.Thread(
            name='com_listener', target=self._listen_serial)
        self.thread_com_listener.daemon = True
        self.daemon_threads.append(self.thread_com_listener.name)
        self.thread_com_at_queue = threading.Thread(
            name='at_queue', target=self._process_pending_at_command)
        self.thread_com_at_queue.daemon = True
        self.daemon_threads.append(self.thread_com_listener.name)
        # --- Timer threads for communication establishment and monitoring
        # self.thread_lock = threading.RLock()   # TODO: deprecate
        self.timer_threads = []
        self.com_connect_interval = com_connect_interval
        self.thread_com_connect = RepeatingTimer(seconds=self.com_connect_interval,
                                                 name='com_connect',
                                                 callback=self._com_connect,
                                                 defer=False)
        self.timer_threads.append(self.thread_com_connect.name)
        # --- Timer threads for self-monitoring
        self.sat_status_interval = sat_status_interval
        self.sat_mt_message_interval = sat_mt_message_interval
        self.sat_events_interval = sat_events_interval
        self.sat_mo_message_interval = sat_mo_message_interval
        # TODO: Low Power override of the above timer intervals
        if self._is_autonomous():
            self.thread_sat_status = RepeatingTimer(seconds=self.sat_status_interval,
                                                    name='sat_status_monitor', 
                                                    callback=self._check_sat_status,
                                                    defer=False)
            self.thread_sat_status.start()
            self.timer_threads.append(self.thread_sat_status.name)
            self.thread_mt_monitor = RepeatingTimer(seconds=self.sat_mt_message_interval,
                                                    name='sat_mt_message_monitor',
                                                    callback=self.mt_message_queue)
            self.thread_mt_monitor.start()
            self.timer_threads.append(self.thread_mt_monitor.name)
            self.thread_mo_monitor = RepeatingTimer(seconds=self.sat_mo_message_interval,
                                                    name='sat_mo_message_monitor',
                                                    callback=self.mo_message_status)
            self.thread_mo_monitor.start()
            self.timer_threads.append(self.thread_mo_monitor.name)
            self.thread_event_monitor = RepeatingTimer(seconds=self.sat_events_interval,
                                                       name='sat_events_monitor',
                                                       callback=self.check_events)
            self.thread_event_monitor.start()
            self.timer_threads.append(self.thread_event_monitor.name)
            self.thread_tracking = RepeatingTimer(seconds=self.tracking_interval,
                                                  name='tracking',
                                                  callback=self._tracking,
                                                  defer=False)
            self.thread_tracking.start()
            self.timer_threads.append(self.thread_tracking.name)
        self.thread_com_listener.start()
        self.thread_com_at_queue.start()
        self.thread_com_connect.start_timer()

    def _is_autonomous(self):
        if (self.sat_status_interval > 0
            or self.sat_mt_message_interval > 0
            or self.sat_mo_message_interval > 0
            or self.sat_events_interval > 0
            or self.tracking_interval > 0):
            #: Autonomous
            return True
        return False

    def terminate(self):
        self.log.debug("Terminated by external call {}".format(
            sys._getframe(1).f_code.co_name))
        end_time = str(datetime.datetime.utcnow())
        self._terminate = True
        if self._is_autonomous():
            for t in threading.enumerate():
                if t.name in self.timer_threads:
                    self.log.debug("Terminating thread {}".format(t.name))
                    t.stop_timer()
                    t.terminate()
                    t.join()
                elif t.name in self.daemon_threads:
                    self.log.debug("Terminating thread {}".format(t.name))
                    t.join()
        try:
            self.serial_port.close()
        except serial.SerialException as e:
            self._handle_error(e)
        self.log.info(
            "*** Statistics from {} to {} ***".format(self._start_time, end_time))
        self.log_statistics()

    def _handle_error(self, error_str):
        error_str = error_str.replace(',', ';')
        self.log.error(error_str)
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
                    self.log.info("Connected to {} at {} baud".format(details,
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
    def _init_at_stats():
        # TODO: track response times per AT command type
        at_cmd_stats = {
            'lastResTime': 0,
            'avgResTime': 0,
        }
        return at_cmd_stats

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
                    self.log.warning("{} event callback overwritten - old:{} new{}"
                                     .format(event, self.event_callbacks[event].__name__, callback.__name__))
                self.event_callbacks[event] = callback
                return True, None
            else:
                return False, "No callback defined"
        else:
            self.log.error("Invalid attempt to register callback event {}, must be in {}".format(
                event, self.events))
            return False, "Invalid event"

    def _on_event(self):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # ------------------------ Connection Management ------------------------------------------------ #
    def _com_connect(self):
        """
        Called on a repeating timer on power up or after connection is lost,
        attempts to establish and restore saved config using ATZ.
        """
        if not self.is_connected and self.at_command_active is None:
            self.at_connect_attempts += 1
            self.log.debug('Attempt ({}) to connect'.format(self.at_connect_attempts))
            self.total_at_connect_attempts += 1
            timeout = int(self.com_connect_interval / 2)
            self.submit_at_command(
                at_command='ATZ', callback=self._cb_com_connect, timeout=timeout)

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
            self.log.debug("Modem connected after {} attempts".format(
                self.at_connect_attempts))
            self.at_connect_attempts = 0
            self._on_connect()
        else:
            self.log.debug("Modem connect attempt {} failed".format(
                self.at_connect_attempts))

    def _on_connect(self):
        """
        Stops trying to establish communications, starts monitoring for communications loss and calls back connect event
        """
        self.thread_com_connect.stop_timer()
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
        self.log.debug("Monitoring communication: {} timeouts".format(self.at_timeouts))
        if self.at_timeouts >= disconnect_timeouts and self.is_connected:
            self.is_connected = False
            self.disconnects += 1
            self.log.warning(
                "AT responses timed out {} times - attempting to reconnect".format(self.at_timeouts))
            self._on_disconnect()

    def _on_disconnect(self):
        """
        Stops monitoring modem operations and communications, and starts trying to re-connect.
        Calls back the disconnect event.
        """
        if self._is_autonomous():
            # TODO: optimize this to allow for new threads
            self.thread_sat_status.stop_timer()
            self.thread_mt_monitor.stop_timer()
            self.thread_event_monitor.stop_timer()
            self.thread_tracking.stop_timer()
            self.thread_com_connect.start_timer()
        self.is_initialized = False
        if self.event_callbacks['disconnect'] is not None:
            self.event_callbacks['disconnect']()

    # ------------------------ SERIAL PORT DATA PROCESSING ------------------------------------------ #
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
                c = ser.read(1).decode()
            else:
                if not parsing_at_response and self.at_command_active is not None:
                    self.log.debug("Awaiting response to {}".format(
                        self.at_command_active.command))
                    parsing_at_response = True
                if parsing_at_response:
                    at_cmd = self.at_command_active
                    if time.time() - at_cmd.send_time > at_cmd.timeout:
                        parsing_at_response = False
                        at_tick = 0
                        self._on_at_timeout(at_cmd)
                    else:
                        if time.time() - at_cmd.send_time >= at_tick + 1:
                            at_tick += 1
                            self.log.debug("Waiting for {} response - tick={}"
                                            .format(at_cmd.command, at_tick))
                time.sleep(CHAR_WAIT)
            if c is not None:
                if ((self.at_command_active is not None 
                    or parsing_at_response)
                    and not parsing_unsolicited):
                    if not parsing_at_response:
                        self.log.debug("Processing response for {}"
                            .format(self.at_command_active.command))
                        parsing_at_response = True
                        self.at_command_active.response_time = time.time()
                    read_str, complete = self._parse_at_response(read_str, c)
                    if complete:
                        parsing_at_response = False
                        at_tick = 0
                        read_str = ""
                        self._on_at_response(self.at_command_active)
                else:
                    if not parsing_unsolicited:
                        self.log.debug('AT command queue depth {}'.format(
                                        len(self.at_commands_pending)))
                        if self.at_command_active is not None:
                            self.log.debug('Active command: {}'.format(
                                            self.at_command_active.command))
                        self.log.warning(
                            'No AT command pending - parsing unsolicited')
                        parsing_unsolicited = True
                    read_str, complete = self._parse_unsolicited(read_str, c)
                    if complete:
                        self._on_unsolicited_serial(read_str)
                        parsing_unsolicited = False
                        read_str = ""

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
        self.at_command_active.response_raw += c
        response_complete = False
        if c == '\r':
            # cases <echo><cr>
            # or <cr>...
            # or <numeric code><cr> (verbose off, no crc)
            if self.at_command_active.command in read_str:
                # case <echo><cr>
                if self.at_command_active.command.upper() == 'ATE0':
                    self.at_config.echo = False
                    self.log.debug(
                        "ATE0 (echo disable) requested - takes effect for next AT command")
                else:
                    self.at_config.echo = True
                if not self.at_command_active.echo_received:
                    self.log.debug(
                        "Echo {} received - removing from raw response".format(read_str.strip()))
                    self.at_command_active.echo_received = True
                else:
                    self.log.warning("Echo {} received more than once - removing from raw message"
                                     .format(read_str.strip()))
                self.at_command_active.response_raw = self.at_command_active.response_raw.replace(
                    read_str, '')
                # <echo><cr> will be not be followed by <lf>
                # can be followed by <text><cr><lf>
                # or <cr><lf><text><cr><lf>
                # or <numeric code><cr>
                # or <cr><lf><verbose code><cr><lf>
                read_str = ""  # clear for next line of parsing
            elif ser.inWaiting() == 0 and read_str.strip() != '':
                if read_str.strip() != '0' and self.at_command_active.command != 'ATV0' and self.at_config.verbose:
                    # case <cr><lf><text><cr>...<lf> e.g. delay between NMEA sentences
                    # or Quiet mode? --unsupported, suppresses result codes
                    self.log.debug(
                        "Assuming delay between <cr> and <lf> of Verbose response...waiting")
                else:
                    # case <numeric code><cr> since all other alternatives should have <lf> or other pending
                    if not self.at_config.verbose:
                        self.log.debug("Assuming receipt of <numeric code = {}><cr> with Verbose undetected"
                                       .format(read_str.strip()))
                        self.at_config.verbose = False
                    self.at_command_active.result_code = read_str.strip()
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
                self.at_command_active.result = read_str.strip()
                if not self.at_config.crc or ser.inWaiting() == 0:  # no checksum pending...response complete
                    response_complete = True
                else:
                    read_str = ""  # continue parsing next line (checksum)
            elif '*' in read_str and len(read_str.strip()) == 5:
                # <*crc><cr><lf>
                self.at_config.crc = True
                self.at_command_active.response_crc = read_str.replace(
                    '*', '').strip()
                self.log.debug(
                    "Found CRC {} - removing from raw response".format(read_str.strip()))
                self.at_command_active.response_raw = self.at_command_active.response_raw.replace(
                    read_str, '')
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
                        self.at_command_active.responses.append(
                            read_str.strip())  # don't include \r\n in callback
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
        if self.event_callbacks['unsolicited_serial'] is not None:
            self.event_callbacks['unsolicited_serial'](read_str)
        else:
            self.log.warning(
                "No callback defined for unsolicited serial: {}".format(read_str))

    # ------------------------- AT Command handling -------------------------------------------------- #
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
        command = self._PendingAtCommand(
            at_command=at_command, callback=callback, timeout=timeout, retries=retries)
        self.log.debug("Submitting command {} at {} with timeout {}s calling back to {}"
                       .format(at_command, round(command.submit_time, 3), timeout,
                               callback.__name__ if callback is not None else None))
        if at_command in self.at_commands_pending:
            self.log.warning("Prior command {} queued - discarding".format(at_command))
        else:
            if jump_queue:
                # Not inserted at 0 since the prior command needs to complete
                self.at_commands_pending.insert(1, command)
            else:
                self.at_commands_pending.append(command)
        queue_debug = []
        for c in self.at_commands_pending:
            queue_debug.append('{}'.format(c.command))
        self.log.debug("AT command queue: {}".format(queue_debug))

    def _process_pending_at_command(self):
        """Checks the queue of pending AT commands and sends on serial if one is pending and none are active"""
        while self.serial_port.isOpen() and not self._terminate:
            if len(self.at_commands_pending) > 0:
                if self.at_command_active is None:
                    # for cmd in self.at_commands_pending:
                    #     self.log.debug("[{}]: {}".format(self.at_commands_pending.index(cmd), cmd.command))
                    at_cmd = self.at_commands_pending[0] if len(
                        self.at_commands_pending) > 0 else None
                    if at_cmd is not None:
                        self.log.debug("{} Pending commands - processing: {}"
                                       .format(len(self.at_commands_pending), at_cmd.command))
                        at_cmd.send_time = time.time()   #: Should be submit_time
                        self.at_command_active = at_cmd
                        if self.at_config.crc:
                            to_send = at_cmd.command + \
                                ('*'+self.get_crc(at_cmd.command)
                                 if self.at_config.crc else '')
                        else:
                            to_send = at_cmd.command
                        if "AT%CRC=1" in at_cmd.command.upper():
                            self.at_config.crc = True
                            self.log.debug("CRC enabled for next command")
                        elif "AT%CRC=0" in at_cmd.command.upper():
                            self.at_config.crc = False
                            self.log.debug("CRC disabled for next command")
                        self.log.debug("Sending {} at {} with timeout {} seconds"
                                       .format(to_send,
                                       round(at_cmd.send_time, 3),
                                       at_cmd.timeout))
                        self.serial_port.write((to_send + '\r').encode())
                # else:
                #     self.log.debug("Processing AT command: {}".format(self.at_command_active.command))

    def _on_at_response(self, response):
        """
        Called when a response parsing completes. Validates CRC if present.
        If a response ERROR is detected, requests the result code immediately jumping the AT queue.
        Updates debug statistics and sends the final completed response for processing.

        :param response: (_PendingAtCommand) the current pending command
        """
        response.response_processed_time = time.time()
        self.at_timeouts = 0
        self.log.debug(
            "Processing AT response: {}".format(vars(response)))
        if response.response_crc is not None:
            if not self.at_config.crc:
                self.log.warning(
                    "Unexpected CRC response received, setting CRC flag True")
                self.at_config.crc = True
            self.log.debug("Raw response to validate CRC: {}"
                           .format(response.response_raw.replace('\r', '<cr>').replace('\n', '<lf>')))
            expected_crc = self.get_crc(response.response_raw)
            self.log.debug("Expected CRC: *{}".format(expected_crc))
            if response.response_crc != expected_crc:
                response.crc_ok = False
                self.crc_errors += 1
                self.log.warning(
                    "Bad CRC received: *{} - expected: *{}".format(response.response_crc, expected_crc))
                if response.result_code == '100' or not self.at_config.crc and response.response_crc is not None:
                    self.log.info(
                        "CRC found on response but not explicitly configured...capturing config")
                    self.at_config.crc = True
        if response.result == 'ERROR' or response.result_code == '4':
            response.error = True
            self.at_command_parked = response
            self.log.warning("Error detected on response to {},"
                             " checking last error code".format(
                                self.at_command_parked.command))
            # TODO: fix problem with queueing second response before first response is closed
            self.submit_at_command(
                at_command='ATS80?', callback=self._cb_get_result_code, jump_queue=True)
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
            error_desc = self.at_err_result_codes[str(result_code)]
            self.at_command_parked.result_code = result_code
            self.at_command_parked.result = error_desc
            self.log.debug("Processing result code {} for: {}".format(
                error_desc, vars(self.at_command_parked)))
            self._complete_pending_command(self.at_command_parked)
        else:
            self.log.error(
                "Unhandled exception: {} {}".format(request, responses))

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
        # if self.serial_port.inWaiting(): clear input buffer
        response.timed_out = True
        self.at_timeouts += 1
        self.at_timeouts_total += 1
        self._com_monitor()
        self._complete_pending_command(response)

    def _is_at_pending(self, response):
        is_pending = False
        if len(self.at_commands_pending) > 0:
            pending = self.at_commands_pending[0]
            self.log.debug('Expected pending {} vs response {}'.format(
                pending.command, response.command))
        else:
            pending = None
        if pending is not None and response.command == pending.command and response.submit_time == pending.submit_time:
            is_pending = True
        return is_pending

    def _is_at_active(self, response):
        is_active = False
        active = self.at_command_active if self.at_command_active is not None else None
        if active is not None and response.command == active.command and response.submit_time == active.submit_time:
            is_active = True
        return is_active

    def _is_at_parked(self, response):
        parked = self.at_command_parked if self.at_command_parked is not None else None
        if parked is not None and response.command == parked.command and response.submit_time == parked.submit_time:
            return True
        return False

    def _retry_command(self, response):
        if response.retries > 0:
            self.log.info("Retrying command {}".format(response.command))
            response.retries -= 1
            self.submit_at_command(at_command=response.command, callback=response.callback,
                                   timeout=response.timeout, retries=response.retries - 1)
        else:
            self.log.debug(
                "No retries remaining for command {}".format(response.command))

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
        # TODO: fix error handling and recovery for failed AT e.g. AT%MGRT
        if self._is_at_pending(response):
            discard = self.at_commands_pending.pop(0)
            if discard is not None:
                self.log.debug("Pending AT command buffer FIFO popped ({}) - {} pending AT commands"
                               .format(discard.command, len(self.at_commands_pending)))
                queue_debug = []
                for c in self.at_commands_pending:
                    queue_debug.append('{}'.format(c.command))
                self.log.debug("AT command queue: {}".format(queue_debug))
            else:
                self.log.error(
                    "Tried to pop pending command from AT queue but got nothing")
        if self._is_at_parked(response):
            self.log.debug("Processing error result for {}: {}".format(
                response.command, response.result))
            if response.result is not None and response.result != 'ERROR':
                self.log.debug(
                    "Closing parked command {}".format(response.command))
                complete = True
                self.at_command_parked = None
                # TODO (Geoff) don't want to retry in all cases
                self._retry_command(response)
            else:
                self.log.debug(
                    "Awaiting closure of parked command {}".format(response.command))
        elif self._is_at_active(response):
            if response.timed_out or not response.crc_ok:
                self._retry_command(response)
            ''' TODO (Geoff) this can't happen due to above condition
            if self._is_at_parked(response):
                self.log.debug(
                    "Awaiting error code for {}".format(response.command))
            else:'''
            complete = True
        else:
            self.log.warning(
                "Did not find {} active or parked".format(response.command))
        if len(self.at_commands_pending) > 0:
            self.log.debug("Next pending command: {}".format(
                self.at_commands_pending[0].command))
        else:
            self.log.debug("No pending AT commands")
        self.log.debug("Clearing active command {}".format(response.command))
        self.at_command_active = None
        if complete:
            if response.callback is not None:
                self.log.debug("Calling back to {}".format(
                    response.callback.__name__))
                if response.timed_out:
                    response.callback(False, "TIMED_OUT", response.command)
                elif not response.crc_ok:
                    response.callback(
                        False, "RESPONSE_CRC_ERROR", response.command)
                elif response.error:
                    response.callback(False, response.result, response.command)
                else:
                    response.callback(
                        True, response.responses, response.command)
            else:
                self.log.warning(
                    "No callback defined for command {}".format(response.command))
        else:
            self.log.warning(
                "Message incomplete, awaiting further processing...(probably ATS80?)")

    def _update_stats_at_response(self, response):
        """
        Updates the last and average AT command response time statistics.

        :param response: (_PendingAtCommand) the response to the AT command that was sent

        """
        if response.response_time is None:
            # TODO: shouldn't need this workaround
            self.log.debug('(fixme) Missed received time for {}'
                .format(response.command))
            response.response_time = response.response_processed_time
        at_response_time_ms = int((
            response.response_time - response.send_time) * 1000)
        self.system_stats['lastATResponseTime_ms'] = at_response_time_ms
        self.log.debug("Response time for {}: {} [ms]".format(
            response.command, at_response_time_ms))
        if self.system_stats['avgATResponseTime_ms'] == 0:
            self.system_stats['avgATResponseTime_ms'] = at_response_time_ms
        else:
            self.system_stats['avgATResponseTime_ms'] = \
                int((
                    self.system_stats['avgATResponseTime_ms'] + at_response_time_ms) / 2)
        # TODO: categorize AT commands for characterization
        if 'AT%GPS' in response.command.upper():
            request_parts = response.command.split(',')
            sentences = []
            for part in request_parts:
                if part.replace('"', '') in ['RMC', 'GGA', 'GSA', 'GSV']:
                    sentences.append(part.replace('"', ''))
            self.log.debug("Get GNSS information processed for {}".format(
                sentences))
        elif 'AT%MGFG' in response.command.upper():
            self.log.debug("Get To-Mobile message processed")
        elif 'ATS' in response.command.upper():
            self.log.debug("S-register operation {} processed".format(
                response.command[2:].replace('?', '')))
        elif 'AT%EVMON' in response.command.upper():
            self.log.debug("Event Log Monitor {} processed".format(
                response.command[10:]))
        elif 'AT%EVNT' in response.command.upper():
            self.log.debug("Event Log Get {} processed".format(
                response.command[9:]))

    # ---------------------- Modem initialization & twinning ---------------------------------------- #
    def _init_modem(self, step=1):
        # TODO: optimize to a single command query/response
        self.log.debug("Initializing modem...step {}".format(step))
        if step == 1:   # Restore default configuration
            # TODO: consider using Factory defaults (AT&F) instead of NVM for first initialization?
            self.submit_at_command(
                at_command='AT&V', callback=self._cb_get_config, timeout=3)
        # enable CRC if explicitly during object creation (used for long serial cable)
        elif step == 2:
            if self.crc and not self.at_config.crc:
                self.submit_at_command(
                    at_command="AT%CRC=1", callback=self._cb_configure_crc, timeout=3)
            elif not self.crc and self.at_config.crc:
                self.submit_at_command(
                    at_command='AT%CRC=0', callback=self._cb_configure_crc, timeout=3)
            else:
                self.log.info("CRC already {} in configuration".format(
                    "enabled" if self.crc else "disabled"))
                self._init_modem(step=3)
        # enable Verbose since response codes are only OK (0) or ERROR (4)
        elif step == 3:
            if not self.at_config.verbose:
                self.submit_at_command(
                    at_command='ATV1', callback=self._cb_configure_verbose, timeout=3)
            else:
                self.log.info("Verbose already {} in configuration".format(
                    "enabled" if self.at_config.verbose else "disabled"))
                self._init_modem(step=4)
        elif step == 4:   # get mobileID, versions
            self.submit_at_command(
                at_command='AT+GSN;+GMR', callback=self._cb_get_modem_info, timeout=3)
        elif step == 5:   # get key parameters from S-registers
            self.log.warning(
                "TODO: get S-register values for notifications, wakeup, power mode, gnss")
            ''' Volatile Registers not returned by AT&V:
            volatile_registers = {
            'S39': 'GNSS Mode',
            'S41': 'GNSS Fix Timeout',
            'S42': 'GNSS Augmentation Systems',   # returns ERROR from Modem Simulator
            'S51': 'Wakeup Interval',
            'S55': 'GNSS Continuous',
            'S56': 'GNSS Jamming Status',
            'S57': 'GNSS Jamming Indicator',
            }
            '''
            self.submit_at_command(
                at_command='ATS39? S41? S51? S55? S56? S57?', callback=self._cb_get_volatile_sreg)
            self.get_event_notification_control(init=True)
            # self.get_wakeup_interval()
            # self.get_power_mode()
            # self.get_gnss_mode()
            # self.get_gnss_continuous()
            # self.get_gnss_dpm()
            # self._init_modem(step=6)
        elif step == 6:   # save config to NVM
            self.submit_at_command(
                at_command='AT&W', callback=self._cb_init_nvm, timeout=3)
        else:
            self.log.warning(
                "Modem initialization called with invalid step {}".format(step))

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
                    self.log.warning(
                        "Unknown config parameter: {}".format(param))
            reg_config = responses[2].split(" ")
            for c in reg_config:
                name = c.split(":")[0]
                reg = self.s_registers.register(name)
                value = int(c.split(":")[1])
                if value != reg.default:
                    self.log.warning("Updating {}:{} value={} (default={})".format(
                        name, reg.description, value, reg.default))
                    reg.set(value)
        self._init_modem(
            step=step+1) if success else self._init_modem(step=step)

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
            fw_ver, hw_ver, at_ver = responses[1].lstrip(
                '+GMR:').strip().split(",")
            self.log.info(
                "Versions - Hardware: {} | Firmware: {} | AT: {}".format(hw_ver, fw_ver, at_ver))
            self.hardware_version = hw_ver if hw_ver != '' else 'unknown'
            self.software_version = fw_ver if fw_ver != '' else 'unknown'
            self.at_version = at_ver if at_ver != '' else 'unknown'
        self._init_modem(
            step=step+1) if success else self._init_modem(step=step)

    def _cb_get_volatile_sreg(self, valid_response, responses, request):
        step = 5
        success = False
        if valid_response:
            success = True
            # Note that last list element will be '' due to last ?
            s_regs = request.replace('AT', '').split('?')
            for i in range(0, len(s_regs)-1):
                name = s_regs[i].strip()   # remove spaces left by split
                reg = self.s_registers.register(name)
                self.log.debug("Processing {}:{}".format(name, reg.description))
                value = int(responses[i])
                if value != reg.default:
                    self.log.warning("Updating {}:{} value={} (default={})".format(
                        name, reg.description, value, reg.default))
                    reg.set(value)
        self._init_modem(
            step=step+1) if success else self._init_modem(step=step)

    def _cb_configure_crc(self, valid_response, responses, request):
        if valid_response:
            self.log.debug("CRC {}".format(
                'enabled' if '=1' in request else 'disabled'))
        else:
            self.log.error("Error setting CRC: {}".format(responses))

    def _cb_configure_verbose(self, valid_response, responses, request):
        if valid_response:
            self.log.debug("Verbose {}".format(
                'enabled' if '=1' in request else 'disabled'))
        else:
            self.log.error("Error setting Verbose: {}".format(responses))

    def _cb_init_nvm(self, valid_response, responses, request):
        if valid_response:
            self.log.info("Initialization complete")
            self.is_initialized = True
            self._on_initialized()
        else:
            self.log.error("Failed to write NVM - {}".format(responses))

    def _on_initialized(self):
        if self._is_autonomous():
            self.thread_sat_status.start_timer()
            self.thread_mt_monitor.start_timer()
            self.thread_mo_monitor.start_timer()
            self.thread_tracking.start_timer()
            if self.tracking_interval > 0:
                self.tracking_setup(
                    interval=self.tracking_interval, on_location=self.on_location)
            # self.thread_event_monitor.start_timer()
            self.log.info("Event notification monitoring not enabled")
        else:
            self.log.info(
                "Automonous mode disabled, user application must query modem actively")

    # ---------------------- SATELLITE STATUS MONITORING -------------------------------------------- #
    def _check_sat_status(self):
        self.log.debug(
            "Monitoring satellite status - current status: {}".format(self.sat_status.ctrl_state))
        # S122: satellite trace status
        # S116: C/N0
        self.submit_at_command(
            'ATS90=3 S91=1 S92=1 S122? S116?', callback=self._cb_check_sat_status)

    # TODO: sort out Low SNR Threshold etc
    def _cb_check_sat_status(self, valid_response, responses, request):
        LOW_SNR_THRESHOLD = 38.0
        # TODO (Geoff) valid_response needs to be checked for format
        if valid_response:
            self.log.debug("Current satellite status: {}".format(
                self.ctrl_states[int(responses[0])]))
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
                        blockage_duration = int(
                            time.time() - self.system_stats['lastBlockStartTime'])
                        if self.system_stats['avgBlockageDuration'] > 0:
                            self.system_stats['avgBlockageDuration'] \
                                = int((blockage_duration + self.system_stats['avgBlockageDuration']) / 2)
                        else:
                            self.system_stats['avgBlockageDuration'] = blockage_duration
                            sat_status_change = 'unblocked'
                    if not self.sat_status.registered:
                        self.sat_status.registered = True
                        if old_sat_ctrl_state != 'Stopped':
                            self.log.debug("Modem registered")
                            self.system_stats['nRegistration'] += 1
                            if self.system_stats['lastRegStartTime'] > 0:
                                registration_duration = int(
                                    time.time() - self.system_stats['lastRegStartTime'])
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
                    gnss_duration = int(
                        time.time() - self.system_stats['lastGNSSStartTime'])
                    self.log.info(
                        "GNSS acquired in {} seconds".format(gnss_duration))
                    if self.system_stats['avgGNSSFixDuration'] > 0:
                        self.system_stats['avgGNSSFixDuration'] \
                            = int((gnss_duration + self.system_stats['avgGNSSFixDuration']) / 2)
                    else:
                        self.system_stats['avgGNSSFixDuration'] = gnss_duration
                    if new_sat_ctrl_state not in ['Stopped', 'Blocked', 'Active']:
                        sat_status_change = 'new_gnss_fix'
                    else:
                        self.log.debug(
                            "GNSS fix implied by state transition to {}".format(new_sat_ctrl_state))
                if old_sat_ctrl_state == 'Downloading Bulletin Board' \
                        and new_sat_ctrl_state not in ['Stopped', 'Blocked']:
                    bulletin_duration = int(
                        time.time() - self.system_stats['lastBBStartTime'])
                    self.log.info(
                        "Bulletin Board downloaded in {} seconds".format(bulletin_duration))
                    if self.system_stats['avgBBReacquireDuration'] > 0:
                        self.system_stats['avgBBReacquireDuration'] \
                            = int((bulletin_duration + self.system_stats['avgBBReacquireDuration']) / 2)
                    else:
                        self.system_stats['avgBBReacquireDuration'] = bulletin_duration
                self._on_sat_status_change(sat_status_change)
            # second response S116 = C/No
            c_n0 = int(responses[1]) / 100.0
            if c_n0 <= LOW_SNR_THRESHOLD and not self._low_snr:
                # TODO: generate event
                self.log.warning("Low SNR {} dB detected".format(c_n0))
                self._low_snr = True
            elif c_n0 > LOW_SNR_THRESHOLD and self._low_snr:
                self.log.info('Adequate SNR {} dB recovered'.format(c_n0))
                self._low_snr = False
            self.log.debug("SNR: {} dB".format(c_n0))
            if self.system_stats['avgCN0'] == 0:
                self.system_stats['avgCN0'] = c_n0
            else:
                self.system_stats['avgCN0'] = round(
                    (self.system_stats['avgCN0'] + c_n0) / 2.0, 2)

    def _on_sat_status_change(self, event):
        if event in self.events:
            if self.event_callbacks[event] is not None:
                self.log.info("Calling back for {} to {}".format(
                    event, self.event_callbacks[event].__name__))
                self.event_callbacks[event](event)
            else:
                self.log.info("No callback defined for {}".format(event))
        else:
            if self.event_callbacks['satellite_status_change'] is not None:
                self.log.info("Calling back for satellite_status_change to {}"
                              .format(self.event_callbacks[event].__name__))
                self.event_callbacks['satellite_status_change'](event)
            else:
                self.log.info(
                    "No callback defined for satellite_status_change")

    def get_sat_status(self, callback):
        # TODO: validate callback is a function
        self.log.info("User queried Satellite Status")
        if self.sat_status_pending_callback is not None:
            self.log.warning("Overwriting pending status")
        self.sat_status_pending_callback = callback
        self.submit_at_command(
            'ATS90=3 S91=1 S92=1 S122? S116?', callback=self._cb_get_sat_status)
    
    def _cb_get_sat_status(self, valid_response, responses, request):
        if valid_response:
            ctrl_state = self.ctrl_states[int(responses[0])]
            snr = round(int(responses[1]) / 100.0, 2)
            self.log.debug("Calling back to {}".format(
                            self.sat_status_pending_callback))
            self.sat_status_pending_callback(ctrl_state, snr)
            self.sat_status_pending_callback = None
        else:
            self.log.error("Invalid response to get_sat_status")

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
        return self.mo_message_send(mo_message, callback, priority)

    def mo_message_send(self, mo_message, callback=None, priority=None):
        """
        Submits a message on the AT command interface and calls back when complete.

        :param mo_message: (MobileOriginatedMessage)
        :param callback: (function)
        :param priority: (int(
        :return: (string) a unique 8-character name for the message based on the time it was submitted

        """
        if isinstance(mo_message, MobileOriginatedMessage):
            p_msg = self._PendingMoMessage(
                message=mo_message, callback=callback)
            self.log.debug("User submitted message name: {} mapped to {}".format(
                            mo_message.name, p_msg.q_name))
            mo_message.priority = priority if priority is not None else mo_message.priority
            msg_min = mo_message.min if mo_message.min is not None else None
            self.mo_msg_queue.append(p_msg)
            self.submit_at_command(
                at_command='AT%MGRT={},{},{}{},{},{}'.format(
                    '"{}"'.format(p_msg.q_name),
                    mo_message.priority, 
                    mo_message.sin, 
                    '.{}'.format(mo_message.min) if msg_min is not None else '',
                    mo_message.data_format,
                    mo_message.data(mo_message.data_format,
                                    include_min=False if msg_min is not None else True)),
                callback=self._cb_send_message)
            return p_msg.q_name
        else:
            self._handle_error(
                "Message submitted must be type MobileOriginatedMessage")

    def _cb_send_message(self, valid_response, responses, request):
        if valid_response:
            msg_name = request.split('=', 1)[1].split(',')[0].replace('"', '')
            self.log.info(
                "Mobile-Originated message {} submitted".format(request))
        else:
            msg_name = request.split('\"')[1]
            self.log.error(
                "MO Message {} failed: {}".format(msg_name, responses))
            # TODO: de-queue failed message?
            for p_msg in self.mo_msg_queue:
                if p_msg.q_name == msg_name:
                    if p_msg.callback is not None:
                        p_msg.callback(success=False, message=None)
                    self.mo_msg_queue.remove(p_msg)
                    break

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
            self.log.debug("{} MO messages queued ({})".format(
                len(self.mo_msg_queue), msg_list))
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
                    name, msg_no, priority, sin, state, size, sent_bytes = res.replace(
                        '%MGRS:', '').strip().split(',')
                    del msg_no  # unused
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
                                           .format(msg.name, p_msg.q_name, self.message_state_get(state)))
                            if state != msg.state:
                                msg.state = state
                                if state in (TX_COMPLETE, TX_FAILED):
                                    p_msg.complete_time = time.time()
                                    mo_msg_latency = int(p_msg.complete_time - p_msg.submit_time)
                                    if state == TX_FAILED:
                                        p_msg.failed = True
                                        self.mo_msg_failed += 1
                                    else:
                                        self.log.info("Message {} completed".format(p_msg.q_name))
                                    self.log.debug(
                                        "Removing {} from pending message queue".format(p_msg.q_name))
                                    self.mo_msg_queue.remove(p_msg)
                                    self._update_stats_mo_messages(size, mo_msg_latency)
                                    # TODO: calculate statistics for MO message transmission times
                                    if p_msg.callback is not None:
                                        p_msg.callback(success=True,
                                                       message=(msg.name, p_msg.q_name, msg.state, msg.size))
                                    else:
                                        self.log.warning(
                                            "No callback defined for {}".format(p_msg.q_name))
                                else:
                                    self.log.debug("MO message {} state changed to: {}"
                                                   .format(p_msg.q_name, self.message_state_get(state)))
                            break
                else:
                    self.log.debug("MO Message(s) completed")
        else:
            self.log.warning(
                "Invalid response to AT%MGRS: {}".format(responses))

    def mt_message_queue(self, user_callback=None):
        callback = self._cb_check_mt_messages if user_callback is None else user_callback
        self.submit_at_command(at_command='AT%MGFN', callback=callback)

    def _cb_check_mt_messages(self, valid_response, responses, request):
        if valid_response and '%MGFN' in responses[0]:
            self.log.debug("Processing AT%MGFN {}".format(responses))
            new_messages = []
            for res in responses:
                # Format of responses should be: %MGFN: "<name>",<msg_no>,<priority>,<sin>,<state>,<size>,<bytes_rcvd>
                msg_pending = res.replace('%MGFN:', '').strip()
                # TODO: skip if no message
                if 'FM' in msg_pending:
                    name, number, priority, sin, state, size, bytes_read = \
                        msg_pending.split(',')
                    del number  # unused
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
                                self.log.debug(
                                    "Pending message {} already in queue".format(name))
                                break
                        if not queued:
                            new_messages.append(name)
                            p_msg = self._PendingMtMessage(
                                message=None, q_name=name, sin=sin, size=size)
                            self.mt_msg_queue.append(p_msg)
                            self._update_stats_mt_messages(size)
                    else:
                        self.log.debug(
                            "Message {} not complete ({}/{} bytes)".format(name, bytes_read, size))
                else:
                    self.log.debug("No pending MT messages")
            if len(new_messages) > 0:
                if self.event_callbacks['new_mt_message'] is not None:
                    self.log.debug("Calling back to {}"
                                    .format(self.event_callbacks['new_mt_message'].__name__))
                    self.event_callbacks['new_mt_message'](
                        self.mt_msg_queue)
                else:
                    self.log.warning(
                        "No callback registered for new MT messages")
                if len(self._mt_message_callbacks) > 0:
                    for msg_name in new_messages:
                        for msg in self.mt_msg_queue:
                            if msg.q_name == msg_name:
                                for tup in self._mt_message_callbacks:
                                    if tup[0] == msg.sin:
                                        self.mt_message_get(
                                            name=name, callback=tup[1])
                                        break  # for tup
                                break  # for msg
        else:
            self.log.error("Invalid %MGFN response {}".format(responses))

    def _update_stats_mt_messages(self, size):
        self.mt_msg_count += 1
        if self.system_stats['avgMTMsgSize'] == 0:
            self.system_stats['avgMTMsgSize'] = size
        else:
            self.system_stats['avgMTMsgSize'] = int((self.system_stats['avgMTMsgSize'] + size) / 2)

    def get_message(self, name, callback, data_format=FORMAT_B64):
        return self.mt_message_get(name, callback, data_format)

    def mt_message_get(self, name, callback, data_format=FORMAT_B64):
        found = False
        for m in self.mt_msg_queue:
            if m.q_name == name:
                found = True
                m.callback = callback
                if data_format is None:
                    data_format = FORMAT_HEX if m.size <= 100 else FORMAT_B64
                self.log.info("Retrieving MT message {}".format(name))
                self.submit_at_command(at_command='AT%MGFG=\"{}\",{}'.format(name, data_format),
                                       callback=self._cb_get_mt_message)
                break
        return found, "Message not found in MT queue" if not found else None

    def _cb_get_mt_message(self, valid_response, responses, request):
        if valid_response:
            # Response format: "<fwdMsgName>",<msgNum>,<priority>,<sin>,<state>,<length>,<dataFormat>,<data>
            #  where <data> is surrounded by quotes if dataFormat is text
            if len(responses) > 1:
                self.log.warning("Unexpected responses {}".format(responses))
            response = responses[0].replace('%MGFG:', '').strip()
            q_name, msg_num, priority, sin, state, length, data_format, data = response.split(
                ',')
            del state  # unused
            q_name = q_name.replace('\"', '')
            priority = int(priority)
            if priority != PRIORITY_MT:
                # T203 states that priority is always 0 for Mobile-Terminated messages
                self.log.warning(
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
                        self.log.debug(
                            "Calling back to {}".format(m.callback.__name__))
                        m.callback(mt_msg)
                    else:
                        self.log.error(
                            "No callback defined for message {}".format(q_name))
                    break
        else:
            self.log.error("Invalid response ({})".format(responses))

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
            self.log.error("SIN must be integer in range 0..255")

    def mt_message_callback_remove(self, sin):
        """Removes the specified SIN from the callback list"""
        for tup in self._mt_message_callbacks:
            if tup[0] == sin:
                self._mt_message_callbacks.remove(tup)

    def mt_message_remove(self, name):
        if name in self.mt_msg_queue:
            self.log.debug("Removing MT message {}".format(name))
            self.submit_at_command(at_command='AT%MGFM=\"{}\"'.format(name),
                                   callback=self._cb_mt_message_remove)
            pass

    def _cb_mt_message_remove(self, valid_response, responses, request):
        # TODO: test
        self.log.warning("Message remove callback not implemented")
        msg_name = request.split('=')[1]
        if valid_response:
            self.log.debug(
                "Mobile-Terminated message removed {}".format(request))
            for p_msg in self.mt_msg_queue:
                if p_msg.q_name == msg_name:
                    self.mt_msg_queue.remove(p_msg)
                    break
        else:
            msg_name = request.split('\"')[1]
            self.log.error(
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
            self.log.warning(
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
        self.log.debug("Updated event notifications: {}".format(
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
                    self.log.info("{} event notification {}".format(
                        key, "enabled" if value else "disabled"))
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
            self.log.warning(
                "Failed to update event notifications control S88: {}".format(responses))
            self.get_event_notification_control()

    def check_events(self, user_callback=None, events=['ALL']):
        """
        Function to be called by microcontroller when event pin is asserted.

        NOT implemented.  TODO: set up event callbacks
        """
        self.log.warning("CHECK ATS89? NOT IMPLEMENTED")
        if user_callback is None:
            callback = self._cb_check_events
        else:
            callback = user_callback
        self.submit_at_command('ATS89?', callback=callback)

    def _cb_check_events(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))
        if valid_response:
            self.log.warning("FUNCTION SHOULD BE TRIGGERED BY GPIO ASSERTION")
            # TODO: parse bitmap for the various events, check against registered notifications & act
        else:
            self.log.error("Error checking S89 events: {}".format(responses))

    def _on_hw_event(self, event):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # --------------------- NMEA/LBS OPERATIONS --------------------------------------------------- #
    # TODO: set up periodic tracking pushed with callbacks, and using %TRK
    def set_gnss_mode(self, gnss_mode=GNSS_MODE_GPS):
        if gnss_mode in GNSS_MODES:
            if int(self.s_registers.register('S39').get()) == gnss_mode:
                self.log.debug("GNSS mode already set to {}".format(gnss_mode))
            else:
                self.submit_at_command(
                    'ATS39={}'.format(gnss_mode), callback=None)
                # TODO: some risk that write fails and twin is no longer sychronized
                self.s_registers.register('S39').set(gnss_mode)
        else:
            self.log.error("Invalid GNSS mode {}".format(gnss_mode))

    def get_gnss_mode(self):
        """Gets the GNSS mode (S39, default 0) and stores in the Modem instance."""
        return int(self.s_registers.register('S39').get())

    def set_gnss_dynamic_mode(self, dpm_mode=GNSS_DPM_PORTABLE):
        if dpm_mode in GNSS_DPM_MODES:
            if int(self.s_registers.register('S33').get()) == dpm_mode:
                self.log.debug("GNSS mode already set to {}".format(dpm_mode))
            else:
                self.submit_at_command(
                    'ATS33={}'.format(dpm_mode), callback=None)
                # TODO: some risk that write fails and twin is no longer sychronized
                self.s_registers.register('S33').set(dpm_mode)
        else:
            self.log.error(
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
                self.log.debug(
                    "GNSS refresh interval already set to {}".format(seconds))
            else:
                self.submit_at_command(at_command='AT%TRK={}{}'.format(
                                            seconds, doppler_str),
                                       callback=self._cb_set_gnss_continuous)
        else:
            self.log.error(
                "Invalid GNSS refresh interval - must be integer in range 0..30 (seconds)")

    def _cb_set_gnss_continuous(self, valid_response, responses, request):
        if valid_response:
            seconds = int(request.split('=')[1])
            self.s_registers.register('S55').set(seconds)
        else:
            self.log.error("Error setting GNSS continuous {}".format(request))
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
                self.log.error(
                    "Unsupported NMEA sentence: {}".format(sentence))
        # TODO: manage multiple _PendingLocation using a queue
        if self.location_pending is None:
            self.log.debug("New Location request pending")
            self.location_pending = self._PendingLocation(
                name=name, callback=callback)
            self.submit_at_command(at_command='AT%GPS={},{},{}'.format(stale_secs, wait_secs, req_sentences),
                                   callback=self._cb_get_nmea, timeout=wait_secs+5)
            self.gnss_stats['nGNSS'] += 1
            self.gnss_stats['lastGNSSReqTime'] = int(time.time())
        else:
            self.log.warning(
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
                gnss_fix_duration = int(time.time()) - \
                    self.gnss_stats['lastGNSSReqTime']
                if self.gnss_stats['avgGNSSFixDuration'] > 0:
                    self.gnss_stats['avgGNSSFixDuration'] = int((gnss_fix_duration +
                                                                 self.gnss_stats['avgGNSSFixDuration']) / 2)
                else:
                    self.gnss_stats['avgGNSSFixDuration'] = gnss_fix_duration
                try:
                    self.location_pending.location = nmea.location_get(nmea_data_set=nmea_data_set)
                    if self.location_pending is not None and self.location_pending.callback is not None:
                        self.location_pending.callback(
                            self.location_pending.location)
                    else:
                        self.log.warning(
                            "No callback defined for pending location")
                    self.location_pending = None
                except Exception as e:
                    self.log.error(e)
        else:
            self.log.error("Error getting location: {}".format(responses))
            if 'TIMEOUT' in responses:
                # TODO: set up heuristic/backoff on timed out responses
                self.gnss_stats['timeouts'] += 1
                time.sleep(5)
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
                self.log.info("Tracking disabled")
                self.thread_tracking.stop_timer()
                self.tracking_interval = 0
                self.set_gnss_continuous_interval(seconds=0)
            else:
                if interval <= 30:
                    refresh = int(interval/2)
                    self.log.debug(
                        "Setting GNSS continuous mode at {} seconds refresh".format(refresh))
                else:
                    refresh = 0
                    self.log.debug(
                        "Disabling GNSS continuous mode for interval {}s".format(interval))
                self.set_gnss_continuous_interval(seconds=refresh)
                self.log.info(
                    "Tracking interval set to {} seconds".format(interval))
                self.thread_tracking.change_interval(interval)
        else:
            self.log.error('Invalid tracking interval {}'.format(interval))

    def _tracking(self):
        self.get_location(callback=self._cb_tracking,
                          name='tracking', fix_age=self.tracking_interval)

    def _cb_tracking(self, loc):
        if self.on_location is not None:
            self.log.debug("Tracking calling back to {} with Location".format(
                self.on_location.__name__))
            self.on_location(loc)
        else:
            self.log.warning("No on_location callback defined")

    # --------------------- LOW POWER OPERATIONS ----------------------------------------------- #
    # TODO: manage GNSS settings on entry/exit to LPM, collect garbage, etc.
    def get_wakeup_interval(self, init=False):
        if not init:
            self.log.warning(
                "S51 value twin may be out of date requiring follow-up query")
            self.submit_at_command(at_command='ATS51?',
                                   callback=self._cb_get_wakeup_interval)
        return self.s_registers.register('S51').get()

    def _cb_get_wakeup_interval(self, valid_response, responses, request):
        if valid_response:
            value = int(responses[0])
            self.log.info("Updating S51 register value: {}".format(value))
            self.s_registers.register('S51').set(value)

    def set_wakeup_interval(self, wakeup_interval=WAKEUP_5_SEC):
        if wakeup_interval in WAKEUP_INTERVALS:
            self.submit_at_command(at_command='ATS51={}'.format(
                wakeup_interval), callback=self._cb_set_wakeup_interval)
            # TODO: some risk that write fails and twin is no longer synchronized
            self.s_registers.register('S51').set(wakeup_interval)
        else:
            self.log.error(
                "Invalid wakeup interval {}".format(wakeup_interval))

    def _cb_set_wakeup_interval(self, valid_response, responses, request):
        if valid_response:
            value = int(request.replace('ATS51=', ''))
            self.log.info("Updating S51 register value: {}".format(value))
            self.s_registers.register('S51').set(value)

    def set_power_mode(self, power_mode=POWER_MODE_MOBILE_POWERED):
        if power_mode in POWER_MODES:
            self.submit_at_command(at_command='ATS50={}'.format(
                power_mode), callback=self._cb_set_power_mode)
            # TODO: some risk that write fails and twin is no longer synchronized
            self.s_registers.register('S50').set(power_mode)
        else:
            self.log.error("Invalid power mode {}".format(power_mode))

    def _cb_set_power_mode(self, valid_response, responses, request):
        if valid_response:
            value = int(request.replace('ATS50=', ''))
            self.log.info("Updating S50 register value: {}".format(value))
            self.s_registers.register('S50').set(value)

    def get_power_mode(self, init=False):
        if not init:
            self.log.warning(
                "S50 value twin may be out of date requiring follow-up query")
            self.submit_at_command(at_command='ATS50?',
                                   callback=self._cb_get_power_mode)
        return self.s_registers.register('S50').get()

    def _cb_get_power_mode(self, valid_response, responses, request):
        if valid_response:
            value = int(responses[0])
            self.log.info("Updating S50 register value: {}".format(value))
            self.s_registers.register('S50').set(value)

    # ---------------------- S-REGISTER OPERATIONS -------------------------------------------------- #
    def set_s_register(self, register, value, callback=None, save=False):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _cb_set_s_register(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def get_s_register(self, register, callback):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _cb_get_s_register(self, valid_response, responses, request):
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    def _update_twin_parameters(self, register, value):
        # TODO: check which IdpModem parameters are affected by the read/write of s-Registers and update twin
        self.log.warning("{} FUNCTION NOT IMPLEMENTED".format(
            sys._getframe().f_code.co_name))

    # --------------------- Generic functions that might be useful ----------------------------------- #
    def send_raw(self, command, callback):
        self._at_command_active_user_callback = callback
        self.submit_at_command(command, callback=self._cb_send_raw)

    def _cb_send_raw(self, valid_response, responses, request):
        if valid_response:
            self._at_command_active_user_callback(responses)
        else:
            self.log.error('Raw command {} response failed'.format(request))
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
        self.log.info("*** Modem AT Configuration ***")
        for (attr, value) in vars(self.at_config).items():
            self.log.info("*  {}={}".format(attr, 1 if value else 0))

    def log_sat_status(self):
        """Logs/displays the current satellite status on the console."""
        self.log.info("*** Satellite Status ***")
        for (attr, value) in vars(self.sat_status).items():
            self.log.info("*  {}={}".format(attr, value))

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


def _bytearray_to_str(arr):
    s = ''
    for b in bytearray(arr):
        if chr(b) in printable:
            s += chr(b)
        else:
            s += '{0:#04x}'.format(b).replace('0x', '\\')
    return s


def _bytearray_to_hex_str(arr):
    return binascii.hexlify(bytearray(arr)).decode()


def _bytearray_to_b64_str(arr):
    return binascii.b2a_base64(bytearray(arr)).strip().decode()


class Message(object):
    """
    Class intended for abstracting message attributes.

    :param payload: one of the following:

       * (bytearray) including SIN and MIN bytes as first 2 in the array if not explicitly set in the call
       * (list) of integer bytes (0..255) including SIN and MIN if not specified explicitly in the call
       * (string) ASCII-HEX which includes SIN and MIN if not specified explicitly in the call
       * (string) Text which requires both SIN and MIN explictly specified in the call

    :param name: (string) optional up to 8 characters. A message name will be generated if not supplied
    :param msg_sin: integer (0..255)
    :param msg_min: integer (0..255)
    :param priority: (1=high, 4=low, 0=mobile-terminated)
    :param data_format: (optional) 1=FORMAT_TEXT, 2=FORMAT_HEX, 3=FORMAT_B64
    :param log: (optional) logger object
    :param debug: (optional) sets logging level to DEBUG

    """

    MAX_NAME_LENGTH = 8
    MAX_HEX_SIZE = 100

    def __init__(self, payload, name=None, msg_sin=None, msg_min=None, priority=PRIORITY_LOW,
                 data_format=FORMAT_HEX, size=None, log=None, debug=False):
        if is_logger(log):
            self.log = log
        else:
            self.log = get_wrapping_logger(debug=debug)
        if name is not None:
            self.name = str(name)[0:self.MAX_NAME_LENGTH - 1]
        else:
            self.name = str(int(time.time()))[1:9]
            self.log.info("Message using name={}".format(self.name))
        if msg_min is not None:
            if msg_sin is None:
                raise ValueError("SIN must be specified if MIN is specified")
            elif isinstance(msg_min, int) and msg_min in range(0, 255+1):
                self.min = msg_min
                # assume that payload does not also include MIN
            else:
                self.log.warning(
                    "Invalid MIN value {} must be integer in range 0..255".format(msg_min))
        elif payload is not None:
            if isinstance(payload, bytearray):
                self.min = payload[0]
            else:
                raise ValueError(
                    "Payload must be bytearray type if MIN is not specified")
        else:
            raise ValueError("Payload cannot be None if MIN is not specified")
        if msg_sin is not None:
            if isinstance(msg_sin, int) and msg_sin in range(16, 256):
                self.sin = msg_sin
            else:
                raise ValueError(
                    "Invalid SIN value {}, must be integer in range 16..255".format(msg_sin))
        elif payload is not None:
            if isinstance(payload, bytearray):
                if payload[0] > 15:
                    self.sin = payload[0]
                    self.log.debug(
                        "Received bytearray with implied SIN={}".format(self.sin))
                    payload = payload[1:] if msg_min is None else payload[2:]
                else:
                    raise ValueError(
                        "Invalid payload, first byte (SIN) must be integer in range 16..255")
            else:
                raise ValueError(
                    "Payload must be bytearray type if SIN is not specified")
        else:
            raise ValueError("Payload cannot be None if SIN is not specified")
        self.raw_payload = bytearray(0)
        if self.sin is not None and payload is not None:
            if isinstance(payload, str):  #: TODO broken on Python2 (unicode not str)
                if data_format == FORMAT_TEXT:
                    if msg_sin is not None and msg_min is not None:
                        payload = bytearray(payload.encode())
                    else:
                        raise ValueError(
                            "Function call with text string payload must include SIN and MIN")
                elif data_format == FORMAT_HEX:
                    if _is_hex_string(payload):
                        payload = bytearray.fromhex(payload)
                    else:
                        raise ValueError(
                            "Hex format received with invalid characters")
                elif data_format == FORMAT_B64:
                    if msg_sin is not None and msg_min is not None:
                        payload = base64.b64decode(payload)
                    else:
                        raise ValueError(
                            "Function call with base64 string payload must include SIN and MIN")
                else:
                    raise ValueError(
                        "Unrecognized data_format: {}".format(data_format))
            elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255+1)) for i in payload):
                payload = bytearray(payload)
            elif not isinstance(payload, bytearray):
                raise ValueError("Invalid payload {} ({}),".format(payload, type(payload))
                                + " must be text or hex string,"
                                + " integer list or bytearray")
            self.raw_payload = bytearray(payload)
            if msg_min is not None:
                self.raw_payload = bytearray([self.min]) + self.raw_payload
            if self.sin is not None:
                self.raw_payload = bytearray([self.sin]) + self.raw_payload
        self.size = len(self.raw_payload)
        if size is not None and size != self.size:
            self.log.warning(
                "Size {} passed during init does not match derived size {}".format(size, self.size))
        self.priority = priority
        self.data_format = data_format
        # self.log.debug("New message created: {}".format(vars(self)))

    def data(self, data_format=FORMAT_HEX, include_min=True, include_sin=False):
        """
        Returns the data content of the message

        :param data_format: (int) 1=FORMAT_TEXT, 2=FORMAT_HEX (default), 3=FORMAT_B64
        :param include_min: (boolean) whether to include MIN byte in the data (used when not specifying MIN explicitly)
        :param include_sin: (boolean) whether to include SIN byte (not part of data for MO messages)
        :return: data as a string for submission using AT%MGRT
        """
        if len(self.raw_payload) > 0:
            if include_sin:
                if not include_min:
                    raise ValueError("Must include MIN when including SIN")
                else:
                    payload = self.raw_payload
            else:
                payload = self.raw_payload[1:] if include_min else self.raw_payload[2:]
            if data_format == FORMAT_TEXT:
                data = '"{}"'.format(_bytearray_to_str(payload))
            elif data_format == FORMAT_HEX:
                data = _bytearray_to_hex_str(payload)
            else:
                data = _bytearray_to_b64_str(payload)
            return data
        else:
            raise ValueError("No data to return")


class MobileOriginatedMessage(Message):
    """
    Subclass of Message containing Mobile Originated (aka Return) message properties.
    Mobile-Originated state (starting=None) is represented as an attribute:

       - ``UNAVAILABLE``: 0
       - ``TX_READY``: 4
       - ``TX_SENDING``: 5
       - ``TX_COMPLETE``: 6
       - ``TX_FAILED``: 7

    :param name: (string) user identifier for the message
    :param payload: follows the structure of the Message superclass
    :param data_format: follows the structure of the Message superclass
    :param msg_sin: Service Identification Number (1st byte of payload)
    :param msg_min: Message Identification Number (2nd byte of payload)
    :param kwargs: follows the structure of the Message superclass

    """

    def __init__(self, payload, name=None, data_format=FORMAT_HEX, msg_sin=None, msg_min=None, **kwargs):
        """

        :param name: (string) user identifier for the message
        :param payload: follows the structure of the Message superclass
        :param data_format: follows the structure of the Message superclass
        :param msg_sin: Service Identification Number (1st byte of payload)
        :param msg_min: Message Identification Number (2nd byte of payload)
        :param **kwargs: follows the structure of the Message superclass

        """
        ''' TODO: remove pre-filter
        if isinstance(payload, str):
            if _is_hex_string(payload):
                payload = bytearray.fromhex(payload)
            else:
                if msg_sin is not None and msg_min is not None:
                    if data_format is None or data_format != FORMAT_TEXT:
                        payload = bytearray(payload)
                else:
                    raise ValueError(
                        "Function call with text string payload must include SIN and MIN")
        elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255+1)) for i in payload):
            payload = bytearray(payload)
        elif not isinstance(payload, bytearray):
            raise ValueError(
                "Invalid payload type, must be text or hex string, integer list or bytearray")
        '''
        super(MobileOriginatedMessage, self).__init__(payload=payload, name=name, msg_sin=msg_sin, msg_min=msg_min,
                                                      data_format=data_format, **kwargs)
        self.state = None


class MobileTerminatedMessage(Message):
    """
    Subclass of Message containing Mobile-Terminated (MT aka Forward) message properties.
    Initializes MT message with state = ``RX_RETRIEVED``
    MT message state represented as an attribute:

       - ``UNAVAILABLE``: 0
       - ``COMPLETE``: 2
       -  ``RETRIEVED``: 3

    :param name: (string) name assigned by the modem
    :param payload: follows the structure of the Message superclass
    :param data_format: follows the structure of the Message superclass
    :param msg_num: (string) message number assigned by the modem (unused)
    :param priority: (int) always 0 for MT messages
    :param kwargs: follows the structure of the Message superclass

    """

    def __init__(self, payload, name, data_format, msg_num=None, priority=PRIORITY_MT, **kwargs):
        """

        :param name: (string) name assigned by the modem
        :param payload: follows the structure of the Message superclass
        :param data_format: follows the structure of the Message superclass
        :param msg_num: (string) message number assigned by the modem (unused)
        :param priority: (int) always 0 for MT messages
        :param kwargs: follows the structure of the Message superclass

        """
        if data_format not in (FORMAT_TEXT, FORMAT_HEX, FORMAT_B64):
            raise ValueError(
                "Unrecognized data format: {}".format(data_format))
        super(MobileTerminatedMessage, self).__init__(payload=payload, name=name, data_format=data_format,
                                                      priority=priority, **kwargs)
        self.state = RX_RETRIEVED
        self.number = msg_num


if __name__ == "__main__":
    SELFTEST_PORT = '/dev/ttyUSB0'
    modem = None
    try:
        modem = Modem(serial_name=SELFTEST_PORT, debug=True)
        while (not modem.is_initialized or 
                modem.sat_status.ctrl_state == 'Stopped'):
            time.sleep(1)
        # modem.tracking_setup(interval=30)
        textformat='Hello World'
        b64format='SGVsbG8gV29ybGQ='
        hexformat='48656c6c6f20576f726c64'
        test_msg = MobileOriginatedMessage(payload=b64format, 
                                            name='TEST', 
                                            data_format=FORMAT_B64, 
                                            msg_sin=255, msg_min=255)
        modem.mo_message_send(test_msg)
        while len(modem.mo_msg_queue) > 0:
            print('Awaiting {} messages in queue'.format(len(modem.mo_msg_queue)))
            time.sleep(5)
        # time.sleep(60)
        print('Test time completed')
    except Exception as e:
        print('*** EXCEPTION: {}'.format(e))
    finally:
        if modem is not None:
            modem.terminate()
