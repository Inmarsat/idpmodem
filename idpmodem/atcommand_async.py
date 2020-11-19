# -*- coding: utf-8 -*-
"""AT command protocol (asyncio) for Inmarsat IDP satellite messaging modems.

This module provides an async serial interface that sends and receives 
AT commands, decoding/abstracting typically used operations.
Based on the AioSerial package.

"""

import aioserial
import asyncio
import atexit
from base64 import b64decode, b64encode
from collections import OrderedDict
import logging
from time import time
from typing import Callable, Tuple, Union

from .crcxmodem import get_crc, validate_crc
from .utils import get_wrapping_logger
from . import constants
from . import nmea

LOGGING_VERBOSE_LEVEL = 9
logging.addLevelName(LOGGING_VERBOSE_LEVEL, 'VERBOSE')
def verbose(self, message, *args, **kwargs):
    if self.isEnabledFor(LOGGING_VERBOSE_LEVEL):
        self._log(LOGGING_VERBOSE_LEVEL, message, args, **kwargs)
logging.Logger.verbose = verbose
logging.VERBOSE = LOGGING_VERBOSE_LEVEL


def _printable(string: str) -> str:
    return string.replace('\r', '<cr>').replace('\n', '<lf>')


def _serial_asyncio_lost_bytes(response: str) -> bool:
    if ('AT' in response or '\r\r' in response):
        return True
    return False


class AtException(Exception):
    """Base class for AT command exceptions."""
    pass


class AtTimeout(AtException):
    """Indicates a timeout waiting for response."""
    pass


class AtCrcError(AtException):
    """Indicates a detected CRC mismatch on a response."""
    pass


class AtCrcConfigError(AtException):
    """Indicates a CRC response was received when none expected or vice versa.
    """
    pass


