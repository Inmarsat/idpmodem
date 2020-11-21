# -*- coding: utf-8 -*-
"""AT command protocol (threaded) for Inmarsat IDP satellite messaging modems.

This module provides a threaded serial interface that sends and receives 
AT commands, decoding/abstracting typically used operations.
Based on the PySerial threaded protocol factory, using a byte reader.

"""

from base64 import b64decode, b64encode
from collections import OrderedDict
import platform
from serial import Serial, SerialException
from serial.threaded import LineReader, ReaderThread
import threading
from time import time, sleep
from typing import Callable, Tuple, Union

try:
    import queue
except ImportError:
    import Queue as queue

try:
    from .aterror import AtCrcConfigError, AtCrcError, AtException, AtTimeout, AtUnsolicited
    from crcxmodem import get_crc, validate_crc
    import constants
    import nmea
except ImportError:
    from idpmodem.crcxmodem import get_crc, validate_crc
    from idpmodem import constants
    from idpmodem import nmea

'''
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


class AtUnsolicited(AtException):
    """Indicates unsolicited data was received from the modem."""
    pass
'''

class AtProtocol(LineReader):
    """Threaded protocol factory for the IDP Modem.
    
    Accepts only one AT command at a time.  Handles command echo 
    terminated with <cr>, verbose response framed with <cr><lf>, 
    unsolicited data terminated with <lf>, and CRC error checking.

    Attributes:
        alive (bool): True while the factory is running.
        crc (bool): Indicates if CRC error checking is enabled.
        pending_command (str): The AT command being processed.
        responses (Queue): Queued responses to be processed as a line.
        unsolicited (Queue): Unexpected data received if no pending command.
        unsolicited_callback (Callable): optional callback function for 
        unexpected data
    """

    ERROR_CODES = constants.AT_ERROR_CODES
    DEFAULT_AT_TIMEOUT = 5

    def __init__(self,
                 crc: bool = False,
                 unsolicited_callback: Callable = None,
                 default_at_timeout: int = DEFAULT_AT_TIMEOUT):
        """Initialize with CRC and optional callback

        Args:
            crc: Use CRC error checking (for long serial line).
            unsolicited_callback: Handler for non-command data.

        """
        super(AtProtocol, self).__init__()
        self.crc = crc
        self.alive = True
        self.default_at_timeout = default_at_timeout
        self.pending_command = None
        self.responses = queue.Queue()
        self.unsolicited = queue.Queue()
        self._unsolicited_thread = threading.Thread(
            target=self._run_unsolicited,
            name='at_unsolicited',
            daemon=True)
        self._unsolicited_thread.start()
        self.unsolicited_callback = unsolicited_callback
        self._lock = threading.Lock()

    def stop(self):
        """Stop the data processing thread.
        
        Aborts pending commands, if any.
        """
        self.alive = False
        self.unsolicited.put(None)
        self.responses.put('<exit>')

    def _run_unsolicited(self):
        """Process unsolicited in a separate thread.
        
        Ensures the command response thread is not blocked.

        Raises:
            AtUnsolicited: If an unexpected handling error occurs.
        
        """
        while self.alive:
            try:
                self.handle_unsolicited(self.unsolicited.get())
            except:
                raise AtUnsolicited("Unexpected error handling unsolicited data")

    def data_received(self, data: bytearray):
        """Buffer received data and create packets for handlers.

        Args:
            data: a data byte received from the serial device

        """
        self.buffer.extend(data)
        if self.pending_command is not None:
            if data == b'\r':
                #: Echo case
                if self.buffer == self.pending_command.encode() + b'\r':
                    echo = self.buffer
                    self.buffer = bytearray(b'')
                    self.handle_packet(echo)
            elif data == b'\n':
                if (self.buffer != bytearray(b'\r\n')
                    and self.buffer != bytearray(b'\n')):
                    #: Framed/multiline response, error code or CRC
                    packet = self.buffer
                    self.buffer = bytearray(b'')
                    self.handle_packet(packet)
                elif self.buffer == bytearray(b'\n'):
                    #: (Unexpected) drop any empty lines
                    self.buffer = bytearray(b'')
        else:
            if data == b'\n':
                unsolicited = self.buffer
                self.buffer = bytearray(b'')
                self.handle_packet(unsolicited)

    def handle_packet(self, packet: bytearray):
        """Processes the buffer to unicode for parser handling.

        Duplicates LineReader method for clarity in this code.

        Args:
            packet: Raw binary data buffer from the serial port.

        """
        self.handle_line(packet.decode(self.ENCODING, self.UNICODE_HANDLING))

    def handle_line(self, line: str):
        """Enqueues lines for parsing by command handler.

        Overrides LineReader class method to distinguish unsolicited
        from expected responses.
        
        Args:
            line: The unicode string received from the serial port.
        """
        if self.pending_command is not None:
            self.responses.put(line)
        else:
            if line != '\n':
                self.unsolicited.put(line)

    def handle_unsolicited(self, unsolicited: str):
        """Calls a user-defined function with the unicode string.

        Args:
            unsolicited: A unicode string terminated by <lf>.

        """
        if unsolicited is not None:
            if self.unsolicited_callback is not None:
                self.unsolicited_callback(unsolicited)
            else: 
                print('Unsolicited message: {}'.format(
                    unsolicited.replace('\r', '<cr>').replace('\n', '<lf>')))

    @staticmethod
    def _clean_response(lines: list,
                        command_time: int=None,
                        response_time: int=None) -> list:
        """Removes empty lines from response and returns with latency.
        
        Args:
            lines: A list of reponse lines.
            command_time: The timestamp the command was sent to the modem.
            response_time: The timestamp of the first line of the response.
        
        Returns:
            Tuple with stripped lines, command_latency (or None)
        """
        if command_time is not None:
            if response_time is None: response_time = time()
            command_latency = round(response_time - command_time, 3)
        else:
            command_latency = None
        for l in range(len(lines)):
            lines[l] = lines[l].strip()
        clean = list(filter(lambda line: line != '', lines))
        return (clean, command_latency)

    def _get_crc(self, command: str) -> str:
        """Returns the command with CRC.
        
        Calculates CCITT-16 checksum.

        Args:
            command: The AT command or response to calculate CRC on
        
        Returns:
            The command with CRC appended after *

        """
        return get_crc(command)
    
    @staticmethod
    def _validate_crc(lines: list, crc: str) -> bool:
        """Calculates and validates the response CRC against expected"""
        validate = ''
        for line in lines:
            validate += '{}'.format(line)
        return validate_crc(validate, crc)

    def command(
        self, command: str, timeout: int = DEFAULT_AT_TIMEOUT
    ) -> (list, int):
        """Send an AT command and wait for the response.

        Returns the response as a list.  If an error response code was
        received then 'ERROR' will be the only string in the list.

        .. todo: generalize for OK-only response, and provide error detail

        Args:
            command: The AT command
            timeout: Time to wait for response in seconds (default 5)
        
        Returns:
            The response as a list of strings
            Command latency in seconds

        Raises:
            AtCrcError if CRC does not match.
            AtCrcConfigError if CRC was returned unexpectedly.
            AtTimeout if the request timed out.

        """
        with self._lock:  # ensure that just one thread is sending commands at once
            if timeout < self.default_at_timeout:
                timeout = self.default_at_timeout
            command = get_crc(command) if self.crc else command
            self.pending_command = command
            command_sent = time()
            self.write_line(command)
            response_received = None
            lines = []
            while self.pending_command is not None:
                try:
                    line = self.responses.get(timeout=timeout)
                    content = line.strip()
                    if content == command:
                        pass   # ignore echo
                    elif content == 'OK':
                        if response_received is None:
                            response_received = time()
                        lines.append(line)
                        if not (self.crc or '%CRC=1' in self.pending_command):
                            return self._clean_response(lines,
                                                        command_sent,
                                                        response_received)
                    elif content == 'ERROR':
                        if response_received is None:
                            response_received = time()
                        lines.append(line)
                        # wait in case CRC is following
                    elif content.startswith('*'):
                        if response_received is None:
                            response_received = time()
                        if self.crc or '%CRC=1' in self.pending_command:
                            self.pending_command = None
                            crc = content.replace('*', '')
                            if self._validate_crc(lines, crc):
                                return self._clean_response(lines,
                                                            command_sent,
                                                            response_received)
                            else:
                                raise AtCrcError(
                                    'INVALID_CRC_RESPONSE {}'.format(command))
                        else:
                            raise AtCrcConfigError('UNEXPECTED_CRC_DETECTED')
                    else:
                        if response_received is None:
                            response_received = time()
                        lines.append(line)
                except queue.Empty:
                    if not response_received:
                        raise AtTimeout('TIMEOUT ({!r})'.format(command))
                    if self.crc:
                        self.crc = False
                    return self._clean_response(lines,
                                                command_sent,
                                                response_received)


