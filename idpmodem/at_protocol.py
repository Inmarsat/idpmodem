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


class AtQueueFull(AtException):
    pass


class AtTimeout(AtException):
    pass


class AtCrcError(AtException):
    pass


class AtCrcConfigError(AtException):
    pass


class AtUnsolicited(AtException):
    pass


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
        self.responses = queue.Queue()
        self.unsolicited = queue.Queue()
        self._unsolicited_thread = threading.Thread(
            target=self._run_unsolicited)
        self._unsolicited_thread.daemon = True
        self._unsolicited_thread.name = 'at_unsolicited'
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
        if self.unsolicited_callback is not None:
            self.unsolicited_callback(unsolicited)
        else: 
            print('Unsolicited message: {}'.format(
                unsolicited.replace('\r', '<cr>').replace('\n', '<lf>')))

    @staticmethod
    def _clean_response(lines: list) -> list:
        """Removes empty lines from response"""
        for l in range(len(lines)):
            lines[l] = lines[l].strip()
        clean = list(filter(lambda line: line != '', lines))
        return clean

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

    def command(self, command: str, timeout: int = 5) -> list:
        """Send an AT command and wait for the response.

        Returns the response as a list.  If an error response code was
        received then 'ERROR' will be the only string in the list.

        .. todo: generalize for OK-only response, and provide error detail

        Args:
            command: The AT command
            timeout: Time to wait for response in seconds (default 5)
        
        Returns:
            The response as a list of strings, or 'ERROR' as the sole string

        Raises:
            AtQueueFull if another message is pending.
            AtCrcError if CRC does not match.
            AtCrcConfigError if CRC was returned unexpectedly.
            AtTimeout if the request timed out.

        """
        if self.pending_command:
            raise AtQueueFull('QUEUE_FULL command rejected another is pending')
        with self._lock:  # ensure that just one thread is sending commands at once
            command = self._get_crc(command) if self.crc else command
            self.pending_command = command
            self.write_line(command)
            lines = []
            while True:
                try:
                    line = self.responses.get(timeout=timeout)
                    content = line.strip()
                    if content == command:
                        pass   # ignore echo
                    elif content == 'OK' and not self.crc:
                        self.pending_command = None
                        lines.append(line)
                        return self._clean_response(lines)
                    elif content.startswith('*'):
                        if self.crc:
                            self.pending_command = None
                            crc = content.replace('*', '')
                            if self._validate_crc(lines, crc):
                                return self._clean_response(lines)
                            else:
                                raise AtCrcError(
                                    'INVALID_CRC for {}'.format(command))
                        else:
                            raise AtCrcConfigError('CRC_DETECTED')
                    else:
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


