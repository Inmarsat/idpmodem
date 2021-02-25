"""Codec functions for IDP Common Message Format supported by Inmarsat MGS."""

# from base64 import decode
from binascii import b2a_base64
from math import log2, ceil
from struct import pack, unpack
from typing import Union
from warnings import WarningMessage, warn
import xml.etree.ElementTree as ET

from idpmodem.constants import FORMAT_HEX, FORMAT_B64

__version__ = '2.0.0'


DATA_TYPES = {
    'bool': 'BooleanField',
    'int_8': 'SignedIntField',
    'uint_8': 'UnsignedIntField',
    'int_16': 'SignedIntField',
    'uint_16': 'UnsignedIntField',
    'int_32': 'SignedIntField',
    'uint_32': 'UnsignedIntField',
    'int_64': 'SignedIntField',
    'uint_64': 'UnsignedIntField',
    'float': 'DataField',
    'double': 'DataField',
    'string': 'StringField',
    'data': 'DataField',
    'enum': 'EnumField',
    # 'array': 'ArrayField',   # TODO: support for array type
}
XML_NAMESPACE = {
    'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
    'xsd': 'http://www.w3.org/2001/XMLSchema'
}
SIN_RANGE = (16, 255)

for ns in XML_NAMESPACE:
    ET.register_namespace(ns, XML_NAMESPACE[ns])


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


def _bits_to_string(bits: str) -> str:
    stringbytes = int(bits, 2).to_bytes(int(len(bits) / 8), 'big')
    return stringbytes.decode() or '\0'


class Field:
    """A data field within a Common Message Format message.
    
    Attributes:
        name (str): The field name
        data_type (str): A supported data type for encoding/decoding
        value (any): The value which is type dependent
        value_range (tuple): The min, max of allowed values or enum strings
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
                 fixed: bool = None,
                 default: any = None,
                 size: int = None,
                 fields: list = None):
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
        self.default = default
        self.size = None
        self.value = value
        self.fields = None
        if fixed is not None or optional:
            raise NotImplementedError('optional and fixed currently unsupported')
        if not data_type in DATA_TYPES:
            raise ValueError("Unsupported data_type {}".format(data_type))
        self.data_type = data_type
        if (data_type == 'bool'): # and isinstance(value, bool)
            if not isinstance(default, bool):
                self.default = False
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
            self.size = int(self.bits / 8)
        elif (data_type == 'enum'):
            if (not isinstance(value_range, tuple) or
                not all(isinstance(i, str) for i in value_range)):
                raise ValueError('value_range must be tuple of strings')
            self.bits = _get_optimal_bits((0, len(value_range) - 1))
            if isinstance(bits, int):
                self.bits = max(bits, self.bits)
        else:
            raise ValueError("Unsupported data_type {} or type mismatch"
                .format(data_type))
        # optimize bits
        self.value_range = value_range
        if not self.bits:
            if bits:
                self.bits = bits
            elif value_range is not None:
                self.bits = _get_optimal_bits(value_range)
        self._format = '0{}b'.format(self.bits)
    
    def __repr__(self):
        from pprint import pformat
        return pformat(vars(self), indent=4)
    
    def get_xml(self) -> ET.Element:
        """Returns the XML definition of the field."""
        xsi_type = DATA_TYPES[self.data_type]
        field = ET.Element('Field', attrib={
                '{http://www.w3.org/2001/XMLSchema-instance}type': xsi_type})
        # field.set('xsi:type', xsi_type)
        name = ET.SubElement(field, 'Name')
        name.text = self.name
        if self.description:
            description = ET.SubElement(field, 'Description')
            description.text = str(self.description)
        if self.optional:
            optional = ET.SubElement(field, 'Optional')
            optional.text = str(self.optional)
        if self.default:
            default = ET.SubElement(field, 'Default')
            default.text = str(self.default)
            if self.data_type == 'bool':
                default.text = default.text.lower()
        if self.fixed:
            fixed = ET.SubElement(field, 'Fixed')
            fixed.text = str(self.fixed)
        if xsi_type == 'EnumField':
            size_bits = ET.SubElement(field, 'Size')
            size_bits.text = str(self.bits)
            items = ET.SubElement(field, 'Items')
            for string in self.value_range:
                item = ET.SubElement(items, 'string')
                item.text = str(string)
        elif 'IntField' in xsi_type:
            size_bits = ET.SubElement(field, 'Size')
            size_bits.text = str(self.bits)
        elif xsi_type in ['StringField', 'DataField']:
            size_bytes = ET.SubElement(field, 'Size')
            size_bytes.text = str(int(self.bits / 8))
        elif xsi_type == 'ArrayField':
            size = ET.SubElement(field, 'Size')
            size.text = str(self.size)
        return field

    
class Message:
    """The Payload structure for Message Definition Files uploaded to a Mailbox.
    
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
        if not isinstance(sin, int) or sin not in range(16, 256):
            raise ValueError('Invalid SIN {} must be in range 16..255'.format(
                             sin))
        if not isinstance(min, int) or min not in range (0, 256):
            raise ValueError('Invalid MIN {} must be in range 0..255'.format(
                             min))
        self.name = name
        self.description = description
        self.is_forward = is_forward
        self.sin = sin
        self.min = min
        self.fields = Fields()

    @property
    def ota_size(self):
        ota_bits = 2 * 8
        for field in self.fields:
            ota_bits += field.bits + (1 if field.optional else 0)
        return ceil(ota_bits / 8)

    def decode(self, databytes: bytes) -> None:
        """Decodes field values from raw data bytes (received over-the-air).
        
        Args:
            databytes: A bytes array (typically from the forward message)

        """
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
                field.value = _bits_to_string(binary)
            elif field.data_type == 'data':
                field.value = int(binary, 2).to_bytes(int(field.bits / 8), 'big')
            elif field.data_type == 'enum':
                field.value = field.value_range[int(binary, 2)]
            else:
                raise NotImplementedError('data_type {} not supported'.format(
                                          field.data_type))
            bit_offset += field.bits

    def derive(self, databytes: bytes) -> None:
        """Derives field values from raw data bytes (received over-the-air).
        
        Deprecated/replaced by `decode`.

        Args:
            databytes: A bytes array (typically from the forward message)

        """
        self.decode(databytes)

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
            elif data_type == 'enum':
                f = '0{}b'.format(bits)
                bin_field = format(value, '{}'.format(f))
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
        xmessage = ET.Element('Message')
        name = ET.SubElement(xmessage, 'Name')
        name.text = self.name
        min = ET.SubElement(xmessage, 'MIN')
        min.text = str(self.min)
        fields = ET.SubElement(xmessage, 'Fields')
        for field in self.fields:
            fields.append(field.get_xml())
        return xmessage


