"""
AT command protocol factory for Inmarsat IDP satellite messaging modems.

A threaded serial interface that sends and receives AT commands, 
decoding/abstracting typically used operations.
"""

from serial.threaded import LineReader, ReaderThread
import threading
from time import time, sleep
from typing import Callable, Tuple

try:
    import queue
except ImportError:
    import Queue as queue

try:
    import crcxmodem
except ImportError:
    from idpmodem import crcxmodem


class AtException(Exception):
    """Base class for AT command exceptions."""
    pass


class AtCommandError(AtException):
    pass


class AtTimeout(AtException):
    pass


class AtCrcError(AtException):
    pass


class AtCrcConfigError(AtException):
    pass


class AtUnsolicited(AtException):
    pass


class SearchableQueue(object):
    """Mimics relevant FIFO queue functions to avoid duplicate commands.
    
    Makes use of queue Exceptions to mimic a standard queue.

    Attributes:
        max_size: The maximum queue depth.
    """
    def __init__(self, max_size=100):
        self._queue = []
        self.max_size = max_size
    
    def contains(self, item):
        """Returns true if the queue contains the item."""
        for i in self._queue:
            if i == item: return True
        return False

    def put(self, item, index=None):
        """Adds the item to the queue.
        
        Args:
            item: The object to add to the queue.
            index: The queue position (None=end)
        """
        if len(self._queue) > self.max_size:
            raise queue.Full
        if index is None:
            self._queue.append(item)
        else:
            self._queue.insert(index, item)

    def put_exclusive(self, item):
        """Adds the item to the queue only if unique in the queue.
        
        Args:
            item: The object to add to the queue.
        
        Raises:
            queue.Full if a duplicate item is in the queue.
        """
        if not self.contains(item):
            self.put(item)
        else:
            raise queue.Full('Duplicate item in queue')

    def get(self):
        """Pops the first item from the queue.
        
        Returns:
            An object from the queue.
        
        Raises:
            queue.Empty if nothing in the queue.
        """
        if len(self._queue) > 0:
            return self._queue.pop(0)
        else:
            raise queue.Empty
    
    def qsize(self):
        """Returns the current size of the queue."""
        return len(self._queue)
    
    def empty(self):
        """Returns true if the queue is empty."""
        return len(self._queue) == 0


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

    ERROR_CODES = {
        '0': 'OK',
        '4': 'ERROR',
        '100': 'INVALID_CRC',
        '101': 'COMMAND_UNKNOWN',
        '102': 'INVALID_PARAMETER',
        '103': 'MESSAGE_TOO_LONG',
        '104': 'DATA_MODE_ERROR',
        '105': 'SYSTEM_ERROR',
        '106': 'INSUFFICIENT_RESOURCES',
        '107': 'MESSAGE_NAME_ALREADY_IN_USE',
        '108': 'GNSS_TIMEOUT',
        '109': 'MESSAGE_UNAVAILABLE',
        '110': 'RESERVED',
        '111': 'RESOURCE_BUSY',
        '112': 'ATTEMPT_WRITE_TO_READ_ONLY_REGISTER',
    }

    def __init__(self,
                 crc: bool = False,
                 unsolicited_callback: Callable = None):
        """Initialize with CRC and optional callback

        Args:
            crc: Use CRC error checking (for long serial line).
            unsolicited_callback: Handler for non-command data.

        """
        super(AtProtocol, self).__init__()
        self.crc = crc
        self.alive = True
        self.pending_command = None
        self.commands = SearchableQueue()
        self._command_thread = threading.Thread(
            target=self._run_command,
            name='at_commands',
            daemon=True)
        self._command_thread.start()
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

    def _run_command(self):
        """Process pending commands in a separate thread.
        
        Processes a single command at a time.

        Raises:
            AtException: If an unexpected handling error occurs.
        
        """
        while self.alive:
            try:
                if not self.pending_command and not self.commands.empty():
                    (next_command, timeout) = self.commands.get()
                    self.command(next_command, timeout)
            except:
                raise AtException("Unexpected error handling command queue")
    
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
        return '{}*{:04X}'.format(command, crcxmodem.crc(command, 0xffff))
    
    @staticmethod
    def _validate_crc(lines: list, crc: str) -> bool:
        """Calculates and validates the response CRC against expected"""
        validate = ''
        for line in lines:
            validate += '{}'.format(line)
        expected_crc = '{:04X}'.format(crcxmodem.crc(validate, 0xffff))
        return expected_crc == crc

    def command(self, command: str, timeout: int = 5) -> (list, int):
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
        if self.pending_command is not None:
            self.commands.put((command, timeout))
        with self._lock:  # ensure that just one thread is sending commands at once
            command = self._get_crc(command) if self.crc else command
            self.pending_command = command
            command_sent = time()
            self.write_line(command)
            response_received = None
            lines = []
            while True:
                try:
                    line = self.responses.get(timeout=timeout)
                    content = line.strip()
                    if content == command:
                        pass   # ignore echo
                    elif ((content == 'OK' or content == 'ERROR')
                          and not self.crc):
                        if response_received is None:
                            response_received = time()
                        lines.append(line)
                        return self._clean_response(lines,
                                                    command_sent,
                                                    response_received)
                    elif content.startswith('*'):
                        if response_received is None:
                            response_received = time()
                        if self.crc:
                            self.pending_command = None
                            crc = content.replace('*', '')
                            if self._validate_crc(lines, crc):
                                return self._clean_response(lines,
                                                            command_sent,
                                                            response_received)
                            else:
                                raise AtCrcError(
                                    'INVALID_CRC for {}'.format(command))
                        else:
                            raise AtCrcConfigError('CRC_DETECTED')
                    else:
                        if response_received is None:
                            response_received = time()
                        lines.append(line)
                except queue.Empty:
                    raise AtTimeout(
                        'TIMEOUT ({!r})'.format(command))


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
            except serial.SerialException as e:
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


