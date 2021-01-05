"""Codec functions for IDP Common Message Format supported by Inmarsat MGS."""

from binascii import b2a_base64
from math import log2, ceil
from struct import pack, unpack
from typing import Union
from warnings import WarningMessage, warn

from idpmodem.constants import FORMAT_HEX, FORMAT_B64

__version__ = '2.0.0'


DATA_TYPES = (
    'bool',
    'int_8',
    'uint_8',
    'int_16',
    'uint_16',
    'int_32',
    'uint_31',   # unique to SkyWave IDP-series Lua 5.3
    'uint_32',   # not supported by all ORBCOMM/SkyWave terminals
    'int_64',    # not supported by all ORBCOMM/SkyWave terminals
    'uint_64',   # not supported by all ORBCOMM/SkyWave terminals
    'float',     # not supported by ORBCOMM/SkyWave terminals
    'double',    # not supported by ORBCOMM/SkyWave terminals
    'string',
    'data',
    # 'array',   # TODO: support for array type
    # 'enum',   #TODO: support for enum type
)


def _get_optimal_bits(value_range: tuple) -> int:
    if not (isinstance(value_range, tuple) and len(value_range) == 2 and
        value_range[0] <= value_range[1]):
        #: non-compliant
        raise ValueError('value_range must be of form (min, max)')
    total_range = value_range[1] - value_range[0]
    total_range += 1 if value_range[0] == 0 else 0
    optimal_bits = max(1, ceil(log2(value_range[1] - value_range[0])))
    return optimal_bits


def _twos_comp(val: int, bits: int) -> int:
    """compute the 2's complement of int value val"""
    if (val & (1 << (bits - 1))) != 0: # if sign bit is set e.g., 8bit: 128-255
        val = val - (1 << bits)        # compute negative value
    return val                         # return positive value as is


def _bits2string(bits: str) -> str:
    stringbytes = int(bits, 2).to_bytes(int(len(bits) / 8), 'big')
    return stringbytes.decode() or '\0'


class Field:
    """A data field within a Common Message Format message.
    
    Attributes:
        name (str): The field name
        data_type (str): A supported data type for encoding/decoding
        value (any): The value which is type dependent
        value_range (tuple): The min, max of allowed values
        bits (int): size in bits
        description (str): An optional description
        optional (bool): Indicates if the field is optional
        fixed (bool): Indicates if the field size is fixed (or variable)
    """
    def __init__(self,
                 name: str,
                 data_type: str,
                 value: any,
                 value_range: tuple = None,
                 bits: int = None,
                 description: str = None,
                 optional: bool = False,
                 fixed: bool = True):
        """Initialize the field.
        
        Raises:
            ValueError for invalid data type
        """
        if not (isinstance(name, str) or name == ''):
            raise ValueError("Field name must be non-empty string")
        self.name = name
        self.description = description
        self.optional = optional
        self.fixed = fixed
        if not fixed or optional:
            raise NotImplementedError('optional and fixed currently unsupported')
        if not data_type in DATA_TYPES:
            raise ValueError("Unsupported data_type {}".format(data_type))
        self.data_type = data_type
        if (data_type == 'bool'): # and isinstance(value, bool)
            if bits is not None and bits != 1:
                warn('bits must be 1 for boolean', WarningMessage)
            self.bits = 1
            self.value = bool(value)
        elif 'int' in data_type:
            default_bits = int(data_type.split('_')[1])
            self.bits = bits or default_bits
            if 'uint' in data_type and int(value >= 0):
                self.value = min(int(value), 2**self.bits - 1)
            else:
                if int(value) < -int(2**self.bits / 2):
                    self.value = -int(2**self.bits / 2)
                elif int(value) > int(2**self.bits / 2 - 1):
                    self.value = int(2**self.bits - 1)
                else:
                    self.value = int(value)
        elif (data_type == 'string' and isinstance(value, str) or
              (data_type == 'data' and
              (isinstance(value, bytearray) or isinstance(value, bytes)))):
            if bits is None or bits % 8 > 0:
                raise ValueError('Multiple of 8 bits must be specified for {}'
                    .format(data_type))
            self.bits = bits
            self.value = str(value) if data_type == 'string' else value
        elif ((data_type == 'float' or data_type == 'double') and
              isinstance(value, float)):
            self.bits = 32 if data_type == 'float' else 64
            self.value = float(value)
        else:
            raise ValueError("Unsupported data_type {} or type mismatch"
                .format(data_type))
        # optimize bits
        self.value_range = None
        if value_range is not None:
            self.bits = _get_optimal_bits(value_range)
            self.value_range = value_range
        self._format = '0{}b'.format(self.bits)
    
    def __repr__(self):
        from pprint import pformat
        return pformat(vars(self), indent=4)

    