class CommonMessageFormat(Message):
    """Included for backward compatibility, replaced by Message."""
    pass


class Service:
    def __init__(self, name: str, sin: int, description: str = None) -> None:
        if not isinstance(name, str) or name == '':
            raise ValueError('Invalid service name {}'.format(name))
        if sin not in range(16, 256):
            raise ValueError('Invalid SIN must be 16..255')
        self.name = name
        self.sin = sin
        if description is not None:
            raise ValueError('Service Description not currently supported')
        self.description = description
        self.messages_forward = Messages(self.sin, is_forward=True)
        self.messages_return = Messages(self.sin, is_forward=False)
    
    def get_xml(self):
        if len(self.messages_forward) == 0 and len(self.messages_return) == 0:
            raise ValueError('No messages defined for service {}'.format(
                             self.sin))
        service = ET.Element('Service')
        name = ET.SubElement(service, 'Name')
        name.text = str(self.name)
        sin = ET.SubElement(service, 'SIN')
        sin.text = str(self.sin)
        if self.description:
            desc = ET.SubElement(service, 'Description')
            desc.text = str(self.description)
        if len(self.messages_forward) > 0:
            forward_messages = ET.SubElement(service, 'ForwardMessages')
            for m in self.messages_forward:
                forward_messages.append(m.get_xml())
        if len(self.messages_return) > 0:
            return_messages = ET.SubElement(service, 'ReturnMessages')
            for m in self.messages_return:
                return_messages.append(m.get_xml())
        return service


