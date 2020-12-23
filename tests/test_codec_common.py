from copy import deepcopy
import inspect
from pprint import pprint, pformat
from random import randrange, random, choice
from unittest import TestCase, TestSuite, TextTestRunner, defaultTestLoader

from idpmodem.codecs import common
from idpmodem.constants import FORMAT_TEXT, FORMAT_HEX, FORMAT_B64

data_types = [
    {
        'name': 'bool',
        'description': 'boolean',
        'data_type': 'bool',
        'value': choice([True, False]),
        'value_range': None
    },
    {
        'name': 'uint_8',
        'description': 'unsigned 8-bit integer',
        'data_type': 'uint_8',
        'value': randrange(0, 2**8),
        'value_range': (0, 2**8-1)
    },
    {
        'name': 'int_8',
        'description': 'signed 8-bit integer',
        'data_type': 'int_8',
        'value': randrange(int(-(2**8)/2), int(2**8/2)-1),
        'value_range': (int(-(2**8)/2), int(2**8/2)-1)
    },
    {
        'name': 'uint_16',
        'description': 'unsigned 16-bit integer',
        'data_type': 'uint_16',
        'value': randrange(0, 2**16),
        'value_range': (0, 2**16-1)
    },
    {
        'name': 'int_16',
        'description': 'signed 16-bit integer',
        'data_type': 'int_16',
        'value': randrange(int(-(2**16)/2), int(2**16/2)-1),
        'value_range': (int(-(2**16)/2), int(2**16/2)-1)
    },
    {
        'name': 'uint_32',
        'description': 'unsigned 32-bit integer',
        'data_type': 'uint_32',
        'value': randrange(0, 2**32),
        'value_range': (0, 2**32-1)
    },
    {
        'name': 'int_32',
        'description': 'signed 32-bit integer',
        'data_type': 'int_32',
        'value': randrange(int(-(2**32)/2), int(2**32/2)-1),
        'value_range': (int(-(2**32)/2), int(2**32/2)-1)
    },
    {
        'name': 'uint_31',
        'description': 'unsigned 31-bit integer',
        'data_type': 'uint_31',
        'value': randrange(0, 2**31),
        'value_range': (0, 2**31-1)
    },
    {
        'name': 'uint_64',
        'description': 'unsigned 64-bit integer',
        'data_type': 'uint_64',
        'value': randrange(0, 2**64),
        'value_range': (0, 2**64-1)
    },
    {
        'name': 'int_64',
        'description': 'signed 64-bit integer',
        'data_type': 'int_64',
        'value': randrange(int(-(2**64)/2), int(2**64/2)-1),
        'value_range': (int(-(2**64)/2), int(2**64/2)-1)
    },
    {
        'name': 'float',
        'description': '32-bit floating point',
        'data_type': 'float',
        'value': random(),
        'value_range': None
    },
    {
        'name': 'double',
        'description': '64-bit floating point',
        'data_type': 'double',
        'value': random(),
        'value_range': None
    },
    {
        'name': 'string',
        'description': 'ASCII text',
        'data_type': 'string',
        'value': choice(['a fast brown fox', 'holy cow']),
        'bits': 8 * 100
    },
    {
        'name': 'data',
        'description': 'A byte array',
        'data_type': 'data',
        'value': bytearray('this is a test', 'utf-8'),
        'bits': 8 * len('this is a test')
    },
]

