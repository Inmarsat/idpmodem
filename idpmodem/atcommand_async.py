# -*- coding: utf-8 -*-
"""AT command protocol (asyncio) for Inmarsat IDP satellite messaging modems.

This module provides an async serial interface that sends and receives 
AT commands, decoding/abstracting typically used operations.
Based on the AioSerial package.

"""

from aioserial import AioSerial
from asyncio import AbstractEventLoop, gather, TimeoutError, wait_for
from base64 import b64decode, b64encode
from collections import OrderedDict
import logging
from time import time
from typing import Callable, Tuple, Union

from .aterror import AtCrcConfigError, AtCrcError, AtException, AtTimeout, AtUnsolicited
from .constants import AT_ERROR_CODES, BEAMSEARCH_STATES, CONTROL_STATES, FORMAT_B64, FORMAT_HEX, FORMAT_TEXT, MESSAGE_STATES, NOTIFICATION_BITMASK
from .crcxmodem import get_crc, validate_crc
# from .message import MobileOriginatedMessage, MobileTerminatedMessage
from .nmea import location_get, Location, NmeaException
from .utils import get_wrapping_logger

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


def _to_signed32(n):
    """Converts an integer to signed 32-bit format."""
    n = n & 0xffffffff
    return (n ^ 0x80000000) - 0x80000000


def _notifications_dict(sreg_value: int = None) -> OrderedDict:
    """Returns an OrderedDictionary as an abstracted bitmask of notifications.
    
    Args:
        sreg_value: (optional) the integer value stored in S88 or S89
    
    Returns:
        ordered dictionary corresponding to bitmask
    """
    template = OrderedDict([
        (bit, False) for bit in NOTIFICATION_BITMASK])
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


class IdpModemBusy(Exception):
    pass


class GnssTimeout(Exception):
    pass


