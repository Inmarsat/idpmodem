#! /usr/bin/env python
# encoding: utf-8
"""
AT command protocol for satellite modems operating on Inmarsat's 
IsatData Pro service
"""

from __future__ import print_function

from serial.threaded import LineReader, ReaderThread
from time import sleep
import threading

try:
    import queue
except ImportError:
    import Queue as queue

try:
    import crcxmodem
except ImportError:
    from idpmodem import crcxmodem


class ATException(Exception):
    pass


class ATProtocol(LineReader):
    """
    Protocol factory for the IDP Modem, accepts only one message at a time
    """

    TERMINATOR = b'\r\n'
    EX_CRC_ENABLED = 'CRC enabled'

    def __init__(self, crc: bool = False, unsolicited_callback=None):
        super(ATProtocol, self).__init__()
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
        self.lock = threading.Lock()

    def stop(self):
        """
        Stop the unsolicited processing thread, abort pending commands, if any.
        """
        self.alive = False
        self.unsolicited.put(None)
        self.responses.put('<exit>')

    def _run_unsolicited(self):
        """
        Process unsolicited in a separate thread so that input thread is not
        blocked.
        """
        while self.alive:
            try:
                self.handle_unsolicited(self.unsolicited.get())
            except:
                raise ATException("Unexpected error handling unsolicited data")

    def data_received(self, data):
        """Buffer received data, find TERMINATOR, call handle_packet"""
        self.buffer.extend(data)
        if self.pending_command is not None:
            if data == b'\r':
                if self.buffer == self.pending_command.encode() + b'\r':
                    echo = self.buffer
                    self.buffer = bytearray(b'')
                    self.handle_echo(echo)
            elif data == b'\n':
                if (self.buffer != bytearray(b'\r\n')
                    and self.buffer != bytearray(b'\n')):
                    '''
                    if (self.buffer.count(self.TERMINATOR) == 2
                        or self.buffer.find(b'*') == 0):
                    '''
                    # response, response code or checksum
                    packet = self.buffer
                    self.buffer = bytearray(b'')
                    self.handle_packet(packet)
                elif self.buffer == bytearray(b'\n'):
                    # drop any extra newlines
                    self.buffer = bytearray(b'')
        else:
            if data == b'\n':
                packet = self.buffer
                self.buffer = bytearray(b'')
                self.handle_unsolicited(packet)

    def handle_echo(self, echo):
        self.responses.put(echo.decode(self.ENCODING, self.UNICODE_HANDLING))

    def handle_packet(self, packet):
        self.handle_line(packet.decode(self.ENCODING, self.UNICODE_HANDLING))

    def handle_line(self, line):
        """
        Handle input from serial port, check for unsolicited.  Override
        LineReader
        """
        if self.pending_command is None:
            self.unsolicited.put(line)
        else:
            self.responses.put(line)

    def handle_unsolicited(self, unsolicited):
        """
        Spontaneous message received.
        """
        print('Unsolicited message: {}'.format(unsolicited))

    @staticmethod
    def _clean_response(lines):
        for l in range(len(lines)):
            lines[l] = lines[l].strip()
        clean = list(filter(lambda line: line != '' and line != 'OK', lines))
        return clean

    def _get_crc(self, at_cmd):
        """
        Returns the CRC-16-CCITT (initial value 0xFFFF) checksum using crcxmodem module.

        :param at_cmd: the AT command to calculate CRC on
        :return: the CRC for the AT command

        """
        if self.crc:
            return '{}*{:04X}'.format(at_cmd, crcxmodem.crc(at_cmd, 0xffff))
        else:
            return at_cmd
    
    @staticmethod
    def _validate_crc(lines, crc):
        validate = ''
        for line in lines:
            validate += '{}'.format(line)
        expected_crc = '{:04X}'.format(crcxmodem.crc(validate, 0xffff))
        return expected_crc == crc

    def command(self, command, timeout=45):
        """
        Set an AT command and wait for the response.
        """
        if self.pending_command:
            raise ATException("AT command rejected another is pending")
        with self.lock:  # ensure that just one thread is sending commands at once
            command = self._get_crc(command)
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
                                raise ATException(
                                    "Invalid CRC for {}".format(command))
                        else:
                            raise ATException(self.EX_CRC_ENABLED)
                    else:
                        lines.append(line)
                except queue.Empty:
                    raise ATException(
                        'AT command timeout ({!r})'.format(command))