class CodecTestCase(TestCase):
    
    @classmethod
    def setUpClass(cls):
        cls.message = None

    def test_01_create_message(self):
        name = 'testMessage'
        SIN = 255
        MIN = 255
        message = common.CommonMessageFormat(name=name, sin=SIN, min=MIN)
        self.assertTrue(message.name == name and
                        message.sin == SIN and
                        message.min == MIN)
        if inspect.stack()[1][3] != '_callTestMethod':
            return message
    
    def test_02_create_field(self, detail: dict = None):
        if isinstance(detail, dict):
            name = detail['name']
            description = detail['description']
            data_type = detail['data_type']
            value = detail['value']
            value_range = detail['value_range'] if 'value_range' in detail else None
            bits = detail['bits'] if 'bits' in detail else None
        else:
            name = 'testField'
            description = 'some description'
            data_type = 'uint_8'
            value = 0
            value_range = (0, 15)
            bits = 4
        field = common.Field(name=name, description=description,
            data_type=data_type, value=value, bits=bits,
            value_range=value_range)
        self.assertTrue(field.name == name and
                        field.description == description and
                        field.data_type == data_type and
                        field.value == value and
                        field.value_range == value_range and
                        isinstance(field.bits, int) and field.bits > 0 and
                        field._format == '0{}b'.format(field.bits))
        if inspect.stack()[1][3] != '_callTestMethod':
            return field
    
    def test_03_add_field(self):
        message = self.test_01_create_message()
        field = self.test_02_create_field()
        message.fields.add(field)
        self.assertTrue(len(message.fields) == 1)
        if inspect.stack()[1][3] != '_callTestMethod':
            return message
    
    def test_04_del_field(self):
        message = self.test_03_add_field()
        field_name = message.fields[0].name
        message.fields.delete(field_name)
        self.assertTrue(len(message.fields) == 0)
    
    def test_05_encode(self):
        message = self.test_03_add_field()
        encoded_b64 = message.encode()
        self.assertTrue(encoded_b64['sin'] == 255 and
                        encoded_b64['min'] == 255 and
                        encoded_b64['data_format'] == 3 and
                        encoded_b64['data'] == 'AA==')
    
    def test_06_encode_hex(self):
        message = self.test_03_add_field()
        encoded_hex = message.encode(data_format=2)
        self.assertTrue(encoded_hex['sin'] == 255 and
                        encoded_hex['min'] == 255 and
                        encoded_hex['data_format'] == 2 and
                        encoded_hex['data'] == '00')
    
    def test_07_all_field_types(self):
        message = self.test_01_create_message()
        for data_type in data_types:
            # if data_type['name'] != 'data': continue
            field = self.test_02_create_field(data_type)
            message.fields.add(field)
        self.assertTrue(isinstance(message.encode(), dict))
        print('Test 07 OTA size: {}'.format(message.ota_size()))
    
    def test_08_derive(self):
        name = 'fowardMessageTest'
        SIN = 255
        MIN = 1
        message = common.CommonMessageFormat(name=name, sin=SIN, min=MIN, is_forward=True)
        message.fields.add(common.Field(name='interval', data_type='uint_32', value=86400, value_range=(0, 86400), bits=17))
        message.fields.add(common.Field(name='signed', data_type='int_8', value=-1, bits=3))
        message.fields.add(common.Field(name='string', data_type='string', value='A', bits=8))
        message.fields.add(common.Field(name='data', data_type='data', value=bytes([1, 2]), bits=16))
        message.fields.add(common.Field(name='float', data_type='float', value=5.6, bits=32))
        m_copy = deepcopy(message)
        encoded = message.encode(data_format=2)
        databytes = bytes([SIN, MIN]) + bytearray.fromhex(encoded['data'])
        message.derive(databytes)
        if round(message.fields['float'].value, 1) == m_copy.fields['float'].value:
            message.fields['float'].value = m_copy.fields['float'].value
        for attr in m_copy.__dict__:
            if attr == 'fields':
                for f in m_copy.fields:
                    print('Testing field {}'.format(f.name))
                    f_derived = message.fields[f.name]
                    for f_attr in f.__dict__:
                        self.assertTrue(f_derived.__dict__[f_attr] == f.__dict__[f_attr])
            else:
                print('Testing message {}'.format(attr))
                self.assertTrue(message.__dict__[attr] == m_copy.__dict__[attr])


def suite():
    suite = TestSuite()
    available_tests = defaultTestLoader.getTestCaseNames(CodecTestCase)
    tests = [
        'test_08_derive',
        # Add test cases above as strings or leave empty to test all cases
    ]
    if len(tests) > 0:
        for test in tests:
            for available_test in available_tests:
                if test in available_test:
                    suite.addTest(CodecTestCase(available_test))
    else:
        for available_test in available_tests:
            suite.addTest(CodecTestCase(available_test))
    return suite


if __name__ == '__main__':
    runner = TextTestRunner()
    runner.run(suite())