class IdpModem(AtProtocol):
    """A class abstracting methods specific to an IDP modem."""

    class _AtStatistics(object):
        """A private class for tracking AT command latency in ms."""
        def __init__(self):
            self.response_times = {
                'gnss': (0, 0),
                'non_gnss': (0, 0),
            }
        
        def update(self, command, submit_time):
            """Updates the latency statistics.
            
            Args:
                command (str): The AT command submitted.
                submit_time (int): The timestamp (unix) when it was submitted.

            """
            latency = int((time() - submit_time) * 1000)
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

    def __init__(self):
        super(IdpModem, self).__init__()
        self._at_stats = self._AtStatistics()

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
        submit_time = time()
        response = super(IdpModem, self).command(
                                                 command=command,
                                                 timeout=timeout
                                                )
        self._at_stats.update(command, submit_time)
        return response

    def error_detail(self):
        """Queries the last error code."""
        response = self.command('ATS80?')
        if response[0] == 'ERROR':
            #: handle CRC error
            return False
        return response

    def config_restore_nvm(self) -> bool:
        """Sends the ATZ command and returns True on success."""
        response = self.command('ATZ')
        return True if response[0] == 'OK' else False

    def config_restore_factory(self) -> bool:
        """Sends the AT&F command and returns True on success."""
        response = self.command('AT&F')
        return True if response[0] == 'OK' else False
    
    def config_nvm_report(self) -> Tuple[dict, dict]:
        """Returns the AT&V interface configuration and key settings.
        
        Returns:
            A tuple with two dictionaries, or False if failed.
            at_config with booleans crc, echo, quiet and verbose
            reg_config with S-register tags and integer values
        
        """
        response = self.command("AT&V")
        if response[0] == 'ERROR':
            return False
        #: else
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
            Dictionary of S-register values, or False if failed
            
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
            return False
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
        """Sends the AT%CRC command and returns 'OK' or 'ERROR'.
        
        Args:
            enable: turn on CRC if True else turn off

        """
        command = 'AT%CRC={}'.format(1 if enable else 0)
        response = self.command(command)
        return True if response[0] == 'OK' else False

    def get_mobile_id(self) -> str:
        """Returns the unique Mobile ID (Inmarsat serial number)."""
        response = self.command("AT+GSN")
        if response == 'ERROR':
            return False 
        return response[0].replace('+GSN:', '').strip()

    def get_versions(self) -> Tuple[str, str, str]:
        """Returns the hardware, firmware and AT versions."""
        response = self.command("AT+GMR")
        if response[0] == 'ERROR':
            return False
        versions = response[0].replace('+GMR:', '').strip()
        fw_ver, hw_ver, at_ver = versions.split(',')
        return (hw_ver, fw_ver, at_ver)

    def sat_status_snr(self) -> Tuple[str, float]:
        """Returns the control state and C/No."""
        response = self.command("ATS90=3 S91=1 S92=1 S122? S116?")
        if response[0] == 'ERROR':
            return False
        response.remove('OK')
        ctrl_state, cn_0 = response
        ctrl_state = int(ctrl_state)
        cn_0 = int(cn_0) / 100.0
        return (ctrl_state, cn_0)

    def message_mo_send(self,
                        data: str,
                        data_format: int = 3,
                        name: str = None,
                        priority: int = 4,
                        sin: int = None,
                        min: int = None
                        ) -> bool:
        """Submits a mobile-originated message to send.
        
        Args:
            data: 
            data_format: 
            name: 
            priority: 
            sin: 
            min: 

        Returns:
            True if successful

        """
        response = self.command('AT%MGRT="{}",{},{}{},{},{}'.format(
                                name,
                                priority,
                                sin,
                                '.{}'.format(min) if min is not None else '',
                                data_format,
                                '"{}"'.format(data) if data_format == 1 else data))
        return True if response[0] == 'OK' else False

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
            return False
        return STATES[int(response[0])]
    
    def message_mt_waiting(self) -> list:
        """Returns a list of received mobile-terminated message information.
        
        Returns:
            List of (name, number, priority, sin, state, length, received)

        """
        response = self.command('AT%MGFN')
        if response[0] == 'ERROR':
            return False
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
            return False
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

    def raw_command(self, command: str = 'AT') -> list:
        """Sends a command and returns a list of responses."""
        response = self.command(command)
        if response[0] == 'ERROR':
            return False
        return response

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
                                timeout=wait_secs+5)
        if response[0] == 'ERROR':
            return False
        response.remove('OK')
        response[0] = response[0].replace('%GPS: ', '')
        return response


# Self-test
if __name__ == '__main__':
    try:
        import serial
        
        ser = serial.Serial('/dev/ttyUSB1', baudrate=9600, timeout=60)
        '''
        with ByteReaderThread(ser, IdpModem) as idp_modem:
            try:
                idp_modem.config_restore_nvm()
            except AtException as e:
                if 'CRC' in e.args[0]:
                    idp_modem.crc = True
                    idp_modem.config_restore_nvm()
                else:
                    raise e
            mobile_id = idp_modem.mobile_id()
            print("Mobile ID: {}".format(mobile_id))
            fw_ver, hw_ver, at_ver = idp_modem.versions()
            print("Versions: FW={} HW={} AT={}".format(fw_ver, hw_ver, at_ver))
            idp_modem.config_nvm_report()
            # nmea_sentences = idp_modem.gnss_nmea_get()
            # print("{}".format(nmea_sentences))
        '''
        # #: --- Alternative Implementation ---
        t = ByteReaderThread(ser, IdpModem)
        t.start()
        transport, idp_modem = t.connect()
        try:
            idp_modem.config_restore_nvm()
        except AtCrcConfigError:
            idp_modem.crc = True
            idp_modem.config_restore_nvm()
        mobile_id = idp_modem.mobile_id()
        print('Mobile ID: {}'.format(mobile_id))
        fw_ver, hw_ver, at_ver = idp_modem.versions()
        print('Versions: FW={} HW={} AT={}'.format(fw_ver, hw_ver, at_ver))
        nmea_sentences = idp_modem.gnss_nmea_get()
        for sentence in nmea_sentences:
            print('{}'.format(sentence))
        for stat in idp_modem._at_stats.response_times:
            print('{}: {} ms'.format(
                stat, idp_modem._at_stats.response_times[stat][0]))
        t.close()
    
    except Exception as e:
        print(e)
