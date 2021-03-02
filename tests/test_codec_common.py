import pytest
from copy import deepcopy
import xml.etree.ElementTree as ET

from idpmodem.codecs import common
from idpmodem.constants import FORMAT_TEXT, FORMAT_HEX, FORMAT_B64


@pytest.fixture
def bool_field():
    """Returns a BooleanField with no value."""
    return common.BooleanField(name='boolFixture',
                               description='A boolean test field.')

@pytest.fixture
def enum_field():
    """Returns a EnumField with no value."""
    test_items = ['item1', 'item2', 'item3']
    return common.EnumField(name='enumFixture',
                            items=test_items,
                            size=2,
                            description='An enum test field.')

@pytest.fixture
def array_field():
    """Returns a ArrayField with no values."""
    fields = common.Fields()
    fields.add(common.StringField(name='propertyName', size=50))
    fields.add(common.UnsignedIntField(name='propertyValue',
                                       size=32,
                                       data_type='uint_32'))
    return common.ArrayField(name='arrayFixture',
                             description='An example array of 2 fields.',
                             size=10,
                             fields=fields)

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
    fields.add(array_field)
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

def test_bool_field(bool_field):
    assert(bool_field.value is None)
    bool_field.value = True
    assert(bool_field.value)
    bool_field.value = False
    assert(not bool_field.value)
    with pytest.raises(ValueError):
        bool_field.value = 1

def test_enum_valid():
    test_items = ['item1', 'item2', 'item3']
    size = 2
    defaults = [None, 'item1', 1]
    for default in defaults:
        enum = common.EnumField(name='validEnum',
                                items=test_items,
                                size=size,
                                default=default)
        assert(enum.items == test_items)
        if default is None:
            assert(enum.value is None)
        elif isinstance(default, str):
            assert(enum.value == default)
        else:
            assert(enum.value == test_items[default])
    assert(enum.encode() == '01')  #:assumes last default is 1
    with pytest.raises(ValueError):
        enum = common.EnumField(name='testEnum', items=None, size=None)
    with pytest.raises(ValueError):
        enum = common.EnumField(name='testEnum', items=[1, 3], size=2)
    with pytest.raises(ValueError):
        enum = common.EnumField(name='testEnum', items=test_items, size=1)

def test_bool_xml(bool_field):
    xml = bool_field.xml()
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'BooleanField')
    assert(xml.find('Name').text == bool_field.name)
    assert(xml.find('Description').text == bool_field.description)

def test_enum_xml(enum_field):
    xml = enum_field.xml()
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'EnumField')
    assert(xml.find('Name').text == enum_field.name)
    assert(xml.find('Size').text == str(enum_field.size))
    assert(xml.find('Description').text == enum_field.description)
    items = xml.find('Items')
    i = 0
    for item in items.findall('string'):
        string = item.text
        assert(string == enum_field.items[i])
        i += 1

def test_array_xml(array_field):
    xml = array_field.xml()
    ET.dump(xml)
    assert(xml.attrib['{http://www.w3.org/2001/XMLSchema-instance}type'] == 'ArrayField')
    assert(xml.find('Name').text == array_field.name)
    assert(xml.find('Size').text == str(array_field.size))
    assert(xml.find('Description').text == array_field.description)
    i = 0
    fields = xml.find('Fields')
    for field in fields.findall('Field'):
        assert(field.find('Name').text == array_field.fields[i].name)
        i += 1

def test_return_message_xml(return_message):
    xml = return_message.xml(indent=True)
    print(xml)
    # ET.dump(xml)
    assert(False)

def test_mdf_xml(message_definitions):
    xml = message_definitions.xml(indent=True)
    ET.dump(xml)
    assert(False)

def test_rm_encode(return_message):
    msg = return_message
    msg_copy = deepcopy(return_message)
    encoded = msg.encode(data_format=FORMAT_HEX)
    print(encoded)
    hex_message = (format(encoded['sin'], '02X') +
                   format(encoded['min'], '02X') +
                   encoded['data'])
    msg.decode(bytes.fromhex(hex_message))
    print(vars(msg))
    print(vars(msg_copy))
    assert(msg_copy == msg)
