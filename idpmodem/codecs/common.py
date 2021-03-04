"""Codec functions for IDP Common Message Format supported by Inmarsat MGS.

Also supported on ORBCOMM IGWS1.
"""

from binascii import b2a_base64
from math import log2, ceil
from struct import pack, unpack
from typing import Tuple, Union
from warnings import WarningMessage, warn
import xml.etree.ElementTree as ET
from xml.dom.minidom import parseString

from idpmodem.constants import FORMAT_HEX, FORMAT_B64, SATELLITE_GENERAL_TRACE

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
    'array': 'ArrayField',   # TODO: support for array type
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


def _encode_field_length(length) -> str:
    if length < 128:
        return '0{:07b}'.format(length)
    return '1{:15b}'.format(length)


def _decode_field_length(binstr: str) -> Tuple[int, int]:
    if binstr[0] == '0':
        bit_index = 8
    else:
        bit_index = 16
    length = int(binstr[1:bit_index], 2)
    return (length, bit_index)


def _attribute_equivalence(reference: object,
                           other: object,
                           exclude: "list[str]" = None) -> bool:
    for attr, val in reference.__dict__.items():
        if exclude is not None and attr in exclude:
            continue
        if not hasattr(other, attr) or val != other.__dict__[attr]:
            return False
    return True


def _indent_xml(elem, level=0):
    xmlstr = parseString(ET.tostring(elem)).toprettyxml(indent="  ")
    # i = "\n" + level*"  "
    # j = "\n" + (level-1)*"  "
    # if len(elem):
    #     if not elem.text or not elem.text.strip():
    #         elem.text = i + "  "
    #     if not elem.tail or not elem.tail.strip():
    #         elem.tail = i
    #     for subelem in elem:
    #         _indent_xml(subelem, level+1)
    #     if not elem.tail or not elem.tail.strip():
    #         elem.tail = j
    # else:
    #     if level and (not elem.tail or not elem.tail.strip()):
    #         elem.tail = j
    # return elem
    return xmlstr


class BaseField:
    """The base class for a Field.
    
    Attributes:
        data_type (str): The data type from a supported list.
        name (str): The unique Field name.
        description (str): Optional description.
        optional (bool): Optional indication the field is optional.

    """
    def __init__(self,
                 name: str,
                 data_type: str,
                 description: str = None,
                 optional: bool = False) -> None:
        """Instantiates the base field.
        
        Args:
            name: The field name must be unique within a Message.
            data_type: The data type represented within the field.
            description: (Optional) Description/purpose of the field.
            optional: (Optional) Indicates if the field is mandatory.
            
        """
        if data_type not in DATA_TYPES:
            raise ValueError('Invalid data type {}'.format(data_type))
        if name is None or name.strip() == '':
            raise ValueError('Invalid name must be non-empty')
        self.data_type = data_type
        self.name = name
        self.description = description
        self.optional = optional
    
    def __repr__(self):
        from pprint import pformat
        return pformat(vars(self), indent=4)
    
    def _base_xml(self) -> ET.Element:
        xsi_type = DATA_TYPES[self.data_type]
        xmlfield = ET.Element('Field', attrib={
            '{http://www.w3.org/2001/XMLSchema-instance}type': xsi_type
        })
        name = ET.SubElement(xmlfield, 'Name')
        name.text = self.name
        if self.description:
            description = ET.SubElement(xmlfield, 'Description')
            description.text = str(self.description)
        if self.optional:
            optional = ET.SubElement(xmlfield, 'Optional')
            optional.text = 'true'
        return xmlfield


