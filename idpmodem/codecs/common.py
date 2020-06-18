#!/usr/bin/env python
"""
Codec functions for IDP Common Message Format supported by Inmarsat MGS
"""

from binascii import b2a_base64
import struct

try:
    from idpmodem import FORMAT_HEX, FORMAT_BASE64
except ImportError:
    FORMAT_HEX = 2
    FORMAT_BASE64 = 3

__version__ = '1.0.0'


class CommonMessageFormat(object):

    data_types = (
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

    class Field(object):
        def __init__(self, name, data_type, value, field_size, description=None):
            self.name = name
            if data_type in CommonMessageFormat.data_types:
                self.data_type = data_type
            else:
                raise ValueError("Invalid data type, must be in: ({})".format(CommonMessageFormat.data_types))
            self.value = value
            self.field_size = field_size
            self.description = description

    def __init__(self, msg_sin, msg_min, name=None, description=None):
        self.name = name
        self.description = description
        if isinstance(msg_sin, int) and msg_sin in range(16, 256):
            self.sin = msg_sin
        else:
            raise ValueError("Invalid SIN ({}) must be in range 16..255".format(msg_sin))
        if isinstance(msg_min, int) and msg_min in range (0, 256):
            self.min = msg_min
        else:
            raise ValueError("Invalid MIN ({}) must be integer type in range 0..255".format(msg_min))
        self.fields = []
        self.size = None

    @staticmethod
    def validate_type(value, data_type):
        if data_type == 'bool':
            return isinstance(value, bool)
        # TODO: more validation

    def add_field(self, name, data_type, value, bits=None, 
                  value_range=None, description=None):
        """
        Add a field to the message.

        :param name: (string)
        :param data_type: (string) from supported types
        :param value: the value (compliant with data_type)
        :param bits: number of bits for message packing

        """
        # TODO: make it so fields cannot be added/deleted/modified without explicit class methods
        # field = {}
        field_size = None
        if (isinstance(name, str) and name != ''):
            for i in range(0, len(self.fields)):
                if self.fields[i].name == name:
                    raise ValueError("Duplicate name found in message")
            if value_range is not None:
                if isinstance(value_range, tuple) and len(value_range) == 2:
                    # TODO: calculate optimal bits to encode
                    pass
                else:
                    raise ValueError("Field value_range must be a tuple")
            if data_type in self.data_types:
                # field['data_type'] = data_type
                if (data_type == 'bool' and isinstance(value, bool)
                    or 'int' in data_type and isinstance(value, int)
                    or data_type == 'string' and isinstance(value, str)
                    or (data_type == 'float' or data_type == 'double') and isinstance(value, float)):
                    # calculate field_size 
                    if bits is None:
                        if data_type == 'bool':
                            field_size = '01b'
                        elif 'int' in data_type:
                            field_size = '0{}b'.format(data_type.split('_')[1])
                        elif data_type == 'float':
                            field_size = '032b'
                        elif data_type == 'double':
                            field_size = '064b'
                    else:
                        if isinstance(bits, int) and bits > 0:
                            field_size = '0{}b'.format(bits)
                        else:
                            raise ValueError("Field bits must be int above 0")
                elif (data_type == 'string' and isinstance(value, str)
                        or data_type == 'data' and isinstance(value, bytearray)):
                    if bits is not None:
                        field_size = '0{}b'.format(bits)
                    else:
                        raise ValueError(
                            "Number of bits must be specified for {}"
                            .format(data_type))
                else:
                    raise ValueError("Unsupported data_type {} or type mismatch"
                        .format(data_type))
                #field['value'] = value
                if field_size is None:
                    raise ValueError("Could not determine field size in bits")
                field = CommonMessageFormat.Field(name, data_type, value, 
                                                  field_size, description)
                self.fields.append(field)
            else:
                raise ValueError("Unsupported data_type {}".format(data_type))
        else:
            raise ValueError("Field name must be non-empty string")

    def delete_field(self, name):
        """
        Remove a field from the message.

        :param name: of field (string)

        """
        # success = False
        for i, field in enumerate(self.fields):
            if field.name == name:
                # success = True
                del self.fields[i]
                break

    def encode_idp(self, data_format=FORMAT_BASE64):
        """
        Encodes the message using the specified data format (Text, Hex, base64).

        :param data_format: 2=ASCII-Hex, 3=base64
        :returns: encoded_payload (string) to pass into AT%MGRT

        """
        encoded_payload = ''
        bin_str = ''
        for field in self.fields:
            # name = field.name
            data_type = field.data_type
            value = field.value
            field_size = field.field_size
            bin_field = ''
            if 'int' in data_type and isinstance(value, int):
                if value < 0:
                    inv_bin_field = format(-value, field_size)
                    comp_bin_field = ''
                    i = 0
                    while len(comp_bin_field) < len(inv_bin_field):
                        comp_bin_field += '1' if inv_bin_field[i] == '0' else '0'
                        i += 1
                    bin_field = format(int(comp_bin_field, 2) + 1, field_size)
                else:
                    bin_field = format(value, field_size)
            elif data_type == 'bool' and isinstance(value, bool):
                bin_field = '1' if value else '0'
            elif data_type == 'float' and isinstance(value, float):
                f = '{0:0%db}' % field_size
                bin_field = f.format(int(hex(struct.unpack('!I', struct.pack('!f', value))[0]), 16))
            elif data_type == 'double' and isinstance(value, float):
                f = '{0:0%db}' % field_size
                bin_field = f.format(int(hex(struct.unpack('!Q', struct.pack('!d', value))[0]), 16))
            elif data_type == 'string' and isinstance(value, str):
                bin_field = bin(int(''.join(format(ord(c), '02x') for c in value), 16))[2:]
                if len(bin_field) < field_size:
                    # TODO: be careful on padding strings...this should pad with NULL
                    bin_field += ''.join('0' for pad in range(len(bin_field), field_size))
            else:
                pass
                # TODO: handle other cases
                # raise
            bin_str += bin_field
        payload_pad_bits = len(bin_str) % 8
        while payload_pad_bits > 0:
            bin_str += '0'
            payload_pad_bits -= 1
        hex_str = ''
        index_byte = 0
        while len(hex_str) / 2 < len(bin_str) / 8:
            hex_str += format(int(bin_str[index_byte:index_byte + 8], 2), '02X').upper()
            index_byte += 8
        self.size = len(hex_str) / 2 + 2
        self.payload_b64 = b2a_base64(bytearray.fromhex(hex_str)).strip().decode()
        if data_format == FORMAT_HEX:
            encoded_payload = hex_str
        elif data_format == FORMAT_BASE64:
            encoded_payload = self.payload_b64
        else:
            raise ValueError("Message data_format {} unsupported".format(data_type))
        return encoded_payload

    def get_xml(self):
        # TODO: create Message Definition File
        return 'NOT IMPLEMENTED'


class MessageDefinitions(object):
    """
    TODO: docstring
    """

    class Service(object):
        """
        TODO: docstring
        """
        SIN_LOW = 16
        SIN_HIGH = 255
        def __init__(self, sin, name, description=None, 
                    return_messages=None, forward_messages=None):
            if sin in range(self.SIN_LOW, self.SIN_HIGH+1):
                self.sin = sin
            else:
                raise ValueError("Service must have SIN in range {}..{}"
                                .format(self.SIN_LOW, self.SIN_HIGH))
            if isinstance(name, str) and name != '':
                self.name = name
            else:
                raise ValueError("Service description must be a non-empty string")
            self.description = ''
            self.messages_return = []
            self.messages_forward = []
            if isinstance(return_messages, list):
                for msg in return_messages:
                    self.add_return_message(msg)
            if isinstance(forward_messages, list):
                for msg in forward_messages:
                    self.add_forward_message
        
        def _add_message(self, message, msg_list):
            # TODO: error handling/returns
            if (isinstance(message, CommonMessageFormat) 
                and message.sin == self.sin):
                # Check for conflict
                conflict = False
                for i in range(0, len(msg_list)):
                    if msg_list[i].min == message.min:
                        # TODO: overwrite log warning
                        conflict = True
                        msg_list[i] = message
                        break
                if not conflict:
                    msg_list.append(message)
            else:
                raise TypeError('Message invalid')
        
        def _remove_message(self, min, msg_list):
            for i in range(0, len(msg_list)):
                if msg_list[i].min == min:
                    msg_list.remove(msg_list[i])
                    break

        def add_return_message(self, message):
            self._add_message(message, self.messages_return)
        
        def remove_return_message(self, min):
            self._remove_message(min, self.messages_return)
            
        def add_forward_message(self, message):
            self._add_message(message, self.messages_forward)
        
        def remove_forward_message(self, min):
            self._remove_message(min, self.messages_forward)

        def get_xml(self):
            # TODO: return XML format compliant with Inmarsat MDF
            return 'NOT IMPLEMENTED'

    def __init__(self):
        self.services = []
    
    def add_service(self, sin, name, description):
        conflict = False
        for i in range(0, len(self.services)):
            if self.services[i].sin == sin:
                # TODO: conflict warning
                conflict = True
                break
        if not conflict:
            new_service = MessageDefinitions.Service(sin, name, description)
            self.services.append(new_service)
    
    def get_mdf(self):
        return 'NOT IMPLEMENTED'

if __name__ == '__main__':
    print('Self test run')
    timestamp = 0
    isat_lat_mmin = int(51.525678 * 60 * 60 * 1000)
    isat_lng_mmin = int(-0.086872 * 60 * 60 * 1000)
    payload = CommonMessageFormat(msg_sin=255, 
                                  msg_min=255, 
                                  name='location')
    payload.add_field('timestamp', 'uint_32', timestamp, bits=31)
    payload.add_field('latitude', 'int_32', isat_lat_mmin, bits=24)
    payload.add_field('longitude', 'int_32', isat_lng_mmin, bits=25)
    payload.add_field('altitude', 'int_16', 1000, bits=16)
    payload.add_field('speed', 'int_16', 0, bits=8)
    payload.add_field('heading', 'int_16', 0, bits=9)
    print(payload.encode_idp(data_format=FORMAT_BASE64))
    print(payload.encode_idp(data_format=FORMAT_HEX))
