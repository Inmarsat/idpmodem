import pytest
from copy import deepcopy
import xml.etree.ElementTree as ET

from idpmodem.codecs import common
from idpmodem.constants import FORMAT_HEX


@pytest.fixture
def bool_field():
    """Returns a BooleanField."""
    def _bool_field(name: str = 'boolFixture',
                    optional: bool = False,
                    default: bool = False,
                    value: bool = None):
        return common.BooleanField(name=name,
                                   optional=optional,
                                   default=default,
                                   value=value,
                                   description='A boolean test field.')
    return _bool_field

@pytest.fixture
def data_field():
    """Returns a DataField with no value."""
    def _data_field(size: int = 1,
                    data_type: str = 'data',
                    optional: bool = False,
                    fixed: bool = False,
                    default: bytes = None,
                    value: bytes = None):
        return common.DataField(name='dataFixture',
                                size=size,
                                data_type=data_type,
                                optional=optional,
                                fixed=fixed,
                                default=default,
                                value=value,
                                description='A data test field.')
    return _data_field

@pytest.fixture
def enum_field():
    """Returns a EnumField with no value."""
    fixture_items = ['item1', 'item2', 'item3']
    def _enum_field(name: str = 'enumFixture',
                    items: list = fixture_items,
                    size: int = 2,
                    description: str = 'An enum test field'):
        return common.EnumField(name=name,
                                items=items,
                                size=size,
                                description=description)
    return _enum_field

@pytest.fixture
def int_field():
    def _int_field(name: str = 'signedintField',
                   size: int = 16,
                   data_type: str = 'int_16',
                   description: str = 'A signedint field'):
        return common.SignedIntField(name=name,
                                     size=size,
                                     data_type=data_type,
                                     description=description)
    return _int_field

@pytest.fixture
def uint_field():
    def _uint_field(name: str = 'signedintField',
                   size: int = 16,
                   data_type: str = 'int_16',
                   description: str = 'A signedint field'):
        return common.UnsignedIntField(name=name,
                                     size=size,
                                     data_type=data_type,
                                     description=description)
    return _uint_field

@pytest.fixture
def string_field():
    """Returns a fixed StringField 10 characters long."""
    def _string_field(name: str = 'stringFixture',
                      size: int = 200,
                      fixed: bool = False,
                      optional: bool = False,
                      default: str = None,
                      value: str = None):
        return common.StringField(name=name,
                                  description='A fixed string test field.',
                                  fixed=fixed,
                                  size=size,
                                  optional=optional,
                                  default=default,
                                  value=value)
    return _string_field

@pytest.fixture
def array_fields_property_example():
    fields = common.Fields()
    fields.add(common.StringField(name='propertyName', size=50))
    fields.add(common.UnsignedIntField(name='propertyValue',
                                       size=32,
                                       data_type='uint_32'))
    return fields

@pytest.fixture
def array_field(array_fields_property_example):
    """Returns a ArrayField defaulting to array_fields_property_example."""
    def _array_field(name: str = 'arrayFixture',
                     size: int = 1,
                     fields: common.Fields = None,
                     description: str = 'An example array of 2 fields',
                     optional: bool = False,
                     fixed: bool = False,
                     elements: "list[common.Fields]" = None):
        return common.ArrayField(name=name,
                                 description=description,
                                 size=size,
                                 fields=fields or array_fields_property_example,
                                 optional=optional,
                                 fixed=fixed,
                                 elements=elements)
    return _array_field

@pytest.fixture
def return_message(array_field):
    """Returns a ArrayField with no values."""
    fields = common.Fields()
    fields.add(common.BooleanField(name='testBool', value=True))
    fields.add(common.UnsignedIntField(name='testUint',
                                       size=16,
                                       data_type='uint_16',
                                       value=42))
    fields.add(common.SignedIntField(name='latitude',
                                     size=24,
                                     data_type='int_32',
                                     value=int(-45.123 * 60000)))
    fields.add(common.StringField(name='optionalString',
                                  size=100,
                                  optional=True))
    fields.add(common.StringField(name='nonOptionalString',
                                  size=100,
                                  value='A quick brown fox'))
    fields.add(array_field(name='arrayFixture'))
    elementOne = fields['arrayFixture'].new_element()
    elementOne['propertyName'].value = 'aPropertyName'
    elementOne['propertyValue'].value = 1
    fields.add(common.DataField(name='testData',
                                size=4,
                                data_type='float',
                                value=4.2))
    message = common.Message(name='returnMessageFixture',
                             sin=255,
                             min=1,
                             fields=fields)
    return message