class ByteReaderThread(ReaderThread):
    """
    Modifies the ReaderThread class to process bytes individually. This
    is due to complexities of optional checksum use for long serial lines.
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


class IdpModemException(Exception):
    pass


class IdpModem(ATProtocol):
    """
    Methods specific to an IDP modem
    """
    
    # TERMINATOR = b'\r\n'   # (default)

    def __init__(self, crc=False):
        super(IdpModem, self).__init__(crc)
    
    def connection_made(self, transport):
        super(IdpModem, self).connection_made(transport)
        self.transport.serial.reset_input_buffer()
    
    def config_restore_nvm(self):
        return 'ERROR' if len(self.command("ATZ")) > 0 else 'OK'

    def config_restore_factory(self):
        return 'ERROR' if len(self.command("AT%F")) > 0 else 'OK'
    
    def config_nvm_report(self):
        header, at_config, s_regs = self.command("AT&V")
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
        print(at_config, reg_config)
        return (at_config, reg_config)

    def config_volatile_report(self):
        register_list = [
            'S39',   # GNSS Mode
            'S41',   # GNSS Fix Timeout
            'S51',   # Wakeup Interval
            'S55',   # GNSS Continuous
            'S56',   # GNSS Jamming Status
            'S57',   # GNSS Jamming Indicator
        ]
        at_cmd = 'AT'
        for reg in register_list:
            at_cmd += '{}?'.format(reg if at_cmd == 'AT' else ' ' + reg)
        s_regs = self.command(at_cmd)
        volatile_regs = {}
        for r in range(len(s_regs)):
            volatile_regs[register_list[r]] = int(s_regs[r])
        return volatile_regs

    def config_nvm_save(self):
        return 'ERROR' if len(self.command("AT&W")) > 0 else 'OK'

    def crc_enable(self, enable=True):
        at_cmd = 'AT%CRC={}'.format(1 if enable else 0)
        return 'ERROR' if len(self.command(at_cmd)) > 0 else 'OK'

    def mobile_id(self):
        return self.command("AT+GSN")[0].replace('+GSN:', '').strip()

    def versions(self):
        versions = self.command("AT+GMR")[0].replace('+GMR:', '').strip()
        fw_ver, hw_ver, at_ver = versions.split(',')
        return (fw_ver, hw_ver, at_ver)

    def sat_status_snr(self):
        ctrl_state, cn_0 = self.command("ATS90=3 S91=1 S92=1 S122? S116?")
        ctrl_state = int(ctrl_state)
        cn_0 = int(cn_0) / 100.0
        return (ctrl_state, cn_0)

    def message_mo_send(self, data, data_format=3, name=None, priority=4, sin=None, min=None):
        result = self.command('AT%MGRT="{}",{},{}{},{},{}'.format(
                                name,
                                priority,
                                sin,
                                '.{}'.format(min) if min is not None else '',
                                data_format,
                                '"{}"'.format(data) if data_format == 1 else data))
        return 'ERROR' if len(result) > 0 else 'OK'

    def message_mo_state(self, name=None):
        return self.command("AT%MGRS{}".format(
                            '="{}"'.format(name) if name is not None else ''))
    
    def message_mt_waiting(self):
        return self.command("AT%MGFN")

    def message_mt_get(self, name):
        return self.command('AT%MGFG="{}"'.format(name))

    def message_mt_delete(self, name):
        return self.command('AT%MGFM="{}"'.format(name))

    def gnss_nmea_get(self, refresh=0, fix_age=30,
                nmea=['RMC', 'GSA', 'GGA', 'GSV']):
        """
        Returns a list of NMEA-formatted sentences from GNSS
        """
        NMEA_SUPPORTED = ['RMC', 'GGA', 'GSA', 'GSV']
        MIN_STALE_SECS = 1
        MAX_STALE_SECS = 600
        MIN_WAIT_SECS = 1
        MAX_WAIT_SECS = 600
        # determine maximum time to wait for response
        if 0 < refresh < fix_age:
            fix_age = refresh
        stale_secs = min(MAX_STALE_SECS, max(MIN_STALE_SECS, fix_age))
        wait_secs = min(MAX_WAIT_SECS, max(
            MIN_WAIT_SECS, int(max(45, stale_secs - 1))))
        sentences = ''
        for sentence in nmea:
            sentence = sentence.upper()
            if sentence in NMEA_SUPPORTED:
                if len(sentences) > 0:
                    sentences += ','
                sentences += '"{}"'.format(sentence)
            else:
                raise IdpModemException("Unsupported NMEA sentence: {}"
                                        .format(sentence))
        response = self.command("AT%GPS={},{},{}"
                                .format(stale_secs, wait_secs, sentences),
                                timeout=wait_secs+5)
        response[0] = response[0].replace('%GPS: ', '')
        return response


# Self-test
if __name__ == '__main__':
    try:
        import serial

        ser = serial.Serial('/dev/ttyUSB1', baudrate=9600, timeout=60)
        with ByteReaderThread(ser, IdpModem) as idp_modem:
            try:
                idp_modem.config_restore_nvm()
            except ATException as e:
                if 'CRC' in e.args[0]:
                    idp_modem.crc = True
                    idp_modem.config_restore_nvm()
                else:
                    raise e
            mobile_id = idp_modem.mobile_id()
            print("Mobile ID: {}".format(mobile_id))
            fw_ver, hw_ver, at_ver = idp_modem.versions()
            print("Versions: FW={} HW={} AT={}".format(fw_ver, hw_ver, at_ver))
            idp_modem.config_volatile_report()
            # nmea_sentences = idp_modem.gnss_nmea_get()
            # print("{}".format(nmea_sentences))
        
        '''# --- Alternative Implementation ---
        t = ByteReaderThread(ser, IdpModem)
        t.start()
        transport, idp_modem = t.connect()
        # <operations under 'with' above>
        t.close()
        # '''
    except Exception as e:
        print(e)