class ByteReaderThread(ReaderThread):
    """Modifies the ReaderThread class to process bytes individually.
    
    This is required due to complexities of optional checksum use 
    for long serial lines.

    """
    def run(self):
        """Reader loop"""
        self.name = 'bytereader@{}'.format(self.serial.name)
        if not hasattr(self.serial, 'cancel_read'):
            self.serial.timeout = 1
        self.protocol = self.protocol_factory()
        try:
            self.protocol.connection_made(self)
        except Exception as e:
            self.alive = False
            self.protocol.connection_lost(e)
            self._connection_made.set()
            return
        error = None
        self._connection_made.set()
        data = bytearray()
        while self.alive and self.serial.is_open:
            try:
                # read all that is there or wait for one byte (blocking)
                if self.serial.in_waiting > 0:
                    data = self.serial.read()
                    self.protocol.data_received(data)
                sleep(0.001)
            except SerialException as e:
                # probably some I/O problem such as disconnected USB serial
                # adapters -> exit
                error = e
                break
            except Exception as e:
                error = e
                break
        self.alive = False
        self.protocol.connection_lost(error)
        self.protocol = None


class _AtStatistics(object):
    """A private class for tracking AT command latency in ms."""
    def __init__(self):
        self.response_times = {
            'gnss': (0, 0),
            'non_gnss': (0, 0),
        }
    
    def update(self, command, response_latency):
        """Updates the latency statistics.
        
        Args:
            command (str): The AT command submitted.
            submit_time (int): The timestamp (unix) when it was submitted.

        """
        latency = int(response_latency * 1000)
        if '%GPS' in command:
            category = 'gnss'
        else:
            category = 'non_gnss'
        category_average = self.response_times[category][0]
        category_count = self.response_times[category][1]
        if category_average == 0:
            category_average = latency
        else:
            category_average = int((category_average + latency) / 2)
        category_count += 1
        self.response_times[category] = (category_average, category_count)