class Fields(list):
    def __init__(self):
        super(Fields, self).__init__()
    
    def add(self, field: Field) -> bool:
        """Add a field to the list.

        Args:
            field (object): A valid Field
        
        Raises:
            ValueError if there is a duplicate or invalid name,
                invalid value_range or unsupported data_type

        """
        if not isinstance(field, Field):
            raise ValueError('Invalid field definition')
        for f in self:
            if f.name == field.name:
                raise ValueError('Duplicate name found in message')
        self.append(field)
        return True

    def __getitem__(self, n: Union[str, int]) -> Field:
        if isinstance(n, str):
            for field in self:
                if field.name == n:
                    return field
            raise ValueError('Field name {} not found'.format(n))
        return super(Fields, self).__getitem__(n)

    def delete(self, name: str):
        for f in self:
            if f.name == name:
                self.remove(f)
                return True
        return False


class CommonMessageFormat:
    """The structure for Message Definition Files uploaded to a Mailbox.
    
    Attributes:
        name (str): The message name
        sin (int): The Service Identification Number
        min (int): The Message Identification Number
        fields (list): An array of Fields
        description (str): Optional description
        is_forward (bool): Indicates if the message is mobile-terminated

    """

    def __init__(self,
                 name: str,
                 sin: int,
                 min: int,
                 description: str = None,
                 is_forward: bool = False):
        self.name = name
        self.description = description
        self.is_forward = is_forward
        if isinstance(sin, int) and sin in range(16, 256):
            self.sin = sin
        else:
            raise ValueError('Invalid sin ({})'.format(sin) +
                ' must be in range 16..255')
        if isinstance(min, int) and min in range (0, 256):
            self.min = min
        else:
            raise ValueError('Invalid min ({})'.format(min) + 
                'must be integer type in range 0..255')
        self.fields = Fields()

    def ota_size(self):
        ota_bits = 2 * 8
        for field in self.fields:
            ota_bits += field.bits + (1 if field.optional else 0)
        return ceil(ota_bits / 8)

    def derive(self, databytes: bytes):
        binary_str = ''
        for b in databytes:
            binary_str += '{:08b}'.format(int(b))
        bit_offset = 16
        for field in self.fields:
            binary = binary_str[bit_offset:(bit_offset + field.bits)]
            if field.data_type == 'bool':
                field.value = True if binary == '1' else False
            elif 'uint' in field.data_type:
                field.value = int(binary, 2)
            elif 'int' in field.data_type:
                field.value = _twos_comp(int(binary, 2), len(binary))
            elif field.data_type == 'float':
                field.value = unpack('>f', int(binary, 2).to_bytes(4, 'big'))[0]
            elif field.data_type == 'double':
                field.value = unpack('>d', int(binary, 2).to_bytes(8, 'big'))[0]
            elif field.data_type == 'string':
                field.value = _bits2string(binary)
            elif field.data_type == 'data':
                field.value = int(binary, 2).to_bytes(int(field.bits / 8), 'big')
            bit_offset += field.bits

    def encode(self,
               data_format: int = FORMAT_B64,
               exclude: list = None) -> dict:
        """Encodes using the specified data format (base64 or hex).

        Args:
            data_format (int): 2=ASCII-Hex, 3=base64
        
        Returns:
            Dictionary with sin, min, data_format and data to pass into AT%MGRT

        """
        if data_format not in [FORMAT_B64, FORMAT_HEX]:
            raise ValueError('data_format {} unsupported'.format(data_format))
        # encoded = '{}.{},{},'.format(self.sin, self.min, data_format)
        bin_str = ''
        for field in self.fields:
            data_type = field.data_type
            value = field.value
            bits = field.bits
            _format = field._format
            bin_field = ''
            if field.optional:
                if isinstance(exclude, list) and field.name in exclude:
                    bin_field = '0'
                    continue
                bin_field = '1'
            if 'int' in data_type and isinstance(value, int):
                if value < 0:
                    inv_bin_field = format(-value, _format)
                    comp_bin_field = ''
                    i = 0
                    while len(comp_bin_field) < len(inv_bin_field):
                        comp_bin_field += '1' if inv_bin_field[i] == '0' else '0'
                        i += 1
                    bin_field = format(int(comp_bin_field, 2) + 1, _format)
                else:
                    bin_field = format(value, _format)
            elif data_type == 'bool' and isinstance(value, bool):
                bin_field = '1' if value == True else '0'
            elif data_type == 'float' and isinstance(value, float):
                f = '{0:0%db}' % bits
                bin_field = f.format(
                    int(hex(unpack('!I', pack('!f', value))[0]), 16))
            elif data_type == 'double' and isinstance(value, float):
                f = '{0:0%db}' % bits
                bin_field = f.format(
                    int(hex(unpack('!Q', pack('!d', value))[0]), 16))
            elif data_type == 'string' and isinstance(value, str):
                bin_field = ''.join(format(ord(c), '08b') for c in value)
            elif (data_type == 'data' and
                  (isinstance(value, bytearray) or isinstance(value, bytes))):
                bin_field = ''.join(format(b, '08b') for b in value)
            else:
                raise NotImplementedError('data_type {} unsupported'.format(
                    data_type))
            if len(bin_field) > bits:
                raise ValueError('Field {} expected {} bits but processed {}'
                    .format(field.name, bits, len(bin_field)))
            if len(bin_field) < bits:
                # TODO: check padding on strings...this should pad with NULL
                bin_field += ''.join('0' for pad in range(len(bin_field), bits))
            bin_str += bin_field
        payload_pad_bits = len(bin_str) % 8
        while payload_pad_bits > 0:
            bin_str += '0'
            payload_pad_bits -= 1
        hex_str = ''
        index_byte = 0
        while len(hex_str) / 2 < len(bin_str) / 8:
            hex_str += format(
                int(bin_str[index_byte:index_byte + 8], 2), '02X').upper()
            index_byte += 8
        if data_format == FORMAT_HEX:
            data = hex_str
        else:
            data = b2a_base64(bytearray.fromhex(hex_str)).strip().decode()
        return {
            'sin': self.sin,
            'min': self.min,
            'data_format': data_format,
            'data': data
        }

    def get_xml(self):
        """Returns the XML definition for a Message Definition File."""
        # TODO: create Message Definition File
        raise NotImplementedError