class Message:
    """The Payload structure for Message Definition Files uploaded to a Mailbox.
    
    Attributes:
        name (str): The message name
        sin (int): The Service Identification Number
        min (int): The Message Identification Number
        fields (list): A list of Fields
        description (str): Optional description
        is_forward (bool): Indicates if the message is mobile-terminated

    """

    def __init__(self,
                 name: str,
                 sin: int,
                 min: int,
                 description: str = None,
                 is_forward: bool = False,
                 fields: "list[Field]" = None):
        """Instantiates a Message.
        
        Args:
            name: The message name should be unique within the xMessages list.
            sin: The Service Identification Number (16..255)
            min: The Message Identification Number (0..255)
            description: (Optional) Description/purpose of the Message.
            is_forward: Indicates if the message is intended to be
                Mobile-Terminated.
            fields: Optional definition of fields during instantiation.

        """
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
        self.fields = fields or Fields()

    @property
    def fields(self):
        return self._fields
    
    @fields.setter
    def fields(self, fields: "list[Field]"):
        if not all(isinstance(field, BaseField) for field in fields):
            raise ValueError('Invalid field found in list')
        self._fields = fields

    @property
    def ota_size(self):
        ota_bits = 2 * 8
        for field in self.fields:
            ota_bits += field.bits + (1 if field.optional else 0)
        return ceil(ota_bits / 8)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def decode(self, databytes: bytes) -> None:
        """Decodes field values from raw data bytes (received over-the-air).
        
        Args:
            databytes: A bytes array (typically from the forward message)

        """
        binary_str = ''.join(format(int(b), '08b') for b in databytes)
        bit_offset = 16   #: Begin after SIN/MIN bytes
        for field in self.fields:
            if field.optional:
                present = binary_str[bit_offset] == '1'
                bit_offset += 1
                if not present:
                    continue
            bit_offset += field.decode(binary_str[bit_offset:])

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
            exclude (list[str]): A list of optional field names to exclude
        
        Returns:
            Dictionary with sin, min, data_format and data to pass into AT%MGRT
                or atcommand function `message_mo_send`

        """
        if data_format not in [FORMAT_B64, FORMAT_HEX]:
            raise ValueError('data_format {} unsupported'.format(data_format))
        #:AT%MGRT uses '{}.{},{},{}'.format(sin, min, data_format, data)
        bin_str = ''
        for field in self.fields:
            if field.optional:
                if exclude is not None and field.name in exclude:
                    present = False
                elif hasattr(field, 'value'):
                    present = field.value is not None
                elif hasattr(field, 'elements'):
                    present = field.elements is not None
                else:
                    raise ValueError('Unknown value of optional')
                bin_str += '1' if present else '0'
                if not present:
                    continue
            bin_str += field.encode()
        for _ in range(0, 8 - len(bin_str) % 8):   #:pad to next byte
            bin_str += '0'
        hex_str = format(int(bin_str, 2), '02X')
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

    def xml(self, indent: bool = False) -> ET.Element:
        """Returns the XML definition for a Message Definition File."""
        xmessage = ET.Element('Message')
        name = ET.SubElement(xmessage, 'Name')
        name.text = self.name
        min = ET.SubElement(xmessage, 'MIN')
        min.text = str(self.min)
        fields = ET.SubElement(xmessage, 'Fields')
        for field in self.fields:
            fields.append(field.xml())
        return xmessage if not indent else _indent_xml(xmessage)


class Service:
    """A data structure holding a set of related Forward and Return Messages.
    
    Attributes:
        name (str): The service name
        sin (int): Service Identification Number or codec service id (16..255)
        description (str): A description of the service (unsupported)
        messages_forward (list): A list of mobile-terminated Message definitions
        messages_return (list): A list of mobile-originated Message definitions

    """
    def __init__(self,
                 name: str,
                 sin: int,
                 description: str = None,
                 messages_forward: "list[Message]" = None,
                 messages_return: "list[Message]" = None) -> None:
        """Instantiates a Service made up of Messages.
        
        Args:
            name: The service name should be unique within a MessageDefinitions
            sin: The Service Identification Number (16..255)
            description: (Optional)
        """
        if not isinstance(name, str) or name == '':
            raise ValueError('Invalid service name {}'.format(name))
        if sin not in range(16, 256):
            raise ValueError('Invalid SIN must be 16..255')
        self.name = name
        self.sin = sin
        if description is not None:
            warn('Service Description not currently supported')
        self.description = None
        self.messages_forward = messages_forward or Messages(self.sin,
                                                             is_forward=True)
        self.messages_return = messages_return or Messages(self.sin,
                                                           is_forward=False)
    
    def xml(self, indent: bool = False) -> ET.Element:
        """Returns the XML structure of the Service for a MDF."""
        if len(self.messages_forward) == 0 and len(self.messages_return) == 0:
            raise ValueError('No messages defined for service {}'.format(
                             self.sin))
        xservice = ET.Element('Service')
        name = ET.SubElement(xservice, 'Name')
        name.text = str(self.name)
        sin = ET.SubElement(xservice, 'SIN')
        sin.text = str(self.sin)
        if self.description:
            desc = ET.SubElement(xservice, 'Description')
            desc.text = str(self.description)
        if len(self.messages_forward) > 0:
            forward_messages = ET.SubElement(xservice, 'ForwardMessages')
            for m in self.messages_forward:
                forward_messages.append(m.xml())
        if len(self.messages_return) > 0:
            return_messages = ET.SubElement(xservice, 'ReturnMessages')
            for m in self.messages_return:
                return_messages.append(m.xml())
        return xservice if not indent else _indent_xml(xservice)


class ObjectList(list):
    """Base class for a specific object type list.
    
    Used for Fields, Messages, Services.

    Attributes:
        list_type: The object type the list is comprised of.

    """
    SUPPORTED_TYPES = [
        BaseField,
        # Field,
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


class Fields(ObjectList):
    """The list of Fields defining a Message or ArrayElement."""
    def __init__(self, fields: "list[BaseField]" = None):
        super().__init__(list_type=BaseField)
        if fields is not None:
            for field in fields:
                self.add(field)
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Fields):
            return NotImplemented
        if len(self) != len(other):
            return False
        for field in self:
            if field != other[field.name]:
                return False
        return True


class Messages(ObjectList):
    """The list of Messages (Forward or Return) within a Service."""
    def __init__(self, sin: int, is_forward: bool):
        super().__init__(list_type=Message)
        self.sin = sin
        self.is_forward = is_forward
    
    def add(self, message: Message) -> bool:
        """Add a message to the list if it matches the parent SIN.

        Overrides the base class add method.

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


