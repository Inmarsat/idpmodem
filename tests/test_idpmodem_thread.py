#!/usr/bin/env python
import argparse
import inspect
import pprint
import sys
import time
import unittest

from idpmodem.atcommand_thread import get_modem_thread, IdpModemBusy, AtException, AtCrcConfigError, AtCrcError, AtTimeout


DEFAULT_PORT = '/dev/ttyUSB1'


class IdpModemTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        user_options = parse_args(sys.argv)
        port = user_options['port']
        print("Setting up modem for test cases...")
        (modem, thread) = get_modem_thread()
        cls.modem = modem
        cls.modem_thread = thread
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

    def test_01_connection(self):
        self.display_tc_header()
        while not self.modem.connected:
            pass
        self.assertTrue(self.modem.connected)

    def test_02_sregisters(self):
        self.display_tc_header()
        at_config, reg_config = self.modem.config_report()
        print('{}\n{}'.format(at_config, reg_config))
        self.assertTrue(at_config and reg_config)

    def test_03_crc_enable(self):
        self.display_tc_header()
        self.modem.config_crc_enable(True)
        self.assertTrue(self.modem.crc)

    def test_04_crc_disable(self):
        self.display_tc_header()
        self.modem.config_crc_enable(False)
        self.assertFalse(self.modem.crc)

    def test_05_device_mobile_id(self):
        self.display_tc_header()
        mobile_id = self.modem.device_mobile_id()
        print('Mobile ID: {}'.format(mobile_id))
        self.assertTrue(len(mobile_id) == 15)

    def test_06_device_versions(self):
        self.display_tc_header()
        versions = self.modem.device_version()
        pprint.pprint(versions)
        self.assertTrue(isinstance(versions, object))

    def test_07_location_get(self):
        self.display_tc_header()
        location = self.modem.location_get()
        print(pprint.pformat(vars(location), indent=2, width=1))
        self.assertTrue(isinstance(location, object))

    def test_08_lowpower_notifications_set(self):
        self.display_tc_header()
        notifications = self.modem.lowpower_notifications_enable()
        self.assertTrue(notifications)

    def test_09_notification_check(self):
        self.display_tc_header()
        notifications = self.modem.lowpower_notification_check()
        print('{}'.format(notifications))
        self.assertTrue(isinstance(notifications, list))

    def test_10_message_mo_send(self):
        self.display_tc_header()
        msg_name = self.modem.message_mo_send(data='TEST10',
                                              data_format=1,
                                              sin=128)
        print('MO message assigned name: {}'.format(msg_name))
        self.mo_messages.append(msg_name)
        self.assertTrue(isinstance(msg_name, str))

    def test_11_message_mo_state(self):
        self.display_tc_header()
        states = self.modem.message_mo_state()
        pprint.pprint(states)
        self.assertTrue(isinstance(states, list))


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
        'test_02_sregisters',
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