@pytest.fixture
def return_messages(return_message):
    return_messages = common.Messages(sin=255, is_forward=False)
    return_messages.add(return_message)
    return return_messages

@pytest.fixture
def service(return_messages):
    service = common.Service(name='testService', sin=255)
    service.messages_return = return_messages
    return service

@pytest.fixture
def services(service):
    services = common.Services()
    services.add(service)
    return services

@pytest.fixture
def message_definitions(services):
    message_definitions = common.MessageDefinitions()
    message_definitions.services = services
    return message_definitions

def test_boolean_field(bool_field):
    test_field = bool_field()
    assert(not test_field.value)
    assert(not test_field.optional)
    assert(not test_field.default)
    assert(test_field.encode() == '0')
    test_field.value = False
    assert(not test_field.value)
    with pytest.raises(ValueError):
        test_field.value = 1
    bool_dflt_true = bool_field(default=True)
    assert(bool_dflt_true.encode() == '1')
    bool_dflt_false_valset = bool_field(value=True)
    assert(bool_dflt_false_valset.encode() == '1')

def test_data_field(data_field):
    MAX_BYTES = 128
    test_field = data_field(size=MAX_BYTES)
    assert(not test_field.value)
    assert(not test_field.optional)
    assert(not test_field.default)
    with pytest.raises(ValueError):
        test_field.encode()
    for i in range(0, MAX_BYTES):
        b = [i % 255] * max(1, i)
        test_field.value = bytes(b)
        enc = test_field.encode()
        bits = len(enc)
        L = enc[:16] if i > 127 else enc[:8]
        data_bin = enc[len(L):]
        data_length = int(L[1:], 2)
        assert(data_length == len(b))
        data_bin = enc[len(L):]
        data = int(data_bin, 2).to_bytes(int((bits - len(L)) / 8), 'big')
        assert(data == bytes(b))
        test_field.value = None
        assert(test_field.value is None)
        test_field.decode(enc)
        assert(test_field.value == bytes(b))
    #TODO: test cases for padding, truncation

def test_string_field(string_field):
    from string import ascii_lowercase as char_iterator
    MAX_SIZE = 155
    FIXED_SIZE = 6
    FIXED_STR_LONG = 'abcdefghi'
    FIXED_STR_SHORT = 'a'
    test_field = string_field(size=MAX_SIZE)
    assert(not test_field.value)
    assert(not test_field.optional)
    assert(not test_field.default)
    with pytest.raises(ValueError):
        test_field.encode()
    test_str = ''
    for i in range(0, MAX_SIZE):
        if i > test_field.size:
            break
        test_str += char_iterator[i % 26]
        test_field.value = test_str
        binstr = ''.join(format(ord(c), '08b') for c in test_str)
        enc = test_field.encode()
        L = enc[:16] if len(test_str) > 127 else enc[:8]
        assert(enc == L + binstr)
        test_field.value = None
        assert(test_field.value is None)
        test_field.decode(enc)
        assert(test_field.value == test_str)
    test_field = string_field(size=FIXED_SIZE)
    test_field.value = FIXED_STR_LONG
    assert(test_field.value == FIXED_STR_LONG[:FIXED_SIZE])
    test_field.fixed=True
    test_field.value = FIXED_STR_SHORT
    assert(test_field.value == FIXED_STR_SHORT)
    binstr = ''.join(format(ord(c), '08b') for c in FIXED_STR_SHORT)
    binstr += '0' * 8 * (FIXED_SIZE - len(FIXED_STR_SHORT))
    enc = test_field.encode()
    assert(len(enc) == FIXED_SIZE * 8)
    assert(enc == binstr)
    v = test_field.value
    test_field.decode(enc)
    assert(test_field.value == v)

def test_array_field(array_field, array_fields_property_example):
    MAX_SIZE = 2
    test_field = array_field(size=1, fields=array_fields_property_example)
    assert(not test_field.fixed)
    assert(not test_field.optional)
    with pytest.raises(ValueError):
        test_field.encode()
    for i in range(0, MAX_SIZE):
        element = array_fields_property_example
        element['propertyName'] = 'testProp{}'.format(i)
        element['propertyValue'] = i
        test_field.append(element)
        ref = deepcopy(test_field)
        enc = test_field.encode()
        L = enc[:16] if i > 127 else enc[:8]
        assert(len(test_field.elements) == int('0b' + L, 2))
        test_field.decode(enc)
        assert(test_field == ref)