class Services(ObjectList):
    """The list of Service(s) within a MessageDefinitions."""
    def __init__(self):
        super().__init__(list_type=Service)
    
    def add(self, service: Service) -> None:
        """Adds a Service to the list of Services."""
        if not isinstance(service, Service):
            raise ValueError('{} is not a valid Service'.format(service))
        if service.name in self:
            raise ValueError('Duplicate Service {}'.format(service.name))
        for existing_service in self:
            if existing_service.sin == service.sin:
                raise ValueError('Duplicate SIN {}'.format(service.sin))
        self.append(service)


class BooleanField(BaseField):
    """A Boolean field."""
    def __init__(self,
                 name: str,
                 description: str = None,
                 optional: bool = False,
                 default: bool = False,
                 value: bool = None) -> None:
        super().__init__(name=name,
                         data_type='bool',
                         description=description,
                         optional=optional)
        """Instantiates a BooleanField.
        
        Args:
            name: The field name must be unique within a Message.
            description: An optional description/purpose for the field.
            optional: Indicates if the field is optional in the Message.
            default: A default value for the boolean.
            value: Optional value to set during initialization.

        """
        self.default = default
        self.value = value if value is not None else default
    
    @property
    def default(self):
        return self._default

    @default.setter
    def default(self, v: bool):
        if v is not None and not isinstance(v, bool):
            raise ValueError('Invalid boolean value {}'.format(v))
        self._default = v

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v: bool):
        if v is not None and not isinstance(v, bool):
            raise ValueError('Invalid boolean value {}'.format(v))
        self._value = v

    @property
    def bits(self):
        return 1
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BooleanField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        """Returns the binary string of the field value.
        """
        if self.value is None and not self.optional:
            raise ValueError('No value assigned to field')
        return '1' if self.value else '0'

    def decode(self, binary_str: str) -> int:
        """Decodes the field value from the first bit of a binary string.
        
        Returns:
            index increment of next binary_str position for continued parsing.

        """
        self.value = True if binary_str[0] == '1' else False
        return 1

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = 'true'
        return xmlfield