class ObjectList(list):
    """Base class for a specific object type list.
    
    Used for Fields, Messages, Services.

    Attributes:
        list_type: The object type the list is comprised of.

    """
    SUPPORTED_TYPES = [
        Field,
        Message,
        Service,
    ]

    def __init__(self, list_type: object):
        if list_type not in self.SUPPORTED_TYPES:
            raise ValueError('Unsupported object type {}'.format(list_type))
        super().__init__()
        self.list_type = list_type

    def add(self, obj: object) -> bool:
        """Add an object to the list.

        Args:
            obj (object): A valid object according to the list_type
        
        Raises:
            ValueError if there is a duplicate or invalid name,
                invalid value_range or unsupported data_type

        """
        if not isinstance(obj, self.list_type):
            raise ValueError('Invalid {} definition'.format(self.list_type))
        for o in self:
            if o.name == obj.name:
                raise ValueError('Duplicate {} name {} found'.format(
                                 self.list_type, obj.name))
        self.append(obj)
        return True

    def __getitem__(self, n: Union[str, int]) -> object:
        """Retrieves an object by name or index.
        
        Args:
            n: The object name or list index
        
        Returns:
            object

        """
        if isinstance(n, str):
            for o in self:
                if o.name == n:
                    return o
            raise ValueError('{} name {} not found'.format(self.list_type, n))
        return super().__getitem__(n)

    def delete(self, name: str) -> bool:
        """Delete an object from the list by name.
        
        Args:
            name: The name of the object.

        Returns:
            boolean: success

        """
        for o in self:
            if o.name == name:
                self.remove(o)
                return True
        return False


class Fields(list):
    def __init__(self):
        super().__init__()
    
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


class Messages(ObjectList):
    def __init__(self, sin: int, is_forward: bool):
        super().__init__(list_type=Message)
        self.sin = sin
        self.is_forward = is_forward
    '''
    def add(self, message: Message) -> bool:
        """Add a message to the list.

        Args:
            message (object): A valid Message
        
        Raises:
            ValueError if there is a duplicate or invalid name,
                invalid value_range or unsupported data_type

        """
        if not isinstance(message, Message):
            raise ValueError('Invalid message definition')
        if message.sin != self.sin:
            raise ValueError('Message SIN {} does not match service {}'.format(
                             message.sin, self.sin))
        for m in self:
            if m.name == message.name:
                raise ValueError('Duplicate message name {} found'.format(
                                 message.name))
            if m.min == message.min:
                raise ValueError('Duplicate message MIN {} found'.format(
                                 message.min))
        self.append(message)
        return True

    def __getitem__(self, n: Union[str, int]) -> Message:
        if isinstance(n, str):
            for message in self:
                if message.name == n:
                    return message
            raise ValueError('Message name {} not found'.format(n))
        return super().__getitem__(n)

    def delete(self, name: str):
        for m in self:
            if m.name == name:
                self.remove(m)
                return True
        return False
    '''


class Services(ObjectList):
    def __init__(self):
        super().__init__(list_type=Service)
    '''
    def add(self, service: Service) -> bool:
        """Add a service to the list.

        Args:
            service (object): A valid Service
        
        Raises:
            ValueError if there is a duplicate or invalid name,
                invalid value_range or unsupported data_type

        """
        if not isinstance(service, Service):
            raise ValueError('Invalid service definition')
        for s in self:
            if s.name == service.name:
                raise ValueError('Duplicate service name {} found'.format(
                                 service.name))
            if s.sin == service.sin:
                raise ValueError('Duplicate SIN {} found'.format(
                                 service.sin))
        self.append(service)
        return True

    def __getitem__(self, n: Union[str, int]) -> Message:
        if isinstance(n, str):
            for service in self:
                if service.name == n:
                    return service
            raise ValueError('Service name {} not found'.format(n))
        return super().__getitem__(n)

    def delete(self, name: str):
        for s in self:
            if s.name == name:
                self.remove(s)
                return True
        return False
    '''


class MessageDefinitions:
    """A set of Message Definitions grouped into Services.

    Attributes:
        services: The list of Services with Messages defined.
    
    """
    def __init__(self):
        self.services = Services()
    
    def get_xml(self) -> ET.Element:
        msg_def = ET.Element('MessageDefinition',
                             attrib={'xmlns:xsd': XML_NAMESPACE['xsd']})
        services = ET.SubElement(msg_def, 'Services')
        for service in self.services:
            services.append(service.get_xml())
        return msg_def
    
    def mdf_export(self, filename: str, pretty: bool = False):
        tree = ET.ElementTree(self.get_xml())
        root = tree.getroot()
        if pretty:
            from xml.dom.minidom import parseString
            xmlstr = parseString(ET.tostring(root)).toprettyxml(indent="  ")
            with open(filename, 'w') as f:
                f.write(xmlstr)
        else:
            with open(filename, 'wb') as f:
                tree.write(f, encoding='utf-8', xml_declaration=True)