def test_enum_field():
    test_items = ['item1', 'item2', 'item3']
    size = 2
    defaults = [None, 'item1', 1]
    for default in defaults:
        test_field = common.EnumField(name='validEnum',
                                items=test_items,
                                size=size,
                                default=default)
        assert(test_field.items == test_items)
        if default is None:
            assert(test_field.value is None)
        elif isinstance(default, str):
            assert(test_field.value == default)
        else:
            assert(test_field.value == test_items[default])
    assert(test_field.encode() == '01')  #:assumes last default is 1
    with pytest.raises(ValueError):
        test_field = common.EnumField(name='testEnum', items=None, size=None)
    with pytest.raises(ValueError):
        test_field = common.EnumField(name='testEnum', items=[1, 3], size=2)
    with pytest.raises(ValueError):
        test_field = common.EnumField(name='testEnum', items=test_items, size=1)

@pytest.mark.filterwarnings('ignore:Clipping')
def test_unsignedint_field():
    BIT_SIZE = 16
    with pytest.raises(ValueError):
        test_field = common.UnsignedIntField(name='failedBitSize', size=0)
    test_field = common.UnsignedIntField(name='testUint', size=BIT_SIZE)
    assert(test_field.default is None)
    assert(test_field.value is None)
    with pytest.raises(ValueError):
        test_field.encode()
    with pytest.raises(ValueError):
        test_field.value = -1
    test_field.value = 1
    assert(test_field.encode() == '0' * (BIT_SIZE - 1) + '1')
    test_field.value = 2**BIT_SIZE
    assert(test_field.value == 2**BIT_SIZE - 1)
    v = test_field.value
    enc = test_field.encode()
    assert(len(enc) == BIT_SIZE)
    test_field.decode(enc)
    assert(test_field.value == v)

@pytest.mark.filterwarnings('ignore:Clipping')
def test_signedint_field():
    BIT_SIZE = 16
    with pytest.raises(ValueError):
        test_field = common.SignedIntField(name='failedBitSize', size=0)
    test_field = common.SignedIntField(name='testInt', size=BIT_SIZE)
    assert(test_field.default is None)
    assert(test_field.value is None)
    with pytest.raises(ValueError):
        test_field.encode()
    test_field.value = -1
    assert(test_field.encode() == '1' * BIT_SIZE)
    test_field.value = 2**BIT_SIZE
    assert(test_field.value == 2**BIT_SIZE / 2 - 1)
    v = test_field.value
    enc = test_field.encode()
    assert(len(enc) == BIT_SIZE)
    test_field.decode(enc)
    assert(test_field.value == v)
    test_field.value = -(2**BIT_SIZE)
    assert(test_field.value == -(2**BIT_SIZE / 2))
    v = test_field.value
    enc = test_field.encode()
    assert(len(enc) == BIT_SIZE)
    test_field.decode(enc)
    assert(test_field.value == v)

def test_bool_xml(bool_field):
    test_field = bool_field()
    xml = test_field.xml()
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'BooleanField')
    assert(xml.find('Name').text == test_field.name)
    assert(xml.find('Description').text == test_field.description)

def test_enum_xml(enum_field):
    test_field = enum_field()
    xml = test_field.xml()
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'EnumField')
    assert(xml.find('Name').text == test_field.name)
    assert(xml.find('Size').text == str(test_field.size))
    assert(xml.find('Description').text == test_field.description)
    items = xml.find('Items')
    i = 0
    for item in items.findall('string'):
        string = item.text
        assert(string == test_field.items[i])
        i += 1

def test_array_xml(array_field):
    test_field = array_field()
    xml = test_field.xml()
    # ET.dump(xml)
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'ArrayField')
    assert(xml.find('Name').text == test_field.name)
    assert(xml.find('Size').text == str(test_field.size))
    assert(xml.find('Description').text == test_field.description)
    i = 0
    fields = xml.find('Fields')
    for field in fields.findall('Field'):
        assert(field.find('Name').text == test_field.fields[i].name)
        i += 1

def test_return_message_xml(return_message):
    rm = return_message
    ET.dump(rm.xml())

def test_mdf_xml(message_definitions):
    xml = message_definitions.xml(indent=True)
    print(xml)

def test_rm_codec(return_message):
    msg = return_message
    msg_copy = deepcopy(return_message)
    encoded = msg.encode(data_format=FORMAT_HEX)
    # print('Encoded:\n{}'.format(encoded))
    hex_message = (format(encoded['sin'], '02X') +
                   format(encoded['min'], '02X') +
                   encoded['data'])
    msg.decode(bytes.fromhex(hex_message))
    # print('Pre-decode:\n{}'.format(vars(msg_copy)))
    # print('Post-decode:\n{}'.format(vars(msg)))
    assert(msg_copy == msg)