class EnumField(BaseField):
    """An enumerated field sends an index over-the-air representing a string."""
    def __init__(self,
                 name: str,
                 items: "list[str]",
                 size: int,
                 description: str = None,
                 optional: bool = False,
                 default: int = None,
                 value: int = None) -> None:
        """Instantiates a EnumField.
        
        Args:
            name: The field name must be unique within a Message.
            items: A list of strings (indexed from 0).
            size: The number of *bits* used to encode the index over-the-air.
            description: An optional description/purpose for the field.
            optional: Indicates if the field is optional in the Message.
            default: A default value for the enum.
            value: Optional value to set during initialization.

        """
        super().__init__(name=name,
                         data_type='enum',
                         description=description,
                         optional=optional)
        if items is None or not all(isinstance(item, str) for item in items):
            raise ValueError('Items must all be strings')
        if not isinstance(size, int) or size < 1:
            raise ValueError('Size must be greater than 0 bits')
        self.items = items
        self.size = size
        self.default = default
        self.value = value if value is not None else self.default
    
    def _validate_enum(self, v: Union[int, str]) -> Union[int, None]:
        if v is not None:
            if isinstance(v, str):
                if v not in self.items:
                    raise ValueError('Invalid value {}'.format(v))
                for index, item in enumerate(self.items):
                    if item == v:
                        return index
            elif isinstance(v, int):
                if v < 0 or v >= len(self.items):
                    raise ValueError('Invalid enum index {}'.format(v))
            else:
                raise ValueError('Invalid value {}'.format(v))
        return v

    @property
    def items(self):
        return self._items
    
    @items.setter
    def items(self, l: list):
        if not isinstance(l, list) or not all(isinstance(x, str) for x in l):
            raise ValueError('Items must be a list of strings')
        self._items = l

    @property
    def default(self):
        if self._default is None:
            return None
        return self.items[self._default]
    
    @default.setter
    def default(self, v: Union[int, str]):
        self._default = self._validate_enum(v)

    @property
    def value(self):
        if self._value is None:
            return None
        return self.items[self._value]
    
    @value.setter
    def value(self, v: Union[int, str]):
        self._value = self._validate_enum(v)

    @property
    def size(self):
        return self._size
    
    @size.setter
    def size(self, v: int):
        if not isinstance(v, int) or v < 1:
            raise ValueError('Size must be greater than zero')
        minimum_bits = _get_optimal_bits((0, len(self.items)))
        if v < minimum_bits:
            raise ValueError('Size must be at least {} to support item count'
                             .format(minimum_bits))
        self._size = v

    @property
    def bits(self):
        return self.size
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EnumField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        if self.value is None:
            raise ValueError('No value configured in EnumField {}'.format(
                             self.name))
        _format = '0{}b'.format(self.bits)
        binstr = format(self.items.index(self.value), _format)
        return binstr

    def decode(self, binary_str: str) -> int:
        self.value = binary_str[:self.bits]
        return self.bits

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        items = ET.SubElement(xmlfield, 'Items')
        for string in self.items:
            item = ET.SubElement(items, 'string')
            item.text = str(string)
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = str(self.default)
        return xmlfield