class MessageDefinitions(object):
    """
    TODO: Not Implemented
    """

    class Service(object):
        """
        TODO: docstring
        """
        sin_LOW = 16
        sin_HIGH = 255
        def __init__(self,
                     sin: int,
                     name: str,
                     description: str = '', 
                     return_messages: list = [],
                     forward_messages: list = []):
            if not sin in range(self.sin_LOW, self.sin_HIGH + 1):
                raise ValueError('Service must have sin in range {}..{}'
                                .format(self.sin_LOW, self.sin_HIGH))
            self.sin = sin
            if not isinstance(name, str) or name == '':
                raise ValueError('Service name must be a non-empty string')
            self.name = name
            self.description = description
            self.messages_return = []
            self.messages_forward = []
            if isinstance(return_messages, list):
                for msg in return_messages:
                    if not isinstance(msg, CommonMessageFormat):
                        raise ValueError('Invalid message structure')
                    self.add_return_message(msg)
            if isinstance(forward_messages, list):
                for msg in forward_messages:
                    if not isinstance(msg, CommonMessageFormat):
                        raise ValueError('Invalid message structure')
                    if not msg.is_forward:
                        raise ValueError('Not defined as a forward message')
                    self.add_forward_message(msg)
        
        def _add_message(self,
                         message: CommonMessageFormat,
                         msg_list: list,
                         overwrite: bool = True) -> bool:
            if not isinstance(message, CommonMessageFormat):
                raise TypeError('Invalid message')
            if not message.sin == self.sin:
                raise ValueError('sin mismatch expected {} got {}'.format(
                    self.sin, message.sin))
            # Check for conflict
            for i in range(0, len(msg_list)):
                if msg_list[i].min == message.min:
                    if overwrite:
                        msg_list[i] = message
                        return True
                    return False
            msg_list.append(message)
            return True
        
        def _remove_message(self, min: int, msg_list: list) -> bool:
            for i in range(0, len(msg_list)):
                if msg_list[i].min == min:
                    msg_list.remove(msg_list[i])
                    return True
            return False

        def add_return_message(self, message: CommonMessageFormat) -> bool:
            return self._add_message(message, self.messages_return)
        
        def remove_return_message(self, min: int) -> bool:
            return self._remove_message(min, self.messages_return)
            
        def add_forward_message(self, message: CommonMessageFormat) -> bool:
            if isinstance(message, CommonMessageFormat) and message.is_forward:
                return self._add_message(message, self.messages_forward)
            return False
        
        def remove_forward_message(self, min: int) -> bool:
            return self._remove_message(min, self.messages_forward)

        def get_xml(self):
            # TODO: return XML format compliant with Inmarsat MDF
            raise NotImplementedError

    def __init__(self):
        self.services = []
    
    def add_service(self,
                    sin: int,
                    name: str,
                    description: str = '') -> bool:
        """Adds a service if the sin is not already defined.
        
        Args:
            sin (int): Service Identification Number
            name (str): Unique name of the service
            description (str): Optional description
        
        Returns:
            True if successful

        """
        # Check for conflict
        for i in range(0, len(self.services)):
            if self.services[i].sin == sin:
                return False
        self.services.append(MessageDefinitions.Service(sin, name, description))
        return True
    
    def get_mdf(self):
        raise NotImplementedError


def encode_at(return_message: CommonMessageFormat,
              data_format: int = FORMAT_B64) -> str:
    """Returns the partial AT%MGRS command string from <sin>."""
    return return_message.encode_at(data_format=data_format)


def decode_at(at_response: str,
              definitions: MessageDefinitions) -> CommonMessageFormat:
    """Decodes a common message format Forward message based on a given MDF.
    
    Args:
        at_response (str): the AT command reponse from %MGFG
        xml_file (object): the Message Definition File

    Returns:
        A CommonMessageFormat message structure
    
    Raises:
        NotImplementedError

    """
    #: %MGFG:"<msgName>",<msgNum>,<priority>,<sin>,<state>,<length>,<dataFormat>[,<data>]
    raise NotImplementedError