class IdpModemAsyncioClient:
    """A satellite IoT messaging modem on Inmarsat's IsatData Pro service.

    Attributes:
        port: The serial port name e.g. `/dev/ttyUSB0`
        baudrate: The baudrate of the serial port
        crc: A boolean used if CRC-16 is enabled for long serial cables
        loop: The asyncio event loop (uses default if not provided)

    """

    def __init__(self,
                 port: str = '/dev/ttyUSB0',
                 baudrate: int = 9600,
                 crc: bool = False,
                 loop: AbstractEventLoop = None,
                 logger: logging.Logger = None,
                 log_level: int = logging.INFO):
        """Initializes the class.
        
        Args:
            port: The serial port name e.g. `/dev/ttyUSB0`
            baudrate: The serial port baudrate
            crc: enables CRC-16 for long serial cables
            loop: (optional) external asyncio event loop to use
            logger: (optional) external logger to use
            log_level: Level for the logger to record

        """
        self._log = logger or get_wrapping_logger(log_level=log_level)
        self.port = port
        self.baudrate = baudrate
        self.loop = loop
        self.serialport = None
        self.pending_command = None
        self.pending_command_time = None
        self.crc = None
        self._retry_count = 0
        self._serial_async_error_count = 0

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
                chars = (await wait_for(
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
                            response_crc = (await wait_for(
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
                        except TimeoutError:
                            self.crc = False
                        break
                    msg = ''
        except TimeoutError:
            timeout_time = time() - self.pending_command_time
            err = ('AT timeout {} after {} seconds ({}s after command)'.format(
                self.pending_command, timeout, timeout_time))
            raise AtTimeout(err)
        return response

    async def command(self, at_command: str, timeout: int = 5, retries: int = 0):
        """Submits an AT command and returns the response asynchronously.
        
        Args:
            at_command: The AT command string
            timeout: The maximum time in seconds to await a response.
            retries: Optional number of additional attempts on failure.
        
        Returns:
            A list of response strings, or ['ERROR', '<error_code>']
        """
        try:
            try:
                self._log.verbose('Opening serial port {}'.format(self.port))
                self.serialport = AioSerial(port=self.port,
                                            baudrate=self.baudrate,
                                            loop=self.loop)
            except Exception as e:
                self._log.error('Error connecting to aioserial: {}'.format(e))
            try:
                self._log.verbose('Checking unsolicited data prior to {}'.format(
                    at_command))
                self.pending_command_time = time()
                unsolicited = await self._recv(timeout=0.25)
                if unsolicited:
                    self._log.warning('Unsolicited data: {}'.format(unsolicited))
            except AtTimeout:
                self._log.verbose('No unsolicited data found')
            tasks = [self._send(at_command),
                self._recv(timeout=timeout)]
            echo, response = await gather(*tasks)
            if echo in response:
                response.remove(echo)
            if response is not None:
                self._retry_count = 0
                if response[0] == 'ERROR':
                    error_code = await self.command('ATS80?')
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
                return await self.command(
                    at_command, timeout=timeout, retries=retries)
            else:
                self._retry_count = 0
                raise AtException('Too many failed CRC')
        finally:
            if self.serialport:
                self._log.verbose('Closing serial port {}'.format(self.port))
                self.serialport.close()
                self.serialport = None
    
    async def initialize(self, crc: bool = False) -> bool:
        """Initializes the modem using ATZ and sets up CRC.

        Args:
            crc: desired initial CRC enabled if True

        Returns:
            True if successful
        
        Raises:
            AtException
        """
        self._log.debug('Initializing modem{}'.format(' (CRC)' if crc else ''))
        initialize_cmd = 'ATZ;E1;V1'
        initialize_cmd += ';%CRC=1' if crc else ''
        try:
            success = await self.command(initialize_cmd)
            if success[0] == 'ERROR':
                if success[1] == '100':
                    if crc and self.crc:
                        self._log.debug('CRC already enabled')
                        return True
                    else:
                        self.crc = True
                        await self.initialize(crc)
                else:
                    raise AtException(AT_ERROR_CODES[success[1]])
            return True
        except Exception as e:
            self._log.error('Error initializing: {}'.format(e))
            raise e
    
    async def config_restore_nvm(self) -> bool:
        """Sends the ATZ command to restore from non-volatile memory.
        
        Returns:
            Boolean success.
        """
        try:
            response = await self.command('ATZ')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    async def config_restore_factory(self) -> bool:
        """Sends the AT&F command and returns True on success."""
        self._log.debug('Restoring factory defaults')
        try:
            response = await self.command('AT&F')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False
    
    async def config_report(self) -> Tuple[dict, dict]:
        """Sends the AT&V command to retrive S-register settings.
        
        Returns:
            A tuple with two dictionaries or both None if failed
            at_config with booleans crc, echo, quiet and verbose
            reg_config with S-register tags and integer values
        """
        self._log.debug('Querying configuration')
        try:
            response = await self.command('AT&V')
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

    async def config_save(self) -> bool:
        """Sends the AT&W command and returns True if successful."""
        self._log.debug('Saving S-registers to non-volatile memory')
        try:
            response = await self.command('AT&W')
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    async def config_crc_enable(self, crc: bool) -> bool:
        """Enables or disables CRC error checking (for long serial cable).
        
        Args:
            crc: enable CRC if true
        """
        self._log.debug('{} CRC'.format('Enabling' if crc else 'Disabling'))
        try:
            response = await self.command('AT%CRC={}'.format(1 if crc else 0))
            if response[0] == 'ERROR' and self.crc != crc:
                raise AtException('Failed to {} crc'.format(
                    'enable' if crc else 'disable'))
            self.crc = crc
            return True
        except AtException:
            return False
    
    async def device_mobile_id(self) -> str:
        """Returns the unique Mobile ID (Inmarsat serial number).
        
        Returns:
            MobileID string or None if error.
        """
        self._log.debug('Querying device Mobile ID')
        try:
            response = await self.command("AT+GSN")
            if response[0] == 'ERROR':
                return None 
            return response[0].replace('+GSN:', '').strip()
        except AtException:
            return None

    async def device_version(self) -> Tuple[str, str, str]:
        """Returns the hardware, firmware and AT versions.
        
        Returns:
            Dict with hardware, firmware, at version or all None if error.
        """
        self._log.debug('Querying device version info')
        try:
            response = await self.command("AT+GMR")
            if response[0] == 'ERROR':
                return None
            versions = response[0].replace('+GMR:', '').strip()
            fw_ver, hw_ver, at_ver = versions.split(',')
            return {'hardware': hw_ver, 'firmware': fw_ver, 'at': at_ver}
        except AtException:
            return None

    async def gnss_continuous_set(self,
                                  interval: int=0,
                                  doppler: bool=True) -> bool:
        """Sets the GNSS continous mode (0 = on-demand).
        
        Args:
            interval: Seconds between GNSS refresh.
            doppler: Often required for moving assets.
        
        Returns:
            True if successful setting.
        """
        self._log.debug('Setting GNSS refresh to {} seconds'.format(interval))
        try:
            if interval < 0 or interval > 30:
                raise ValueError('GNSS continuous interval must be in range 0..30')
            response = await self.command('AT%TRK={}{}'.format(
                interval, ',{}'.format(1 if doppler else 0)))
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    async def gnss_nmea_get(self,
                            stale_secs: int = 1,
                            wait_secs: int = 35,
                            sentences: list = ['RMC', 'GSA', 'GGA', 'GSV']
                            ) -> Union[list, str]:
        """Returns a list of NMEA-formatted sentences from GNSS.

        Args:
            stale_secs: Maximum age of fix in seconds (1..600)
            wait_secs: Maximum time to wait for fix (1..600)
            sentences: Optional list of NMEA sentence types to get

        Returns:
            List of NMEA sentences or 'ERR_TIMEOUT_OCCURRED'

        Raises:
            ValueError if parameter out of range
            GnssTimeout if no response from GNSS
            AtException logged and re-raised

        """
        self._log.debug('Requesting GNSS fix information')
        NMEA_SUPPORTED = ['RMC', 'GGA', 'GSA', 'GSV']
        BUFFER_SECONDS = 5
        if (stale_secs not in range(1, 600+1) or
            wait_secs not in range(1, 600+1)):
            raise ValueError('stale_secs and wait_secs must be 1..600')
        sentence_list = ''
        for sentence in sentences:
            sentence = sentence.upper()
            if sentence in NMEA_SUPPORTED:
                if len(sentence_list) > 0:
                    sentence_list += ','
                sentence_list += '"{}"'.format(sentence)
            else:
                raise ValueError('Unsupported NMEA sentence: {}'
                                 .format(sentence))
        try:
            response = await self.command(
                'AT%GPS={},{},{}'.format(stale_secs, wait_secs, sentence_list),
                timeout=wait_secs + BUFFER_SECONDS)
            if response[0] == 'ERROR':
                if response[1] == '108':
                    raise GnssTimeout(AT_ERROR_CODES[response[1]])
                else:
                    raise AtException(AT_ERROR_CODES[response[1]])
            response.remove('OK')
            response[0] = response[0].replace('%GPS: ', '')
            return response
        except AtException as e:
            self._log.error(e)
            raise e

    async def location(self,
                       stale_secs: int = 1,
                       wait_secs: int = 35) -> Location:
        """Returns a location object
        
        Args:
            stale_secs: the maximum fix age to accept
            wait_secs: the maximum time to wait for a new fix
        
        Returns:
            nmea.Location object
        
        Raises:
            Exception(s) logged and re-raised

        """
        self._log.debug('Querying location')
        try:
            nmea_sentences = await self.gnss_nmea_get(stale_secs, wait_secs)
            return location_get(nmea_sentences)
        except Exception as e:
            self._log.error(e)
            raise e

    async def lowpower_notifications_enable(self) -> bool:
        """Sets up monitoring of satellite status and notification assertion.

        The following events trigger assertion of the notification output:
        - New Forward Message received
        - Return Message completed (success or failure)
        - Trace event update (satellite status change)

        Returns:
            True if successful
        """
        self._log.debug('Enabling low power notifications')
        cmd = 'AT%EVMON=3.1;S88=1030'
        try:
            response = await self.command(cmd)
            if response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    async def lowpower_notifications_check(self) -> list:
        """Returns a list of relevant events or None."""
        self._log.debug('Querying low power notifications')
        reason = await self.notification_check()
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

    async def message_mo_send(self,
                              data: str,
                              data_format: int,
                              sin: int,
                              min: int = None,
                              name: str = None,
                              priority: int = 4) -> str:
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
        self._log.debug('Submitting message named {}'.format(name))
        try:
            if name is None:
                # Use the 8 least-signficant numbers of unix timestamp as unique
                name = str(int(time()))[-8:]
                self._log.debug('Assigned name {}'.format(name))
            elif len(name) > 8:
                name = name[0:8]   # risk duplicates create an ERROR resposne
                self._log.warning('Truncated name to {}'.format(name))
            if min is not None:
                _min = '.{}'.format(min)
            else:
                _min = ''
            if data_format == 1:
                data = '"{}"'.format(data)
            cmd = ('AT%MGRT="{}",{},{}{},{},{}'.format(name,
                                                       priority,
                                                       sin,
                                                       _min,
                                                       data_format,
                                                       data))
            response = await self.command(cmd)
            if response[0] == 'ERROR':
                raise AtException(AT_ERROR_CODES[response[1]])
            return name
        except AtException as e:
            raise e

    async def message_mo_state(self, name: str = None) -> list:
        """Returns the message state(s) requested.
        
        If no name filter is passed in, all available messages states
        are returned.  Returns False is the request failed.

        Args:
            name: The unique message name in the modem queue

        Returns:
            `list` of `dict` with `name`, `state`, `size` and `sent`

        """
        self._log.debug('Querying transmit message state{}'.format(
            ' ={}'.format(name) if name else 's'))
        cmd = 'AT%MGRS{}'.format('="{}"'.format(name) if name else '')
        try:
            response = await self.command(cmd)
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
                    states.append({
                        'name': name.replace('"', ''),
                        'state': int(state),
                        'size': int(size),
                        'bytes_sent': int(sent),
                        })
            return states
        except AtException:
            return None
    
    @staticmethod
    def message_state_name(state: int):
        if state not in MESSAGE_STATES:
            raise ValueError('Message state {} not defined'.format(state))
        return MESSAGE_STATES[state]

    async def message_mo_cancel(self, name: str) -> bool:
        """Cancels a mobile-originated message in the Tx ready state."""
        self._log.debug('Cancelling message {}'.format(name))
        try:
            response = await self.command('AT%MGRC="{}"'.format(name))
            if response[0] == 'ERROR':
                self._log.error('Error cancelling message {} - {}'.format(
                    name, AT_ERROR_CODES[response[1]]))
                return False
            return True
        except AtException:
            return False

    async def message_mo_clear(self) -> int:
        """Clears the modem transmit queue.
        
        Returns:
            Count of messages deleted, or -1 in case of error

        """
        self._log.debug('Clearing transmit queue of return messages')
        cancelled_count = 0
        open_count = 0
        try:
            response = await self.command('AT%MGRSC')
            if response[0] == 'ERROR':
                return -1
            response.remove('OK')
            if '%MGRS:' in response:
                response.remove('%MGRS:')
            for message in response:
                if '%MGRS:' in message:
                    message = message.replace('%MGRS:', '').strip()
                parts = message.split(',')
                status = int(parts[4])
                name = parts[0].replace('"', '')
                if status == 8:
                    cancelled_count += 1
                elif status < 6:
                    cancel_explicit = await self.message_mo_cancel(name)
                    if not cancel_explicit:
                        open_count += 1
            if open_count > 0:
                self._log.warning('{} messages still in transmit queue'.format(
                    open_count))
            return cancelled_count
        except AtException:
            return -1

    async def message_mt_waiting(self) -> Union[list, None]:
        """Returns a list of received mobile-terminated message information.
        
        Returns:
            List of (name, number, priority, sin, state, length, received)

        """
        self._log.debug('Checking receive queue for forward messages')
        try:
            response = await self.command('AT%MGFN')
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
                    waiting.append({'name': name.replace('"', ''),
                                    'sin': int(sin),
                                    'priority': int(priority),
                                    'state': int(state),
                                    'length': int(length),
                                    'received': int(received)})
            return waiting
        except AtException:
            return None

    @staticmethod
    def _message_mt_parse(mgfg_response: str,
                          data_format: int) -> dict:
        #:%MGFG:"<msgName>",<msgNum>,<priority>,<sin>,<state>,<length>,<data_format>,<data>
        parts = mgfg_response.replace('%MGFG:', '').strip().split(',')
        sys_msg_num, sys_msg_seq = parts[1].split('.')
        msg_sin = int(parts[3])
        data_str_no_sin = parts[7]
        if data_format == FORMAT_HEX:
            data = '{:02X}'.format(msg_sin) + data_str_no_sin
            databytes = bytes.fromhex(data)
        elif data_format == FORMAT_B64:
            databytes = bytes([msg_sin]) + b64decode(data_str_no_sin)
            data = b64encode(databytes).decode('ascii')
        elif data_format == FORMAT_TEXT:
            data_str_no_sin = data_str_no_sin[1:len(data_str_no_sin) - 1]
            data = '\\{:02x}'.format(msg_sin) + data_str_no_sin
            databytes = bytes([msg_sin])
            i = 0
            while i < len(data_str_no_sin):
                if data_str_no_sin[i] == '\\' and i < len(data_str_no_sin) - 1:
                    if data_str_no_sin[i + 1] in '0123456789ABCDEF':
                        databytes += bytes([int(data_str_no_sin[i+1:i+3], 16)])
                        i += 3
                else:
                    databytes += data_str_no_sin[i].encode('utf-8')
                    i += 1
        return {
            'name': parts[0].replace('"', ''),
            'system_message_number': int(sys_msg_num),
            'system_message_sequence': int(sys_msg_seq),
            'priority': int(parts[2]),
            'sin': msg_sin,
            'min': databytes[1],
            'state': int(parts[4]),
            'length': int(parts[5]),
            'data_format': data_format,
            'raw_payload': data,
            'bytes': databytes,
        }

    async def message_mt_get(self,
                             name: str,
                             data_format: int = FORMAT_B64,
                             verbose: bool = True) -> Union[dict, bytes]:
        """Returns the payload of a specified mobile-terminated message.
        
        Payload is presented as a string with encoding based on data_format. 

        Args:
            name: The unique name in the modem queue e.g. FM01.01
            data_format: text=1, hex=2, base64=3 (default)
            verbose: if True returns a dictionary, otherwise raw payload bytes

        Returns:
            The encoded data as a string

        """
        self._log.debug('Retrieving forward message {}'.format(name))
        try:
            #: AT%MGFG="<msgName>",<data_format>
            response = await self.command('AT%MGFG="{}",{}'.format(
                name, data_format))
            if response is None or response[0] == 'ERROR':
                return None
            # response.remove('OK')
            message = self._message_mt_parse(response[0], data_format=data_format)
            return message if verbose else message['bytes']
        except AtException:
            return None

    async def message_mt_delete(self, name: str) -> bool:
        """Marks a Return message for deletion by the modem.
        
        Args:
            name: The unique mobile-terminated name in the queue

        Returns:
            True if the operation succeeded

        """
        self._log.debug('Marking forward message {} for deletion'.format(name))
        try:
            response = await self.command('AT%MGFM="{}"'.format(name))
            if response is None or response[0] == 'ERROR':
                return False
            return True
        except:
            return False

    async def event_monitor_get(self) -> Union[list, None]:
        """Returns a list of monitored/cached events.
        As a list of <class.subclass> strings which includes an asterisk
        for each new event that can be retrieved.

        Returns:
            list of strings <class.subclass[*]> or None
        """
        self._log.debug('Querying monitored events')
        try:
            result = await self.command('AT%EVMON')
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

    async def event_monitor_set(self, eventlist: list) -> bool:
        """Sets trace events to monitor.

        Args:
            eventlist: list of tuples (class, subclass)

        Returns:
            True if successfully set
        """
        self._log.debug('Setting event monitors: {}'.format(eventlist))
        #: AT%EVMON{ = <c1.s1>[, <c2.s2> ..]}
        cmd = ''
        try:
            if eventlist is not None:
                for monitor in eventlist:
                    if isinstance(monitor, tuple):
                        if len(cmd) > 0:
                            cmd += ','
                        cmd += '{}.{}'.format(monitor[0], monitor[1])
            result = await self.command('AT%EVMON={}'.format(cmd))
            if result is None or result[0] == 'ERROR':
                return False
            return True
        except:
            return False

    async def event_get(self,
                        event: tuple,
                        raw: bool = True) -> Union[str, dict, None]:
        """Gets the cached event by class/subclass.

        Args:
            event: tuple of (class, subclass)
            raw: Returns the raw text string if True
        
        Returns:
            String if raw=True, dictionary if raw=False or None
        """
        self._log.debug('Querying events: {}'.format(event))
        #: AT%EVNT=c,s
        #: res %EVNT: <dataCount>,<signedBitmask>,<MTID>,<timestamp>,
        # <class>,<subclass>,<priority>,<data0>,<data1>,..,<dataN>
        if not (isinstance(event, tuple) and len(event) == 2):
            raise AtException('event_get expects (class, subclass)')
        try:
            result = await self.command('AT%EVNT={},{}'.format(
                event[0], event[1]))
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
                    event['data'][i] = _to_signed32(int(event['data'][i]))
                else:
                    event['data'][i] = int(event['data'][i])
                i += 1
            # TODO lookup class/subclass definitions
            return result[0] if raw else event
        except AtException:
            return None

    async def notification_control_set(self, event_map: list) -> bool:
        """Sets the event notification bitmask.

        Args:
            event_map: list of tuples (event_name, bool)
        
        Returns:
            True if successful.
            
        """
        self._log.debug('Setting event notifications: {}'.format(event_map))
        #: ATS88=bitmask
        notifications_changed = False
        old_notifications = await self.notification_control_get()
        if old_notifications is None:
            return False
        bitmask = list('0' * len(old_notifications))
        i = 0
        for event in event_map:
            if event[0] not in NOTIFICATION_BITMASK:
                raise ValueError('Invalid event {}'.format(event[0]))
            i = 0
            for key in reversed(old_notifications):
                bit = '1' if old_notifications[key] or bitmask[i] == '1' else '0'
                if key == event[0]:
                    notify = event[1]
                    if old_notifications[key] != notify:
                        bit = '1' if notify else '0'
                        notifications_changed = True
                        # self.notifications[key] = notify
                bitmask[i] = bit
                i += 1
        if notifications_changed:
            bitmask_int = int('0b' + ''.join(bitmask), 2)
            try:
                result = await self.command('ATS88={}'.format(bitmask_int))
                if result is None or result[0] == 'ERROR':
                    return False
            except AtException:
                return False
        return True
    
    async def notification_control_get(self) -> Union[OrderedDict, None]:
        """Returns the current notification configuration bitmask."""
        self._log.debug('Querying event notification controls')
        #: ATS88?
        try:
            result = await self.command('ATS88?')
            if result is None or result[0] == 'ERROR':
                return None
            return _notifications_dict(int(result[0]))
        except AtException:
            return None

    async def notification_check(self) -> OrderedDict:
        """Returns the current active event notification bitmask.
        Clears the value of S89 upon reading.
        """
        self._log.debug('Querying event notification triggers')
        #: ATS89?
        try:
            result = await self.command('ATS89?')
            if result is None or result[0] == 'ERROR':
                return None
            return _notifications_dict(int(result[0]))
        except AtException:
            return None

    async def satellite_status(self) -> dict:
        """Returns the control state and C/No.
        
        Returns:
            Dictionary with state (int), snr (float), beamsearch (int),
                state_name (str), beamsearch_name (str), or None if error.

        """
        self._log.debug('Querying satellite status/SNR')
        cmd = 'ATS90=3 S91=1 S92=1 S116? S122? S123?'
        try:
            response = await self.command(cmd)
            if response is None or response[0] == 'ERROR':
                return None
            response.remove('OK')
            cn_0, ctrl_state, beamsearch_state = response
            cn_0 = int(cn_0) / 100.0
            ctrl_state = int(ctrl_state)
            beamsearch_state = int(beamsearch_state)
            return {
                'state': ctrl_state,
                'state_name': CONTROL_STATES[ctrl_state],
                'snr': cn_0,
                'beamsearch': beamsearch_state,
                'beamsearch_name': BEAMSEARCH_STATES[beamsearch_state],
            }
        except AtException:
            return None

    @staticmethod
    def sat_status_name(ctrl_state: int) -> str:
        """Returns human-readable definition of a control state value.
        
        Raises:
            ValueError if ctrl_state is not found.
        """
        if ctrl_state not in CONTROL_STATES:
            raise ValueError('Control state {} not found'.format(ctrl_state))
        return CONTROL_STATES[ctrl_state]

    @staticmethod
    def sat_beamsearch_name(beamsearch_state: int) -> str:
        if beamsearch_state not in BEAMSEARCH_STATES:
            raise ValueError('Beam search state {} not found'.format(beamsearch_state))
        return BEAMSEARCH_STATES[beamsearch_state]

    async def shutdown(self) -> bool:
        """Tell the modem to prepare for power-down."""
        self._log.debug('Requesting power down')
        try:
            response = await self.command('AT%OFF')
            if response is None or response[0] == 'ERROR':
                return False
            return True
        except AtException:
            return False

    async def time_utc(self) -> Union[str, None]:
        """Returns current UTC time of the modem in ISO format."""
        self._log.debug('Requesting UTC network time')
        try:
            response = await self.command('AT%UTC')
            if response is None or response[0] == 'ERROR':
                return None
            return response[0].replace('%UTC: ', '').replace(' ', 'T') + 'Z'
        except AtException:
            return None

    async def s_register_get(self, register: int) -> Union[int, None]:
        """Returns the value of the S-register requested.

        Args:
            register: The S-register number

        Returns:
            integer value or None

        """
        self._log.debug('Querying register value S{}'.format(register))
        try:
            response = await self.command('ATS{}?'.format(register))
            if response is None or response[0] == 'ERROR':
                return None
            return int(response[0])
        except AtException:
            return None

    async def s_register_get_all(self) -> Union[list, None]:
        """Returns a list of S-register definitions.
        R=read-only, S=signed, V=volatile
        
        Returns:
            tuple(register, RSV, current, default, minimum, maximum) or None
        """
        self._log.debug('Querying S-register values')
        #: AT%SREG
        #: Sreg, RSV, CurrentVal, DefaultVal, MinimumVal, MaximumVal
        try:
            result = await self.command('AT%SREG')
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