class IdpModem(AtProtocol):
    """A class abstracting methods specific to an IDP modem."""

    CONTROL_STATES = {
        '0': 'Stopped',
        '1': 'Waiting for GNSS',
        '2': 'Starting search',
        '3': 'Beam search',
        '4': 'Beam found',
        '5': 'Beam acquired',
        '6': 'Beam switch in progress',
        '7': 'Registration in progress',
        '8': 'Receive only',
        '9': 'Receiving global bulletin board',
        '10': 'Active',
        '11': 'Blocked',
        '12': 'Confirm previously registered beam',
        '13': 'Confirm requested beam',
        '14': 'Connect to confirmed beam',
    }

    def __init__(self):
        super(IdpModem, self).__init__()
        self._at_stats = _AtStatistics()

    def connection_made(self, transport):
        """Clears the input buffer on connect.
        
        May be overridden by user subclass.
        """
        super(IdpModem, self).connection_made(transport)
        self.transport.serial.reset_input_buffer()
    
    def connection_lost(self, exc):
        """Raises an exception on disconnect.
        
        May be overridden by user subclass.
        """
        super(IdpModem, self).connection_lost(exc)
    
    def command(self, command, timeout = 5):
        """Overrides the super class function to add metrics.
        
        Args:
            command (str): The AT command
            timeout (int): The command timeout (default 5 seconds)
        
        Returns:
            Response object.

        """
        (response, latency) = super(IdpModem, self).command(command=command,
                                timeout=timeout)
        self._at_stats.update(command, latency)
        # if response[0] == 'ERROR':
        #     raise AtCommandError('ERROR')
        return response

    def error_detail(self):
        """Queries the last error code.
        
        Returns:
            Reason description string or None if error on error.
        """
        response = self.command('ATS80?')
        if response[0] == 'ERROR':
            #: handle CRC error
            return None
        reason = self.ERROR_CODES[response[0]]
        return reason

    def config_restore_nvm(self) -> bool:
        """Sends the ATZ command and returns True on success."""
        response = self.command('ATZ')
        return True if response[0] == 'OK' else False

    def config_restore_factory(self) -> bool:
        """Sends the AT&F command and returns True on success."""
        response = self.command('AT&F')
        return True if response[0] == 'OK' else False
    
    def config_nvm_report(self) -> Tuple[dict, dict]:
        """Sends the AT&V command to retrive S-register settings.
        
        Returns:
            A tuple with two dictionaries, or None if failed.
            at_config with booleans crc, echo, quiet and verbose
            reg_config with S-register tags and integer values
        """
        response = self.command('AT&V')
        if response[0] == 'ERROR':
            return None
        response.remove('OK')
        header, at_config, s_regs = response
        del header  # unused
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

    def config_volatile_report(self) -> dict:
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
        """Sends the AT&W command and returns 'OK' or 'ERROR'."""
        response = self.command('AT&W')
        return True if response[0] == 'OK' else False

    def crc_enable(self, enable: bool = True) -> bool:
        """Sends the AT%CRC command and returns success flag.
        
        Args:
            enable: turn on CRC if True else turn off
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
            Tuple with (hardware, firmware, at) version or None if error.
        """
        response = self.command("AT+GMR")
        if response[0] == 'ERROR':
            return None
        versions = response[0].replace('+GMR:', '').strip()
        fw_ver, hw_ver, at_ver = versions.split(',')
        return (hw_ver, fw_ver, at_ver)

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
        return True if response[0] == 'OK' else False

    def gnss_nmea_get(self, stale_secs: int = 1, wait_secs: int = 30,
                      nmea: list = ['RMC', 'GSA', 'GGA', 'GSV']) -> list:
        """Returns a list of NMEA-formatted sentences from GNSS.

        Args:
            stale_secs: Maximum age of fix in seconds (1..600)
            wait_secs: Maximum time to wait for fix (1..600)

        Returns:
            List of NMEA sentences

        Raises:
            ValueError if parameter out of range

        """
        NMEA_SUPPORTED = ['RMC', 'GGA', 'GSA', 'GSV']
        BUFFER_SECONDS = 5
        if stale_secs not in range(1, 600+1) or wait_secs not in range(1, 600+1):
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
            return None
        response.remove('OK')
        response[0] = response[0].replace('%GPS: ', '')
        return response

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
            Name of the message if successful, or None
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
        return name if response[0] == 'OK' else None

    def message_mo_state(self, name: str = None) -> str:
        """Returns the message state(s) requested.
        
        If no name filter is passed in, all available messages states
        are returned.  Returns False is the request failed.

        Args:
            name: The unique message name in the modem queue

        Returns:
            State: UNAVAILABLE, TX_READY, TX_SENDING, TX_COMPLETE, TX_FAILED

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
        return STATES[int(response[0])]
    
    def message_mo_cancel(self, name: str) -> bool:
        """Cancels a mobile-originated message in the Tx ready state."""
        response = self.command('AT%MGRC={}'.format(name))
        return True if response[0] == 'OK' else False

    def message_mt_waiting(self) -> list:
        """Returns a list of received mobile-terminated message information.
        
        Returns:
            List of (name, number, priority, sin, state, length, received)

        """
        response = self.command('AT%MGFN')
        if response[0] == 'ERROR':
            return None
        response.remove('OK')
        #: name, number, priority, sin, state, length, bytes_received
        return response

    def message_mt_get(self, name: str, data_format: int = 3) -> str:
        """Returns the payload of a specified mobile-terminated message.
        
        Payload is presented as a string with encoding based on data_format. 

        Args:
            name: The unique name in the modem queue e.g. FM01.01
            data_format: text=1, hex=2, base64=3 (default)

        Returns:
            The encoded data as a string

        """
        response = self.command('AT%MGFG="{},{}"'.format(name, data_format))
        if response[0] == 'ERROR':
            return None
        response.remove('OK')
        #: name, number, priority, sin, state, length, data_format, data
        data_str = response[7]
        return data_str

    def message_mt_delete(self, name: str) -> bool:
        """Marks a message for deletion by the modem.
        
        Args:
            name: The unique mobile-terminated name in the queue

        Returns:
            True if the operation succeeded

        """
        response = self.command('AT%MGFM="{}"'.format(name))
        return True if response[0] == 'OK' else False

    def sat_status_snr(self) -> Tuple[str, float]:
        """Returns the control state and C/No.
        
        Returns:
            Tuple with (state: int, C/No: float) or None if error.
        """
        response = self.command("ATS90=3 S91=1 S92=1 S122? S116?")
        if response[0] == 'ERROR':
            return None
        response.remove('OK')
        ctrl_state, cn_0 = response
        ctrl_state = int(ctrl_state)
        cn_0 = int(cn_0) / 100.0
        return (ctrl_state, cn_0)

    def sat_status_description(self, ctrl_state: int) -> str:
        """Returns human-readable definition of a control state value.
        
        Raises:
            ValueError if ctrl_state is not found.
        """
        for s in self.CONTROL_STATES:
            if int(s) == ctrl_state:
                return self.CONTROL_STATES[s]
        raise ValueError('Control state {} not found'.format(ctrl_state))

    def shutdown(self) -> bool:
        """Sleep in preparation for power-down."""
        response = self.command('AT%OFF')
        return True if response[0] == 'OK' else False

    def utc_time(self) -> str:
        response = self.command('AT%UTC')
        if response[0] == 'ERROR':
            return None
        return response[0]

    def s_register_get(self, register: str) -> int:
        if not register.startswith('S'):
            # TODO: better Exception handling
            raise Exception('Invalid S-register {}'.format(register))
        response = self.command('AT{}?'.format(register))
        if response[0] == 'ERROR':
            return None
        return int(response[0])

    def raw_command(self, command: str = 'AT') -> list:
        """Sends a command and returns a list of responses."""
        response = self.command(command)
        return response


# Self-test
if __name__ == '__main__':
    try:
        import serial
        
        def on_connect():
            print('Connected')

        ser = serial.Serial('/dev/ttyUSB1', baudrate=9600, timeout=60)
        t = ByteReaderThread(ser, IdpModem)
        t.start()
        transport, idp_modem = t.connect()
        idp_modem.on_connect = on_connect
        try:
            idp_modem.config_restore_nvm()
        except AtCrcConfigError:
            idp_modem.crc = True
            idp_modem.config_restore_nvm()
        mobile_id = idp_modem.device_mobile_id()
        print('Mobile ID: {}'.format(mobile_id))
        fw_ver, hw_ver, at_ver = idp_modem.device_version()
        print('Versions: FW={} HW={} AT={}'.format(fw_ver, hw_ver, at_ver))
        
        def send_another_command():
            sleep(5)
            print('Requesting satellite status')
            (state, snr) = idp_modem.sat_status_snr()
            print('State: {} | SNR: {}'.format(state, snr))

        test_thread = threading.Thread(target=send_another_command,
                                       name='send_another_command',
                                       daemon=True)
        test_thread.start()
        gnss_timeout = 15   # seconds
        print('Requesting GNSS location with timeout {}s'.format(gnss_timeout))
        nmea_sentences = idp_modem.gnss_nmea_get(wait_secs=gnss_timeout)
        if nmea_sentences is not None:
            for sentence in nmea_sentences:
                print('{}'.format(sentence))
        else:
            reason = idp_modem.error_detail()
            print(reason)
        for stat in idp_modem._at_stats.response_times:
            print('{}: {} ms'.format(
                stat, idp_modem._at_stats.response_times[stat][0]))
        delay = 10
        print('Waiting for unsoliticted input {} seconds'.format(delay))
        sleep(delay)
    
    except KeyboardInterrupt:
        print('Interrupted by user')

    except Exception as e:
        idp_modem.stop()
        print('EXCEPTION: {}'.format(e))
    
    finally:
        t.close()