class UnsignedIntField(BaseField):
    """An unsigned integer value using a defined number of bits over-the-air."""
    def __init__(self,
                 name: str,
                 size: int,
                 data_type: str = 'uint_16',
                 description: str = None,
                 optional: bool = False,
                 default: int = None,
                 value: int = None) -> None:
        """Instantiates a UnsignedIntField.
        
        Args:
            name: The field name must be unique within a Message.
            size: The number of *bits* used to encode the integer over-the-air
                (maximum 32).
            data_type: The integer type represented (for decoding).
            description: An optional description/purpose for the string.
            optional: Indicates if the string is optional in the Message.
            default: A default value for the string.
            value: Optional value to set during initialization.

        """
        if data_type not in ['uint_8', 'uint_16', 'uint_32']:
            raise ValueError('Invalid unsignedint type {}'.format(data_type))
        super().__init__(name=name,
                         data_type=data_type,
                         description=description,
                         optional=optional)
        self.size = size
        self.default = default
        self.value = value if value is not None else default
    
    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value: int):
        if not isinstance(value, int) or value < 1:
            raise ValueError('Size must be greater than 0 bits')
        self._size = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v: int):
        clip = False
        if v is not None:
            if not isinstance(v, int) or v < 0:
                raise ValueError('Unsignedint must be non-negative')
            if v > 2**self.size - 1:
                self._value = 2**self.size - 1
                warn('Clipping unsignedint at max value {}'.format(self._value))
                clip = True
        if not clip:
            self._value = v
    
    @property
    def default(self):
        return self._default
    
    @default.setter
    def default(self, v: int):
        if v is not None:
            if v > 2**self.size - 1 or v < 0:
                raise ValueError('Invalid unsignedint default {}'.format(v))
        self._default = v
    
    @property
    def bits(self):
        return self.size
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UnsignedIntField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        if self.value is None:
            raise ValueError('No value defined in UnsignedIntField {}'.format(
                             self.name))
        _format = '0{}b'.format(self.bits)
        return format(self.value, _format)

    def decode(self, binary_str: str) -> int:
        self.value = int(binary_str[:self.bits], 2)
        return self.bits

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = str(self.default)
        return xmlfield


class SignedIntField(BaseField):
    """A signed integer value using a defined number of bits over-the-air."""
    def __init__(self,
                 name: str,
                 size: int,
                 data_type: str = 'int_16',
                 description: str = None,
                 optional: bool = False,
                 default: int = None,
                 value: int = None) -> None:
        """Instantiates a SignedIntField.
        
        Args:
            name: The field name must be unique within a Message.
            size: The number of *bits* used to encode the integer over-the-air
                (maximum 32).
            data_type: The integer type represented (for decoding).
            description: An optional description/purpose for the string.
            optional: Indicates if the string is optional in the Message.
            default: A default value for the string.
            value: Optional value to set during initialization.

        """
        if data_type not in ['int_8', 'int_16', 'int_32']:
            raise ValueError('Invalid unsignedint type {}'.format(data_type))
        super().__init__(name=name,
                         data_type=data_type,
                         description=description,
                         optional=optional)
        self.size = size
        self.default = default
        self.value = value if value is not None else default
    
    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value: int):
        if not isinstance(value, int) or value < 1:
            raise ValueError('Size must be greater than 0 bits')
        self._size = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v: int):
        clip = False
        if v is not None:
            if not isinstance(v, int):
                raise ValueError('Unsignedint must be non-negative')
            if v > (2**self.size / 2) - 1:
                self._value = int(2**self.size / 2) - 1
                warn('Clipping signedint at max value {}'.format(self._value))
                clip = True
            if v < -(2**self.size / 2):
                self._value = -1 * int(2**self.size / 2)
                warn('Clipping signedint at min value {}'.format(self._value))
                clip = True
        if not clip:
            self._value = v
    
    @property
    def default(self):
        return self._default
    
    @default.setter
    def default(self, v: int):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError('Invalid signed integer {}'.format(v))
            if v > (2**self.size / 2) - 1 or v < -(2**self.size / 2):
                raise ValueError('Invalid default {}'.format(v))
        self._default = v
    
    @property
    def bits(self):
        return self.size
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SignedIntField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        if self.value is None:
            raise ValueError('No value defined in UnsignedIntField {}'.format(
                             self.name))
        _format = '0{}b'.format(self.bits)
        if self.value < 0:
            invertedbin = format(self.value * -1, _format)
            twocomplementbin = ''
            i = 0
            while len(twocomplementbin) < len(invertedbin):
                twocomplementbin += '1' if invertedbin[i] == '0' else '0'
                i += 1
            binstr = format(int(twocomplementbin, 2) + 1, _format)
        else:
            binstr = format(self.value, _format)
        return binstr

    def decode(self, binary_str: str) -> int:
        value = int(binary_str[:self.bits], 2)
        if (value & (1 << (self.bits - 1))) != 0:   #:sign bit set e.g. 8bit: 128-255
            value = value - (1 << self.bits)        #:compute negative value
        self.value = value
        return self.bits

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = str(self.default)
        return xmlfield


