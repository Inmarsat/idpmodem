#!/usr/bin/env python
import argparse
from asyncio import run
import inspect
import pprint
import sys
import time
import unittest
from unittest.mock import create_autospec

from idpmodem.utils import get_wrapping_logger
import idpmodem.atcommand_async
from idpmodem.atcommand_async import IdpModemAsyncioClient, GnssTimeout
from idpmodem.nmea import Location


DEFAULT_PORT = '/dev/ttyUSB1'


def repeat_to_length(string_to_expand: str, length: int) -> str:
    return (string_to_expand * (int(length/len(string_to_expand))+1))[:length]


class IdpModemTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        user_options = parse_args(sys.argv)
        port = user_options['port']
        print("Setting up modem for test cases...")
        cls.modem = IdpModemAsyncioClient(log_level=10)
        cls.event_callback = None
        cls.new_mt_messages = False
        cls.mt_message_being_retrieved = None
        cls.mo_msg_complete = False
        cls.mt_messages = []
        cls.mo_messages = []
        cls.location_pending = False
        cls.tracking_count = 0
        cls.on_message_pending = False
        cls.test_case = 0

    def setUp(self):
        sleep_time = 3
        print("\n*** NEXT TEST CASE STARTING IN {}s ***".format(sleep_time))
        time.sleep(sleep_time)

    def tearDown(self):
        print("*** TEST CASE {} COMPLETE ***".format(self.test_case))

    def display_tc_header(self, more_info=None):
        calling_function = inspect.stack()[1][3]
        func_tags = calling_function.split('_')
        self.test_case = int(func_tags[1])
        func_name = func_tags[2].upper()
        if len(func_tags) > 2:
            for i in range(3, len(func_tags)):
                func_name += ' ' + func_tags[i].upper()
        if more_info is not None and isinstance(more_info, dict):
            for k, v in more_info.iteritems():
                func_name += ' ({}={})'.format(k, v)
        print("\n*** TEST CASE {} - {} ***".format(self.test_case, func_name))

    def action_prompt(self, message, ref_time, tick=5):
        if time.time() - ref_time >= tick:
            ref_time = time.time()
            message = '\n** ' + 'TEST CASE {} - '.format(self.test_case) + message + ' **\n'
            wrapper = '*' * len(message.strip())
            print('{}{}{}'.format(wrapper, message, wrapper))
        return ref_time

    def test_01_initialize(self):
        self.display_tc_header()
        result = run(self.modem.initialize(crc=False))
        self.assertTrue(result)

    def test_02_initialize_crc(self):
        self.display_tc_header()
        result = run(self.modem.initialize(crc=True))
        self.assertTrue(result)

    def test_03_config_report(self):
        self.display_tc_header()
        at_config, reg_config = run(self.modem.config_report())
        print('{}\n{}'.format(at_config, reg_config))
        self.assertTrue(at_config and reg_config)

    def test_04_crc_enable(self):
        self.display_tc_header()
        run(self.modem.config_crc_enable(True))
        self.assertTrue(self.modem.crc)

    def test_05_crc_disable(self):
        self.display_tc_header()
        run(self.modem.config_crc_enable(False))
        self.assertFalse(self.modem.crc)

    def test_06_device_mobile_id(self):
        self.display_tc_header()
        mobile_id = run(self.modem.device_mobile_id())
        print('Mobile ID: {}'.format(mobile_id))
        self.assertTrue(len(mobile_id) == 15)

    def test_07_device_versions(self):
        self.display_tc_header()
        versions = run(self.modem.device_version())
        pprint.pprint(versions)
        self.assertTrue(isinstance(versions, object))

    def test_08_location(self):
        self.display_tc_header()
        try:
            location = run(self.modem.location())
            if location is not None:
                print(pprint.pformat(vars(location), indent=2, width=1))
                self.assertTrue(isinstance(location, Location))
        except GnssTimeout:
            print('GNSS timeout occurred, check sky visibility')
            self.assertTrue(True)

    def test_09_lowpower_notifications_set(self):
        self.display_tc_header()
        notifications = run(self.modem.lowpower_notifications_enable())
        self.assertTrue(notifications)

    def test_10_lowpower_notification_check(self):
        self.display_tc_header()
        notifications = run(self.modem.lowpower_notification_check())
        print('{}'.format(notifications))
        self.assertTrue(isinstance(notifications, list))

    def test_11_message_mo_send(self):
        self.display_tc_header()
        msg_name = run(self.modem.message_mo_send(data='TEST11',
                                                   data_format=1,
                                                   sin=128))
        print('MO message assigned name: {}'.format(msg_name))
        self.mo_messages.append(msg_name)
        self.assertTrue(isinstance(msg_name, str))

    def test_111_mock_message_mo_send(self):
        self.display_tc_header()
        mock_message_mo_send = create_autospec(self.modem.message_mo_send,
            return_value='testname')
        msg_name = run(mock_message_mo_send(data='TEST11',
                                            data_format=1,
                                            sin=128))
        print('MO message assigned name: {}'.format(msg_name))
        self.mo_messages.append(msg_name)
        mock_message_mo_send.assert_called_once_with(data='TEST11',
            data_format=1,sin=128)

    def test_12_message_mo_state(self):
        self.display_tc_header()
        states = run(self.modem.message_mo_state())
        pprint.pprint(states)
        self.assertTrue(isinstance(states, list))
    
    def test_121_mock_message_mo_state(self):
        self.display_tc_header()
        rv = {
            'name': 'testname',
            'state': 'something',
            'size': 2,
            'sent': 2,
        }
        mock_message_mo_state = create_autospec(self.modem.message_mo_state,
            return_value=rv)
        states = run(mock_message_mo_state())
        pprint.pprint(states)
        self.assertTrue(isinstance(states, list))
    
    def test_13_message_mo_cancel(self):
        self.display_tc_header()
        data = repeat_to_length('TEST', 1999)
        msg_name = run(self.modem.message_mo_send(data=data,
                                                   data_format=1,
                                                   sin=128))
        success = run(self.modem.message_mo_cancel(msg_name))
        self.assertTrue(success)

    def test_14_message_mo_clear(self):
        self.display_tc_header()
        data = repeat_to_length('TEST', 29)
        msg_name = run(self.modem.message_mo_send(data=data,
                                                   data_format=1,
                                                   sin=128))
        deleted_count = run(self.modem.message_mo_clear())
        self.assertTrue(deleted_count == 1)

    def test_15_message_mt_waiting(self):
        self.display_tc_header()
        waiting = run(self.modem.message_mt_waiting())
        if waiting:
            print('Waiting: {}'.format(waiting))
            for meta in waiting:
                self.mt_messages.append(meta['name'])
        self.assertTrue(isinstance(waiting, list) or waiting == None)

    def test_16_message_mt_get(self):
        self.display_tc_header()
        msg_name = self.mt_messages[0]
        data = run(self.modem.message_mt_get(msg_name))
        print('Data: {}'.format(data))
        self.assertTrue(isinstance(data, str))

    def test_17_message_mt_delete(self):
        self.display_tc_header()
        msg_name = self.mt_messages[0]
        success = run(self.modem.message_mt_delete(msg_name))
        self.assertTrue(success)

    def test_151_mock_message_mt_waiting(self):
        self.display_tc_header()
        rv = [{
            'name': 'FM01.01',
            'sin': 128,
            'priority': 0,
            'state': 1,
            'length': 10,
            'received': 10,
        }]
        mock_message_mt_waiting = create_autospec(self.modem.message_mt_waiting,
            return_value=rv)
        waiting = run(mock_message_mt_waiting())
        if waiting:
            print('Waiting: {}'.format(waiting))
            for meta in waiting:
                self.mt_messages.append(meta['name'])
        self.assertTrue(isinstance(waiting, list) or waiting == None)

    def test_161_mock_message_mt_get(self):
        self.display_tc_header()
        msg_name = self.mt_messages[0]
        rv = 'ABCD'
        mock_message_mt_get = create_autospec(self.modem.message_mt_get,
            return_value=rv)
        data = run(mock_message_mt_get(name=msg_name))
        print('Data: {}'.format(data))
        self.assertTrue(isinstance(data, str))

    def test_171_mock_message_mt_delete(self):
        self.display_tc_header()
        msg_name = self.mt_messages[0]
        mock_message_mt_delete = create_autospec(self.modem.message_mt_delete,
            return_value=True)
        success = run(mock_message_mt_delete(name=msg_name))
        self.assertTrue(success)

    def test_end(self):
        pass


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")

    parser.add_argument('-p', '--port', dest='port', type=str, default=DEFAULT_PORT,
                        help="the serial port of the IDP modem")

    return vars(parser.parse_args(args=argv[1:]))


def suite():
    suite = unittest.TestSuite()
    available_tests = unittest.defaultTestLoader.getTestCaseNames(IdpModemTestCase)
    tests = [
        'test_151_mock_message_mt_waiting',
        'test_171_mock_message_mt_delete',
        # Add test cases above as strings or leave empty to test all cases
    ]
    if len(tests) > 0:
        for test in tests:
            for available_test in available_tests:
                if test in available_test:
                    suite.addTest(IdpModemTestCase(available_test))
    else:
        for available_test in available_tests:
            suite.addTest(IdpModemTestCase(available_test))
    return suite


if __name__ == '__main__':
    runner = unittest.TextTestRunner()
    runner.run(suite())