class IdpModemBusy(AtException):
    pass


class IdpModem(AtProtocol):
    """A protocol factory abstracting AT commands for an IDP modem."""

    def __init__(self):
        super(IdpModem, self).__init__()
        self._at_stats = _AtStatistics()
        # TODO REMOVE self.notifications = self._notifications_dict()
        self.busy = False
    
    def connection_made(self, transport):
        """Clears the input buffer on connect.
        TODO: not tested
        May be overridden by user subclass.
        """
        super(IdpModem, self).connection_made(transport)
        self.transport.serial.reset_input_buffer()
    
    def connection_lost(self, exc):
        """Raises an exception on disconnect.
        TODO: not tested
        May be overridden by user subclass.
        """
        super(IdpModem, self).connection_lost(exc)
    
    def command(
        self, command: str, timeout:int = 5, busy_timeout:int = 30
    ) -> list:
        """Overrides the super class function to add metrics.
        
        Args:
            command (str): The AT command
            timeout (int): The command timeout (default 5 seconds)
            busy_timeout (int): Timeout (seconds) for next waiting command
        
        Returns:
            Response object.

        """
        try:
            request_time = time()
            while self.busy:
                if time() - request_time > busy_timeout:
                    raise IdpModemBusy('{}s timeout awaiting prior command: {}'
                                    .format(busy_timeout, self.pending_command))
            self.busy = True
            (response, latency) = super(IdpModem, self).command(command=command,
                                    timeout=timeout)
            self._at_stats.update(command, latency)
            if response[0] == 'ERROR':
                (reason, latency) = super(IdpModem, self).command(
                                    command='ATS80?')
                self._at_stats.update('ATS80?', latency)
                if reason[0] == 'ERROR':
                    raise AtException('Unexpected error on ATS80?')
                response.append(self.ERROR_CODES[reason[0]])
            self.busy = False
            return response
        except (AtCrcConfigError, AtCrcError, AtTimeout) as e:
            self.busy = False
            raise e

    def error_detail(self):   #TODO REMOVE REDUNDANT VS COMMAND
        """Queries the last error code.
        
        Returns:
            Reason description string or None if error on error.
        """
        response = self.command('ATS80?')
        if response is None:
            return None
        elif response[0] == 'ERROR':
            #: TODO handle CRC error? raise?
            return response[1]
        reason = self.ERROR_CODES[response[0]]
        return reason

    def config_restore_nvm(self) -> bool:
        """Sends the ATZ command to restore from non-volatile memory.
        
        Returns:
            Boolean success.
        """
        response = self.command('ATZ')
        if response[0] == 'ERROR':
            return False
        return True

    def config_restore_factory(self) -> bool:
        """Sends the AT&F command and returns True on success."""
        response = self.command('AT&F')
        if response[0] == 'ERROR':
            return False
        return True
    
    def config_report(self) -> Tuple[dict, dict]:
        """Sends the AT&V command to retrive S-register settings.
        
        Returns:
            A tuple with two dictionaries or both None if failed
            at_config with booleans crc, echo, quiet and verbose
            reg_config with S-register tags and integer values
        """
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

    def config_volatile_report(self) -> Union[dict, None]:
        """Returns key S-register settings.
        
        GNSS Mode (S39), GNSS fix timeout (S41), GNSS Continuous (S55),
        GNSS Jamming Status (S56), GNSS Jamming Indicator (S57), 
        Low power Wakeup Period (S51)

        Returns:
            Dictionary of S-register values, or None if failed
            
        """
        register_list = [
            'S39',   #: GNSS Mode
            'S41',   #: GNSS Fix Timeout
            'S51',   #: Wakeup Interval
            'S55',   #: GNSS Continuous
            'S56',   #: GNSS Jamming Status
            'S57',   #: GNSS Jamming Indicator
        ]
        command = 'AT'
        for reg in register_list:
            command += '{}?'.format(reg if command == 'AT' else ' ' + reg)
        response = self.command(command)
        if response[0] == 'ERROR':
            return None
        #: else
        response.remove('OK')
        volatile_regs = {}
        for r in range(len(response)):
            volatile_regs[register_list[r]] = int(response[r])
        return volatile_regs

    def config_nvm_save(self) -> bool:
        """Sends the AT&W command and returns result."""
        response = self.command('AT&W')
        if response[0] == 'ERROR':
            return False
        return True

    def crc_enable(self, enable: bool = True) -> bool:
        """Sends the AT%CRC command and returns success flag.
        
        Args:
            enable: turn on CRC if True else turn off

        Returns:
            True if the operation succeeded else False
        """
        command = 'AT%CRC={}'.format(1 if enable else 0)
        response = self.command(command)
        if response[0] == 'ERROR':
            return False
        self.crc = enable
        return True

    def device_mobile_id(self) -> str:
        """Returns the unique Mobile ID (Inmarsat serial number).
        
        Returns:
            MobileID string or None if error.
        """
        response = self.command("AT+GSN")
        if response[0] == 'ERROR':
            return None 
        return response[0].replace('+GSN:', '').strip()

    def device_version(self) -> Tuple[str, str, str]:
        """Returns the hardware, firmware and AT versions.
        
        Returns:
            Dict with hardware, firmware, at version or all None if error.
        """
        response = self.command("AT+GMR")
        if response[0] == 'ERROR':
            return None
        versions = response[0].replace('+GMR:', '').strip()
        fw_ver, hw_ver, at_ver = versions.split(',')
        return {'hardware': hw_ver, 'firmware': fw_ver, 'at': at_ver}

    def gnss_continuous_set(self, interval: int=0, doppler: bool=True) -> bool:
        """Sets the GNSS continous mode (0 = on-demand).
        
        Args:
            interval: Seconds between GNSS refresh.
            doppler: Often required for moving assets.
        
        Returns:
            True if successful setting.
        """
        if interval < 0 or interval > 30:
            raise ValueError('GNSS continuous interval must be in range 0..30')
        response = self.command('AT%TRK={}{}'.format(
            interval, ',{}'.format(1 if doppler else 0)))
        if response[0] == 'ERROR':
            return False
        return True

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
        BUFFER_SECONDS = self.default_at_timeout
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
        response = self.command('AT%GPS={},{},{}'
                                .format(stale_secs, wait_secs, sentences),
                                timeout=wait_secs + BUFFER_SECONDS)
        if response[0] == 'ERROR':
            return response[1]
        response.remove('OK')
        response[0] = response[0].replace('%GPS: ', '')
        return response

    def location_get(self, stale_secs: int = 1, wait_secs: int = 35):
        """Returns a location object
        """
        nmea_sentences = self.gnss_nmea_get(stale_secs, wait_secs)
        if isinstance(nmea_sentences, str):
            return None
        location = nmea.location_get(nmea_sentences)
        return location

    def message_mo_send(self,
                        data: str,
                        data_format: int = 3,
                        name: str = None,
                        priority: int = 4,
                        sin: int = None,
                        min: int = None
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
            return response[1]
        # TODO: spin thread to check status until complete/remove
        return name

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
    
    def message_mo_cancel(self, name: str) -> bool:
        """Cancels a mobile-originated message in the Tx ready state."""
        response = self.command('AT%MGRC={}'.format(name))
        if response[0] == 'ERROR':
            return False
        return True

    def message_mo_clear(self) -> int:
        """Clears the modem transmit queue.
        
        Returns:
            Count of messages deleted, or -1 in case of error
        """
        list_response = self.command('AT%MGRL')
        if list_response[0] == 'ERROR':
            return -1
        list_response.remove('OK')
        if '%MGRL:' in list_response:
            list_response.remove('%MGRL:')
        message_count = len(list_response)
        for msg in list_response:
            name = msg.replace('%MGRL: ', '').split(',')[0]
            del_response = self.command('AT%MGRD={}C'.format(name))
            if del_response[0] == 'ERROR':
                return -1
        return message_count

    def message_mt_waiting(self) -> Union[list, None]:
        """Returns a list of received mobile-terminated message information.
        
        Returns:
            List of (name, number, priority, sin, state, length, received)

        """
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
        try:
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
        except Exception as e:
            print(e)

    def message_mt_delete(self, name: str) -> bool:
        """Marks a Return message for deletion by the modem.
        
        Args:
            name: The unique mobile-terminated name in the queue

        Returns:
            True if the operation succeeded

        """
        response = self.command('AT%MGFM="{}"'.format(name))
        if response is None or response[0] == 'ERROR':
            return False
        return True

    def event_monitor_get(self) -> Union[list, None]:
        """Returns a list of monitored/cached events.
        As a list of <class.subclass> strings which includes an asterisk
        for each new event that can be retrieved.

        Returns:
            list of strings <class.subclass[*]> or None
        """
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

    def event_monitor_set(self, eventlist: list) -> bool:
        """Sets trace events to monitor.

        Args:
            eventlist: list of tuples (class, subclass)

        Returns:
            True if successfully set
        """
        #: AT%EVMON{ = <c1.s1>[, <c2.s2> ..]}
        cmd = ''
        for monitor in eventlist:
            if isinstance(monitor, tuple):
                if len(cmd) > 0:
                    cmd += ','
                cmd += '{}.{}'.format(monitor[0], monitor[1])
        result = self.command('AT%EVMON={}'.format(cmd))
        if result is None or result[0] == 'ERROR':
            return False
        return True

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
            result = self.command('ATS88={}'.format(bitmask))
            if result is None or result[0] == 'ERROR':
                return False
        return True
    
    def notification_control_get(self) -> Union[OrderedDict, None]:
        """Returns the current notification configuration bitmask."""
        #: ATS88?
        result = self.command('ATS88?')
        if result is None or result[0] == 'ERROR':
            return None
        return self._notifications_dict(int(result[0]))

    def notification_check(self) -> OrderedDict:
        """Returns the current active event notification bitmask.
        Clears the value of S89 upon reading.
        """
        #: ATS89?
        result = self.command('ATS89?')
        if result is None or result[0] == 'ERROR':
            return None
        template = self._notifications_dict(int(result[0]))
        return template

    def sat_status_snr(self) -> Tuple[str, float]:
        """Returns the control state and C/No.
        
        Returns:
            Tuple with (state: int, C/No: float) or None if error.
        """
        response = self.command("ATS90=3 S91=1 S92=1 S122? S116?")
        if response is None or response[0] == 'ERROR':
            return (None, None)
        response.remove('OK')
        ctrl_state, cn_0 = response
        ctrl_state = int(ctrl_state)
        cn_0 = int(cn_0) / 100.0
        return (ctrl_state, cn_0)

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
        response = self.command('AT%OFF')
        if response is None or response[0] == 'ERROR':
            return False
        return True

    def utc_time(self) -> Union[str, None]:
        """Returns current UTC time of the modem in ISO format."""
        response = self.command('AT%UTC')
        if response is None or response[0] == 'ERROR':
            return None
        return response[0].replace('%UTC: ', '').replace(' ', 'T') + 'Z'

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
        response = self.command('AT{}?'.format(register))
        if response is None or response[0] == 'ERROR':
            return None
        return int(response[0])

    def sreg_get_all(self) -> Union[list, None]:
        """Returns a list of S-register definitions.
        R=read-only, S=signed, V=volatile
        
        Returns:
            tuple(register, RSV, current, default, minimum, maximum) or None
        """
        #: AT%SREG
        #: Sreg, RSV, CurrentVal, DefaultVal, MinimumVal, MaximumVal
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

    def raw_command(self, command: str = 'AT', timeout: int = 5) -> list:
        """Sends a command and returns a list of responses and response code."""
        response = self.command(command, timeout)
        return response


def get_modem_thread(port: str = '/dev/ttyUSB0', baudrate: int = 9600) -> tuple:
    """Gets a threaded IDP modem connection / protocol factory.

    Args:
        port: The serial port name (default /dev/ttyUSB0)
        baudrate: The serial baud rate (default 9600)

    Returns:
        (IdpModem, Thread)
    """
    ser = Serial(port, baudrate=baudrate)
    t = ByteReaderThread(ser, IdpModem)
    t.start()
    transport, idp_modem = t.connect()
    del transport   #: unusued
    if platform.system() == 'Windows':
        idp_modem.default_at_timeout = 10
    return (idp_modem, t)


# Self-test
if __name__ == '__main__':
    try:
        idp_modem, t = get_modem_thread()
        try:
            connected = idp_modem.config_restore_nvm()
            if not connected:
                raise Exception('Could not connect to modem')
            print('Modem connected')
        except AtCrcConfigError:
            idp_modem.crc = True
            idp_modem.config_restore_nvm()
        crc_enabled = idp_modem.crc_enable()
        mobile_id = idp_modem.device_mobile_id()
        print('Mobile ID: {}'.format(mobile_id))
        versions = idp_modem.device_version()
        print('Versions: FW={} HW={} AT={}'.format(
              versions['firmware'], versions['hardware'], versions['at']))
        
        def send_another_command():
            sleep(0.5)
            print('Requesting satellite status')
            state = None
            snr = None
            while state is None:
                (state, snr) = idp_modem.sat_status_snr()
            print('State: {} | SNR: {} dB'.format(
                idp_modem.sat_status_name(state), snr))
            waiting = idp_modem.message_mt_waiting()
            if isinstance(waiting, list) and len(waiting) > 0:
                print('{} MT messages waiting'.format(len(waiting)))

        test_thread = threading.Thread(target=send_another_command,
                                       name='send_another_command',
                                       daemon=True)
        # test_thread.start()
        '''
        gnss_timeout = 15   # seconds
        print('Requesting GNSS location with timeout {}s'.format(gnss_timeout))
        nmea_sentences = idp_modem.gnss_nmea_get(wait_secs=gnss_timeout)
        if isinstance(nmea_sentences, list):
            for sentence in nmea_sentences:
                print('{}'.format(sentence))
        else:
            print(nmea_sentences)
        registers = idp_modem.sreg_get_all()
        print(registers)
        '''
        to_monitor = (3, 1)
        mon = idp_modem.event_monitor_set([to_monitor])
        monitored = idp_modem.event_monitor_get()
        print('Monitored: {}'.format(monitored))
        if monitored is not None:
            for mon in monitored:
                if mon[-1] == '*':
                    to_get = tuple(int(i) for i in mon[0:-1].split('.'))
                    print(idp_modem.event_get(to_get))
        idp_modem.notification_control_set({'event_cached': True})
        event_notifications = idp_modem.notification_control_get()
        print('Notifications enabled: {}'.format(event_notifications))
        event_check = idp_modem.notification_check()
        print('Notifications active: {}'.format(event_check))
        print('Latency Statistics:')
        for stat in idp_modem._at_stats.response_times:
            print('  {}: {} ms'.format(
                stat, idp_modem._at_stats.response_times[stat][0]))
        delay = 5
        print('Waiting for unsoliticted input {} seconds'.format(delay))
        sleep(delay)
    
    except KeyboardInterrupt:
        print('Interrupted by user')

    except Exception as e:
        idp_modem.stop()
        print('EXCEPTION: {}'.format(e))
    
    finally:
        t.close()