class StringField(BaseField):
    """A character string sent over-the-air."""
    def __init__(self,
                 name: str,
                 size: int,
                 description: str = None,
                 optional: bool = False,
                 fixed: bool = False,
                 default: str = None,
                 value: str = None) -> None:
        """Instantiates a StringField.
        
        Args:
            name: The field name must be unique within a Message.
            size: The maximum number of characters in the string.
            description: An optional description/purpose for the string.
            optional: Indicates if the string is optional in the Message.
            fixed: Indicates if the string is always fixed length `size`.
            default: A default value for the string.
            value: Optional value to set during initialization.

        """
        super().__init__(name=name,
                         data_type='string',
                         description=description,
                         optional=optional)
        self.size = size
        self.fixed = fixed
        self.default = default
        self.value = value if value is not None else default
    
    def _validate_string(self, s: str):
        if s is not None:
            if not isinstance(s, str):
                raise ValueError('Invalid string {}'.format(s))
            if len(s) > self.size:
                warn('Clipping string at max {} characters'.format(self.size))
                return s[:self.size]
        return s
                
    @property
    def size(self):
        return self._size
    
    @size.setter
    def size(self, value: int):
        if not isinstance(value, int) or value < 1:
            raise ValueError('Size must be greater than 0 characters')
        self._size = value
    
    @property
    def default(self) -> str:
        return self._default
    
    @default.setter
    def default(self, v: str):
        self._default = self._validate_string(v)

    @property
    def value(self) -> str:
        return self._value
    
    @value.setter
    def value(self, v: str):
        self._value = self._validate_string(v)

    @property
    def bits(self):
        if self.fixed or self.value is None:
            return self.size * 8
        return len(self.value) * 8
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StringField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        """Returns a binary string for processing as part of a Message.
        
        Typically this would be for use in a Return Message to submit
        to the modem.

        """
        binstr = ''.join(format(ord(c), '08b') for c in self.value)
        if self.fixed:
            binstr += ''.join('0' for bit in range(len(binstr), self.bits))
        else:
            binstr = _encode_field_length(len(self.value)) + binstr
        return binstr

    def decode(self, binary_str: str) -> str:
        """Returns a string from a binary string derived from a Message.
        
        Typically this would be to parse from a Forward Message retrieved from
        the modem.

        """
        if self.fixed:
            length = self.size
            bit_index = 0
        else:
            (length, bit_index) = _decode_field_length(binary_str)
        n = int(binary_str[bit_index:bit_index + length * 8], 2)
        char_bytes = n.to_bytes((n.bit_length() + 7) // 8, 'big')
        self.value = char_bytes.decode('utf-8', 'surrogatepass') or '\0'
        return bit_index + length * 8

    def xml(self) -> ET.Element:
        """Returns the message definition XML representation of the StringField.
        """
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        if self.fixed:
            fixed = ET.SubElement(xmlfield, 'Fixed')
            fixed.text = 'true'
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = str(self.default)
        return xmlfield


class DataField(BaseField):
    """A data field of raw bytes sent over-the-air.
    
    Can also be used to hold floating point, double-precision or large integers.

    """
    supported_data_types = ['data', 'float', 'double']
    def __init__(self,
                 name: str,
                 size: int = None,
                 data_type: str = 'data',
                 description: str = None,
                 optional: bool = False,
                 fixed: bool = False,
                 default: str = None,
                 value: bytes = None) -> None:
        """Instantiates a EnumField.
        
        Args:
            name: The field name must be unique within a Message.
            size: The maximum number of bytes to send over-the-air.
            data_type: The data type represented within the bytes.
            description: An optional description/purpose for the field.
            optional: Indicates if the field is optional in the Message.
            fixed: Indicates if the data bytes are a fixed `size`.
            default: A default value for the enum.
            value: Optional value to set during initialization.

        """
        if data_type is None or data_type not in self.supported_data_types:
            raise ValueError('Invalid data type {}'.format(data_type))
        super().__init__(name=name,
                         data_type=data_type,
                         description=description,
                         optional=optional)
        self.fixed = fixed
        self.size = size
        self.default = default
        self.value = value if value is not None else default
    
    @property
    def size(self):
        return self._size
    
    @size.setter
    def size(self, value: int):
        if not isinstance(value, int) or value < 1:
            raise ValueError('Size must be greater than 0 bytes')
        if self.data_type == 'float':
            if value != 4:
                warn('Adjusting float size to 4 bytes fixed')
            self._size = 4
            self.fixed = True
        elif self.data_type == 'double':
            if value != 8:
                warn('Adjusting float size to 8 bytes fixed')
            self._size = 4
            self.fixed = True
        else:
            self._size = value
    
    @property
    def value(self):
        if self.data_type == 'float':
            return unpack('!f', self._value)
        if self.data_type == 'double':
            return unpack('!d', self._value)
        return self._value

    @value.setter
    def value(self, v: Union[bytes, float]):
        if v is not None:
            if self.data_type in ['float', 'double']:
                if not isinstance(v, float):
                    raise ValueError('Invalid {} value {}'.format(
                                     self.data_type, v))
                _format = '!f' if self.data_type == 'float' else '!d'
                v = pack(_format, v)
            elif not isinstance(v, bytes):
                raise ValueError('Invalid bytes {}'.format(v))
        self._value = v

    @property
    def bits(self):
        if self.fixed or self.value is None:
            return self.size * 8
        return len(self.value) * 8
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DataField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def encode(self) -> str:
        """Returns the DataField as a binary string."""
        if self.value is None and not self.optional:
            raise ValueError('No value defined for DataField {}'.format(
                             self.name))
        binstr = ''
        binstr = ''.join(format(b, '08b') for b in self._value)
        if self.fixed:   #:pad to fixed length
            binstr += ''.join('0' for bit in range(len(binstr), self.bits))
        else:
            binstr = _encode_field_length(len(self._value)) + binstr
        return binstr

    def decode(self, binary_str: str) -> int:
        """Decodes the DataField from the remaining unparsed payload bits.
        
        Args:
            binary_str (str): The remaining bitstring from the payload, from
                the start of the DataField offset.

        Returns:
            bit_index (int): The bit_index of the binary_str after the end
                of the DataField.
        """
        if self.fixed:
            binary = binary_str[:self.bits]
        else:
            (length, bit_index) = _decode_field_length(binary_str)
            binary = binary_str[bit_index:length * 8 + bit_index]
        self._value = int(binary, 2).to_bytes(int(self.bits / 8), 'big')
        return self.bits

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        if self.default:
            default = ET.SubElement(xmlfield, 'Default')
            default.text = str(self.default)
        return xmlfield


class ArrayField(BaseField):
    """An Array Field provides a list where each element is a set of Fields.
    
    Attributes:
        name (str): The name of the field instance.
        size (int): The maximum number of elements allowed.
        fields (Fields): A list of Field types comprising each ArrayElement
        description (str): An optional description of the array/use.
        optional (bool): Indicates if the array is optional in the Message
        fixed (bool): Indicates if the array is always the fixed `size`
        elements (list): The enumerated list of ArrayElements

    """
    def __init__(self,
                 name: str,
                 size: int,
                 fields: Fields,
                 description: str = None,
                 optional: bool = False,
                 fixed: bool = False,
                 elements: "list[Fields]" = None) -> None:
        """Initializes an ArrayField instance.
        
        Args:
            name: The unique field name within the Message.
            size: The maximum number of elements allowed.
            fields: The list of Field types comprising each element.
            description: An optional description/purpose of the array.
            optional: Indicates if the array is optional in the Message.
            fixed: Indicates if the array is always the fixed `size`.
            elements: Option to populate elements of Fields during instantiation.

        """
        super().__init__(name=name,
                         data_type='array',
                         description=description,
                         optional=optional)
        self.size = size
        self.fixed = fixed
        self.fields = fields
        self.elements = elements or []
    
    @property
    def size(self):
        return self._size
    
    @size.setter
    def size(self, value: int):
        if not isinstance(value, int) or value < 1:
            raise ValueError('Size must be greater than 0 fields')
        self._size = value
    
    @property
    def fields(self):
        return self._fields

    @fields.setter
    def fields(self, fields: Fields):
        if not isinstance(fields, Fields):
            raise ValueError('Invalid Fields definition for ArrayField')
        self._fields = fields

    @property
    def elements(self):
        return self._elements
    
    @elements.setter
    def elements(self, elements: list):
        if elements is None or not hasattr(self, '_elements'):
            self._elements = []
        if not isinstance(elements, list):
            raise ValueError('Elements must be a list of grouped Fields')
        for fields in elements:
            for index, field in enumerate(fields):
                if (field.name != self.fields[index].name):
                    raise ValueError('fields[{}].name expected {} got {}'
                                     .format(index,
                                             self.fields[index].name,
                                             field.name))
                if (field.data_type != self.fields[index].data_type):
                    raise ValueError('fields[{}].data_type expected {} got {}'
                                     .format(index,
                                             self.fields[index].data_type,
                                             field.data_type))
                #TODO: validate non-optional fields have value/elements
                if field.value is None and not field.optional:
                    raise ValueError('fields[{}].value missing'.format(index))
                try:
                    self._elements[index] = fields
                except IndexError:
                    self._elements.append(fields)

    @property
    def bits(self):
        bits = 0
        for field in self.fields:
            bits += field.bits
        return bits
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ArrayField):
            return NotImplemented
        return _attribute_equivalence(self, other)

    def new_element(self):
        new_index = len(self._elements)
        self._elements.append(Fields(self.fields))
        return self.elements[new_index]

    def encode(self) -> str:
        """Returns the ArrayField as a binary string."""
        binstr = ''
        for element in self.elements:
            for field in element:
                binstr += field.encode()
        if not self.fixed:
            binstr = _encode_field_length(len(self.elements)) + binstr
        return binstr

    def decode(self, binary_str: str) -> int:
        """Decodes the ArrayField from the remaining unparsed binary string.
        
        Args:
            binary_str (str): The binary string beginning from the ArrayField
                bit offset.

        Returns:
            bit_index (int): The bit index of the binary_str after the
                ArrayField.

        """
        if self.fixed:
            length = self.size
            bit_index = 0
        else:
            (length, bit_index) = _decode_field_length(binary_str)
        for index in range(0, length):
            fields = Fields(self.fields)
            for field in fields:
                if field.optional:
                    if binary_str[bit_index] == '0':
                        bit_index += 1
                        continue
                    bit_index += 1
                bit_index += field.decode(binary_str[bit_index:])
            try:
                self._elements[index] = fields
            except IndexError:
                self._elements.append(fields)
        return bit_index

    def xml(self) -> ET.Element:
        xmlfield = self._base_xml()
        size = ET.SubElement(xmlfield, 'Size')
        size.text = str(self.size)
        if self.fixed:
            default = ET.SubElement(xmlfield, 'Fixed')
            default.text = 'true'
        fields = ET.SubElement(xmlfield, 'Fields')
        for field in self.fields:
            fields.append(field.xml())
        return xmlfield


class MessageDefinitions:
    """A set of Message Definitions grouped into Services.

    Attributes:
        services: The list of Services with Messages defined.
    
    """
    def __init__(self):
        self.services = Services()
    
    def xml(self, indent: bool = False) -> ET.Element:
        xmsgdef = ET.Element('MessageDefinition',
                             attrib={'xmlns:xsd': XML_NAMESPACE['xsd']})
        services = ET.SubElement(xmsgdef, 'Services')
        for service in self.services:
            services.append(service.xml())
        return xmsgdef if not indent else _indent_xml(xmsgdef)
    
    def mdf_export(self, filename: str, pretty: bool = False):
        tree = ET.ElementTree(self.xml())
        root = tree.getroot()
        if pretty:
            from xml.dom.minidom import parseString
            xmlstr = parseString(ET.tostring(root)).toprettyxml(indent="  ")
            with open(filename, 'w') as f:
                f.write(xmlstr)
        else:
            with open(filename, 'wb') as f:
                tree.write(f, encoding='utf-8', xml_declaration=True)