class IdpModemAsyncioClient():
    """A satellite IoT messaging modem on Inmarsat's IsatData Pro service.

    Attributes:
        crc: A boolean used if CRC-16 is enabled for long serial cables

    """

    def __init__(self,
                 port: str = '/dev/ttyUSB0',
                 baudrate: int = 9600,
                 loop: asyncio.AbstractEventLoop = None,
                 crc: bool = False,
                 debug: bool = False,
                 logger: logging.Logger = None,
                 log_level: int = logging.INFO):
        atexit.register(self._cleanup)
        self._log = logger or get_wrapping_logger(log_level=log_level)
        self.serialport = aioserial.AioSerial(
            port=port, baudrate=baudrate, loop=loop)
        if self.serialport:
            self.connected = True
        self.debug = debug
        self.pending_command = None
        self.pending_command_time = None
        self.crc = None
        self._retry_count = 0
        self._serial_async_error_count = 0
        self.initialize(crc)

    def _cleanup(self):
        """Runs at exit."""
        try:
            self._log.debug('Closing serial port {}'.format(
                self.serialport.port))
            self.serialport.close()
        except AttributeError:
            self._log.warning('No serial port to close')

    async def _send(self, data: str) -> str:
        """Coroutine encodes and sends an AT command.
        
        Args:
            writer: A serial_asyncio writer
            data: An AT command string
        
        Returns:
            A string with the original data.
        """
        if self.crc:
            data = get_crc(data)
        self.pending_command = data
        to_send = self.pending_command + '\r'
        self._log.verbose('Sending {}'.format(_printable(to_send)))
        self.pending_command_time = time()
        await self.serialport.write_async(to_send.encode())
        return data

    async def _recv(self, timeout: int = 5) -> list:
        """Coroutine receives and decodes data from the serial port.

        Parsing stops when 'OK' or 'ERROR' is found.
        
        Args:
            reader: A serial_asyncio reader

        Returns:
            A list of response strings with empty lines removed.
        
        Raises:
            AtTimeout if the response timed out.
        """
        response = []
        verbose_response = ''
        msg = ''
        try:
            while True:
                chars = (await asyncio.wait_for(
                    self.serialport.read_until_async(b'\r\n'),
                    timeout=timeout)).decode()
                msg += chars
                verbose_response += chars
                if msg.endswith('\r\n'):
                    self._log.verbose('Processing {}'.format(_printable(msg)))
                    msg = msg.strip()
                    if msg != self.pending_command:
                        if msg != '':
                            # empty lines are not included in response list
                            # but are preserved in verbose_response for CRC
                            response.append(msg)
                    else:
                        # remove echo for possible CRC calculation
                        echo = self.pending_command + '\r'
                        self._log.verbose('Removing echo {}'.format(
                            _printable(echo)))
                        verbose_response = verbose_response.replace(echo, '')
                    if msg in ['OK', 'ERROR']:
                        try:
                            response_crc = (await asyncio.wait_for(
                                self.serialport.read_until_async(b'\r\n'),
                                timeout=1)).decode()
                            if response_crc:
                                if not self.crc:
                                    self.crc = True
                                response_crc = response_crc.strip()
                                if _serial_asyncio_lost_bytes(verbose_response):
                                    self._serial_async_error_count += 1
                                if not validate_crc(response=verbose_response,
                                                    candidate=response_crc):
                                    err_msg = '{} CRC error for {}'.format(
                                        response_crc,
                                        _printable(verbose_response))
                                    self._log.error(err_msg)
                                    raise AtCrcError(err_msg)
                                else:
                                    self._log.verbose('CRC {} ok for {}'.format(
                                        response_crc,
                                        _printable(verbose_response)))
                        except asyncio.TimeoutError:
                            self.crc = False
                        break
                    msg = ''
        except asyncio.TimeoutError:
            timeout_time = time() - self.pending_command_time
            err = ('AT timeout {} after {} seconds ({}s after command)'.format(
                self.pending_command, timeout, timeout_time))
            raise AtTimeout(err)
        return response

    async def _get_response(self, at_command: str, timeout: int = 5):
        """Coroutine returns the command response.
        
        Args:
            at_command: The command string
            timeout: The response timeout in seconds
        
        Returns:
            A list of response strings including OK or ERROR.
        """
        try:
            self._log.verbose('Checking unsolicited data prior to {}'.format(
                at_command))
            self.pending_command_time = time()
            unsolicited = await self._recv(timeout=0.25)
            if unsolicited:
                self._log.warning('Unsolicited data: {}'.format(unsolicited))
        except AtTimeout:
            if self.debug:
                self._log.verbose('No unsolicited data found')
        tasks = [self._send(at_command),
            self._recv(timeout=timeout)]
        echo, response = await asyncio.gather(*tasks)
        if echo in response:
            response.remove(echo)
        return response

    def command(self, at_command: str, timeout: int = 5, retries: int = 1):
        """Submits an AT command and returns the response asynchronously.
        
        Args:
            at_command: The AT command string
            timeout: The maximum time in seconds to await a response.
        
        Returns:
            A list of response strings, or ['ERROR', '<error_code>']
        """
        try:
            response = asyncio.run(self._get_response(at_command, timeout=timeout))
            if response is not None:
                self._retry_count = 0
                if response[0] == 'ERROR':
                    # error_code = self._loop.run_until_complete(
                    #     self._get_response('ATS80?'))
                    error_code = self.command('ATS80?')
                    if error_code is not None:
                        response.append(error_code[0])
                    else:
                        self._log.error('Failed to get error_code from S80')
                return response
            raise AtException('No response received for {}'.format(at_command))
        except AtCrcError:
            self._retry_count += 1
            if self._retry_count < retries:
                self._log.error('CRC error retrying')
                return self.command(at_command, timeout=timeout, retries=retries)
            else:
                self._retry_count = 0
                raise AtException('Too many failed CRC')
    
    def initialize(self, crc: bool) -> bool:
        """Initializes the modem using ATZ and sets up CRC.

        Args:
            crc: desired initial CRC enabled if True

        Returns:
            True if successful
        
        Raises:
            AtException
        """
        self._log.debug('Initializing modem')
        initialize_cmd = 'ATZ;E1;V1'
        initialize_cmd += ';%CRC=1' if crc else ''
        try:
            success = self.command(initialize_cmd)
            if success[0] == 'ERROR':
                if success[1] == '100':
                    if crc and self.crc:
                        self._log.debug('CRC already enabled')
                        return True
                    else:
                        self.crc = True
                        self.initialize(crc)
                else:
                    raise AtException(constants.AT_ERROR_CODES[success[1]]) 
        except Exception as e:
            self._log.error('Error initializing: {}'.format(e))
            raise e
    
    def config_restore_nvm(self) -> bool:
        """Sends the ATZ command to restore from non-volatile memory.
        
        Returns:
            Boolean success.
        """
        try:
            response = self.command('ATZ')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def config_restore_factory(self) -> bool:
        """Sends the AT&F command and returns True on success."""
        try:
            response = self.command('AT&F')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False
    
    def config_report(self) -> Tuple[dict, dict]:
        """Sends the AT&V command to retrive S-register settings.
        
        Returns:
            A tuple with two dictionaries or both None if failed
            at_config with booleans crc, echo, quiet and verbose
            reg_config with S-register tags and integer values
        """
        try:
            response = self.command('AT&V')
            if response[0] == 'ERROR':
                return (None, None)
            at_config = response[1]
            s_regs = response[2]
            echo, quiet, verbose, crc = at_config.split(' ')
            at_config = {
                "crc": bool(int(crc[4])),
                "echo": bool(int(echo[1])),
                "quiet": bool(int(quiet[1])),
                "verbose": bool(int(verbose[1])),
            }
            reg_config = {}
            for reg in s_regs.split(' '):
                name, value = reg.split(':')
                reg_config[name] = int(value)
            return (at_config, reg_config)
        except AtException:
            return (None, None)

    def config_save(self) -> bool:
        """Sends the AT&W command and returns True if successful."""
        try:
            response = self.command('AT&W')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def config_crc_enable(self, crc: bool) -> bool:
        """Enables or disables CRC error checking (for long serial cable).
        
        Args:
            crc: enable CRC if true
        """
        self._log.debug('{} CRC'.format('Enabling' if crc else 'Disabling'))
        try:
            response = self.command('AT%CRC={}'.format(1 if crc else 0))
            if response[0] == 'ERROR' and self.crc != crc:
                raise AtException('Failed to {} crc'.format(
                    'enable' if crc else 'disable'))
            self.crc = crc
            return True
        except AtException:
            return False
    
    def device_mobile_id(self) -> str:
        """Returns the unique Mobile ID (Inmarsat serial number).
        
        Returns:
            MobileID string or None if error.
        """
        try:
            response = self.command("AT+GSN")
            if response[0] == 'ERROR':
                return None 
            return response[0].replace('+GSN:', '').strip()
        except AtException:
            return None

    def device_version(self) -> Tuple[str, str, str]:
        """Returns the hardware, firmware and AT versions.
        
        Returns:
            Dict with hardware, firmware, at version or all None if error.
        """
        try:
            response = self.command("AT+GMR")
            if response[0] == 'ERROR':
                return None
            versions = response[0].replace('+GMR:', '').strip()
            fw_ver, hw_ver, at_ver = versions.split(',')
            return {'hardware': hw_ver, 'firmware': fw_ver, 'at': at_ver}
        except AtException:
            return None

    def gnss_continuous_set(self, interval: int=0, doppler: bool=True) -> bool:
        """Sets the GNSS continous mode (0 = on-demand).
        
        Args:
            interval: Seconds between GNSS refresh.
            doppler: Often required for moving assets.
        
        Returns:
            True if successful setting.
        """
        try:
            if interval < 0 or interval > 30:
                raise ValueError('GNSS continuous interval must be in range 0..30')
            response = self.command('AT%TRK={}{}'.format(
                interval, ',{}'.format(1 if doppler else 0)))
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def gnss_nmea_get(self, stale_secs: int = 1, wait_secs: int = 35,
                      nmea: list = ['RMC', 'GSA', 'GGA', 'GSV']
                      ) -> Union[list, str]:
        """Returns a list of NMEA-formatted sentences from GNSS.

        Args:
            stale_secs: Maximum age of fix in seconds (1..600)
            wait_secs: Maximum time to wait for fix (1..600)

        Returns:
            List of NMEA sentences or 'ERR_TIMEOUT_OCCURRED'

        Raises:
            ValueError if parameter out of range

        """
        NMEA_SUPPORTED = ['RMC', 'GGA', 'GSA', 'GSV']
        BUFFER_SECONDS = 5
        if (stale_secs not in range(1, 600+1) or
            wait_secs not in range(1, 600+1)):
            raise ValueError('stale_secs and wait_secs must be 1..600')
        sentences = ''
        for sentence in nmea:
            sentence = sentence.upper()
            if sentence in NMEA_SUPPORTED:
                if len(sentences) > 0:
                    sentences += ','
                sentences += '"{}"'.format(sentence)
            else:
                raise ValueError('Unsupported NMEA sentence: {}'
                                 .format(sentence))
        try:
            response = self.command('AT%GPS={},{},{}'
                                    .format(stale_secs, wait_secs, sentences),
                                    timeout=wait_secs + BUFFER_SECONDS)
            if response[0] == 'ERROR':
                return response[1]
            response.remove('OK')
            response[0] = response[0].replace('%GPS: ', '')
            return response
        except AtException:
            return None

    def location_get(self, stale_secs: int = 1, wait_secs: int = 35):
        """Returns a location object
        
        Args:
            stale_secs: the maximum fix age to accept
            wait_secs: the maximum time to wait for a new fix
        
        Returns:
            nmea.Location object
        """
        nmea_sentences = self.gnss_nmea_get(stale_secs, wait_secs)
        if nmea_sentences is None or isinstance(nmea_sentences, str):
            return None
        location = nmea.location_get(nmea_sentences)
        return location

    def lowpower_notifications_enable(self) -> bool:
        """Sets up monitoring of satellite status and notification assertion.

        The following events trigger assertion of the notification output:
        - New Forward Message received
        - Return Message completed (success or failure)
        - Trace event update (satellite status change)

        Returns:
            True if successful
        """
        cmd = 'AT%EVMON=3.1;S88=1030'
        try:
            response = self.command(cmd)
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def lowpower_notification_check(self) -> list:
        """Returns a list of relevant events or None."""
        reason = self.notification_check()
        relevant = []
        if reason is None:
            return None
        if reason['event_cached'] == True:
            relevant.append('event_cached')
        if reason['message_mt_received'] == True:
            relevant.append('message_mt_received')
        if reason['message_mo_complete'] == True:
            relevant.append('message_mo_complete')
        return relevant if len(relevant) > 0 else None

    def message_mo_send(self,
                        data: str,
                        data_format: int,
                        sin: int,
                        min: int = None,
                        name: str = None,
                        priority: int = 4,
                        ) -> str:
        """Submits a mobile-originated message to send.
        
        Args:
            data: 
            data_format: 
            name: 
            priority: 
            sin: 
            min: 

        Returns:
            Name of the message if successful, or the error string
        """
        try:
            if name is None:
                # Use the 8 least-signficant numbers of unix timestamp as unique
                name = str(int(time()))[-8:]
            elif len(name) > 8:
                name = name[0:8]   # risk duplicates create an ERROR resposne
            response = self.command('AT%MGRT="{}",{},{}{},{},{}'.format(
                                    name,
                                    priority,
                                    sin,
                                    '.{}'.format(min) if min is not None else '',
                                    data_format,
                                    '"{}"'.format(data) if data_format == 1 else data))
            if response[0] == 'ERROR':
                raise AtException(constants.AT_ERROR_CODES[response[1]])
            return name
        except AtException as e:
            raise e

    def message_mo_state(self, name: str = None) -> list:
        """Returns the message state(s) requested.
        
        If no name filter is passed in, all available messages states
        are returned.  Returns False is the request failed.

        Args:
            name: The unique message name in the modem queue

        Returns:
            State: UNAVAILABLE, TX_READY, TX_SENDING, TX_COMPLETE, TX_FAILED or None

        """
        STATES = {
            0: 'UNAVAILABLE',
            4: 'TX_READY',
            5: 'TX_SENDING',
            6: 'TX_COMPLETE',
            7: 'TX_FAILED'
        }
        filter = '="{}"'.format(name) if name is not None else ''
        try:
            response = self.command("AT%MGRS{}".format(filter))
            if response[0] == 'ERROR':
                return None
            # %MGRS: "<name>",<msg_no>,<priority>,<sin>,<state>,<size>,<sent_bytes>
            response.remove('OK')
            states = []
            for res in response:
                res = res.replace('%MGRS:', '').strip()
                if len(res) > 0:
                    name, number, priority, sin, state, size, sent = res.split(',')
                    del number
                    del priority
                    del sin
                    states.append({'name': name,
                                'state': STATES[int(state)],
                                'size': size,
                                'sent': sent})
            return states
        except AtException:
            return None
    
    def message_mo_cancel(self, name: str) -> bool:
        """Cancels a mobile-originated message in the Tx ready state."""
        try:
            response = self.command('AT%MGRC={}'.format(name))
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def message_mo_clear(self) -> int:
        """Clears the modem transmit queue.
        
        TODO: change to generic AT%MRGSC
        Returns:
            Count of messages deleted, or -1 in case of error
        """
        try:
            response = self.command('AT%MGRSC')
            if response[0] == 'ERROR':
                return -1
            response.remove('OK')
            if '%MGRS:' in response:
                response.remove('%MGRS:')
            message_count = len(response)
            return message_count
        except AtException:
            return -1

    def message_mt_waiting(self) -> Union[list, None]:
        """Returns a list of received mobile-terminated message information.
        
        Returns:
            List of (name, number, priority, sin, state, length, received)

        """
        try:
            response = self.command('AT%MGFN')
            if response[0] == 'ERROR':
                return None
            response.remove('OK')
            waiting = []
            #: %MGFN: name, number, priority, sin, state, length, bytes_received
            for res in response:
                msg = res.replace('%MGFN:', '').strip()
                if msg.startswith('"FM'):
                    parts = msg.split(',')
                    name, number, priority, sin, state, length, received = parts
                    del number   #: unused
                    waiting.append({'name': name,
                                    'sin': int(sin),
                                    'priority': int(priority),
                                    'state': int(state),
                                    'length': int(length),
                                    'received': int(received)})
            return waiting
        except AtException:
            return None

    def message_mt_get(self, name: str, data_format: int = 3,
                       verbose: bool = False) -> Union[str, dict]:
        """Returns the payload of a specified mobile-terminated message.
        
        Payload is presented as a string with encoding based on data_format. 

        Args:
            name: The unique name in the modem queue e.g. FM01.01
            data_format: text=1, hex=2, base64=3 (default)

        Returns:
            The encoded data as a string

        """
        try:
            response = self.command('AT%MGFG={},{}'.format(name, data_format))
            if response is None or response[0] == 'ERROR':
                return None
            # response.remove('OK')
            #: name, number, priority, sin, state, length, data_format, data
            parts = response[0].split(',')
            sys_msg_num, sys_msg_seq = parts[1].split('.')
            msg_sin = int(parts[3])
            data_str_no_sin = parts[7]
            if data_format == constants.FORMAT_HEX:
                data = hex(msg_sin) + data_str_no_sin.lower()
            elif data_format == constants.FORMAT_B64:
                # add SIN as base64
                databytes = bytes([msg_sin]) + b64decode(data_str_no_sin)
                data = b64encode(databytes).decode('ascii')
            elif data_format == constants.FORMAT_TEXT:
                data = '\\{:02x}'.format(msg_sin) + data_str_no_sin
            message = {
                'name': parts[0],
                'system_message_number': int(sys_msg_num),
                'system_message_sequence': int(sys_msg_seq),
                'priority': int(parts[2]),
                'sin': msg_sin,
                'state': int(parts[4]),
                'length': int(parts[5]),
                'data_format': data_format,
                'data': data
            }
            return message if verbose else message['data']
        except AtException:
            return None

    def message_mt_delete(self, name: str) -> bool:
        """Marks a Return message for deletion by the modem.
        
        Args:
            name: The unique mobile-terminated name in the queue

        Returns:
            True if the operation succeeded

        """
        try:
            response = self.command('AT%MGFM="{}"'.format(name))
            if response is None or response[0] == 'ERROR':
                return False
            return True
        except:
            return False

    def event_monitor_get(self) -> Union[list, None]:
        """Returns a list of monitored/cached events.
        As a list of <class.subclass> strings which includes an asterisk
        for each new event that can be retrieved.

        Returns:
            list of strings <class.subclass[*]> or None
        """
        try:
            result = self.command('AT%EVMON')
            if result is None or result[0] == 'ERROR':
                return None
            events = result[0].replace('%EVMON: ', '').split(',')
            '''
            for i in range(len(events)):
                c, s = events[i].strip().split('.')
                if s[-1] == '*':
                    s = s.replace('*', '')
                    # TODO flag change for retrieval
                events[i] = (int(c), int(s))
            '''
            return events
        except AtException:
            return None

    def event_monitor_set(self, eventlist: list) -> bool:
        """Sets trace events to monitor.

        Args:
            eventlist: list of tuples (class, subclass)

        Returns:
            True if successfully set
        """
        #: AT%EVMON{ = <c1.s1>[, <c2.s2> ..]}
        cmd = ''
        try:
            for monitor in eventlist:
                if isinstance(monitor, tuple):
                    if len(cmd) > 0:
                        cmd += ','
                    cmd += '{}.{}'.format(monitor[0], monitor[1])
            result = self.command('AT%EVMON={}'.format(cmd))
            if result is None or result[0] == 'ERROR':
                return False
            return True
        except:
            return False

    # TODO: move this out of class
    @staticmethod
    def _to_signed32(n):
        """Converts an integer to signed 32-bit format."""
        n = n & 0xffffffff
        return (n ^ 0x80000000) - 0x80000000

    def event_get(
        self, event: tuple, raw: bool = True
    ) -> Union[str, dict, None]:
        """Gets the cached event by class/subclass.

        Args:
            event: tuple of (class, subclass)
            raw: Returns the raw text string if True
        
        Returns:
            String if raw=True, dictionary if raw=False or None
        """
        #: AT%EVNT=c,s
        #: res %EVNT: <dataCount>,<signedBitmask>,<MTID>,<timestamp>,
        # <class>,<subclass>,<priority>,<data0>,<data1>,..,<dataN>
        if not (isinstance(event, tuple) and len(event) == 2):
            raise AtException('event_get expects (class, subclass)')
        try:
            result = self.command('AT%EVNT={},{}'.format(event[0], event[1]))
            if result is None or result[0] == 'ERROR':
                return None
            eventdata = result[0].replace('%EVNT: ', '').split(',')
            event = {
                'data_count': int(eventdata[0]),
                'signed_bitmask': bin(int(eventdata[1]))[2:],
                'mobile_id': eventdata[2],
                'timestamp': eventdata[3],
                'class': eventdata[4],
                'subclass': eventdata[5],
                'priority': eventdata[6],
                'data': eventdata[7:]
            }
            bitmask = event['signed_bitmask']
            while len(bitmask) < event['data_count']:
                bitmask = '0' + bitmask
            i = 0
            for bit in reversed(bitmask):
                #: 32-bit signed conversion redundant since response is string
                if bit == '1':
                    event['data'][i] = self._to_signed32(int(event['data'][i]))
                else:
                    event['data'][i] = int(event['data'][i])
                i += 1
            # TODO lookup class/subclass definitions
            return result[0] if raw else event
        except AtException:
            return None

    # TODO: move this out of class
    @staticmethod
    def _notifications_dict(sreg_value: int = None) -> OrderedDict:
        """Returns an OrderedDictionary as an abstracted bitmask of notifications.
        
        Args:
            sreg_value: (optional) the integer value stored in S88 or S89
        
        Returns:
            ordered dictionary corresponding to bitmask
        """
        template = OrderedDict([
            (bit, False) for bit in constants.NOTIFICATION_BITMASK])
        if sreg_value is not None:
            bitmask = bin(int(sreg_value))[2:]
            if len(bitmask) > len(template):
                bitmask = bitmask[:len(template) - 1]
            while len(bitmask) < len(template):
                bitmask = '0' + bitmask
            i = 0
            for key in reversed(template):
                template[key] = True if bitmask[i] == '1' else False
                i += 1
        return template

    def notification_control_set(self, event_map: list) -> bool:
        """Sets the event notification bitmask.

        Args:
            event_map: list of dict{event_name, bool}
        
        Returns:
            True if successful.
        """
        #: ATS88=bitmask
        # TODO REMOVE old_notifications = self.notifications.copy()
        notifications_changed = False
        old_notifications = self.notification_control_get()
        if old_notifications is None:
            return False
        for event in event_map:
            if event in old_notifications:
                binary = '0b'
                for key in reversed(old_notifications):
                    bit = '1' if old_notifications[key] else '0'
                    if key == event:
                        notify = event_map[event]
                        if old_notifications[key] != notify:
                            bit = '1' if notify else '0'
                            notifications_changed = True
                            # self.notifications[key] = notify
                    binary += bit
        if notifications_changed:
            bitmask = int(binary, 2)
            try:
                result = self.command('ATS88={}'.format(bitmask))
                if result is None or result[0] == 'ERROR':
                    return False
            except AtException:
                return False
        return True
    
    def notification_control_get(self) -> Union[OrderedDict, None]:
        """Returns the current notification configuration bitmask."""
        #: ATS88?
        try:
            result = self.command('ATS88?')
            if result is None or result[0] == 'ERROR':
                return None
            return self._notifications_dict(int(result[0]))
        except AtException:
            return None

    def notification_check(self) -> OrderedDict:
        """Returns the current active event notification bitmask.
        Clears the value of S89 upon reading.
        """
        #: ATS89?
        try:
            result = self.command('ATS89?')
            if result is None or result[0] == 'ERROR':
                return None
            template = self._notifications_dict(int(result[0]))
            return template
        except AtException:
            return None

    def sat_status_snr(self) -> Tuple[str, float]:
        """Returns the control state and C/No.
        
        Returns:
            Tuple with (state: int, C/No: float) or None if error.
        """
        try:
            response = self.command("ATS90=3 S91=1 S92=1 S122? S116?")
            if response is None or response[0] == 'ERROR':
                return (None, None)
            response.remove('OK')
            ctrl_state, cn_0 = response
            ctrl_state = int(ctrl_state)
            cn_0 = int(cn_0) / 100.0
            return (ctrl_state, cn_0)
        except AtException:
            return (None, None)

    @staticmethod
    def sat_status_name(self, ctrl_state: int) -> str:
        """Returns human-readable definition of a control state value.
        
        Raises:
            ValueError if ctrl_state is not found.
        """
        for s in constants.CONTROL_STATES:
            if int(s) == ctrl_state:
                return constants.CONTROL_STATES[s]
        raise ValueError('Control state {} not found'.format(ctrl_state))

    def shutdown(self) -> bool:
        """Tell the modem to prepare for power-down."""
        try:
            response = self.command('AT%OFF')
            if response is None or response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    def utc_time(self) -> Union[str, None]:
        """Returns current UTC time of the modem in ISO format."""
        try:
            response = self.command('AT%UTC')
            if response is None or response[0] == 'ERROR':
                return None
            return response[0].replace('%UTC: ', '').replace(' ', 'T') + 'Z'
        except AtException:
            return None

    def s_register_get(self, register: str) -> Union[int, None]:
        """Returns the value of the S-register requested.

        Args:
            register: The register name/number (e.g. S80)

        Returns:
            integer value or None
        """
        if not register.startswith('S'):
            # TODO: better Exception handling
            raise Exception('Invalid S-register {}'.format(register))
        try:
            response = self.command('AT{}?'.format(register))
            if response is None or response[0] == 'ERROR':
                return None
            return int(response[0])
        except AtException:
            return None

    def sreg_get_all(self) -> Union[list, None]:
        """Returns a list of S-register definitions.
        R=read-only, S=signed, V=volatile
        
        Returns:
            tuple(register, RSV, current, default, minimum, maximum) or None
        """
        #: AT%SREG
        #: Sreg, RSV, CurrentVal, DefaultVal, MinimumVal, MaximumVal
        try:
            result = self.command('AT%SREG')
            if result is None or result[0] == 'ERROR':
                return None
            result.remove('OK')
            reg_defs = result[2:]
            registers = []
            for row in reg_defs:
                reg_def = row.split(' ')
                reg_def = tuple(filter(None, reg_def))
                registers.append(reg_def)
            return registers
        except AtException:
            return None
    
    def _on_connection_lost(self):
        pass

'''    
if __name__ == '__main__':
    try:
        modem = IdpModemAsyncioClient(log_level=logging.VERBOSE)
        if not modem.lowpower_notifications_enable():
            print('Could not enable low power notifications')
        else:
            print('{}'.format(modem.lowpower_notification_check()))
        at_command1 = 'AT%GPS=10,45,"RMC","GGA","GSV","GSA"'
        sentences = modem.command(at_command1, timeout=45)
        for sentence in sentences:
            print(sentence)
        at_command2 = 'ATBAD'
        print(modem.command(at_command2))
    except aioserial.SerialException as e:
        print('SerialException {}'.format(e))
    except AtTimeout:
        print('Serial port unresponsive...')
'''