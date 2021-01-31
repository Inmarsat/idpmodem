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
from idpmodem.atcommand_async import IdpModemAsyncioClient, AtGnssTimeout
from idpmodem.nmea import Location


DEFAULT_PORT = '/dev/ttyUSB0'


def repeat_to_length(string_to_expand: str, length: int) -> str:
    return (string_to_expand * (int(length/len(string_to_expand))+1))[:length]


class IdpModemTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        user_options = parse_args(sys.argv)
        port = user_options['port']
        print("Setting up modem for test cases...")
        cls.modem = IdpModemAsyncioClient(port=DEFAULT_PORT, log_level=10)
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

    def test_999_multithread_fail(self):
        """Creates a race condition that fails."""
        from threading import Thread
        
        def parallel_call():
            try:
                result_alt = run(self.modem.initialize())
                print('{}'.format(result_alt))
            except Exception as e:
                print('Parallel error: {}'.format(e))
                self.assertTrue(e)
        
        self.display_tc_header()
        try:
            test_thread = Thread(target=parallel_call, daemon=True)
            test_thread.start()
            result_main = run(self.modem.initialize())
            print(result_main)
            test_thread.join()
        except Exception as e:
            print('Main error: {}'.format(e))
            self.assertTrue(e)

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
        except AtGnssTimeout:
            print('GNSS timeout occurred, check sky visibility')
            self.assertTrue(True)

    def test_09_lowpower_notifications_set(self):
        self.display_tc_header()
        notifications = run(self.modem.lowpower_notifications_enable())
        self.assertTrue(notifications)

    def test_10_lowpower_notifications_check(self):
        self.display_tc_header()
        notifications = run(self.modem.lowpower_notifications_check())
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

    def test_162_message_mt_parse(self):
        self.display_tc_header()
        hex_example = '%MGFG: "FM31.63",31.63,0,17,2,6,2,AD4F6C8221'
        b64_example = '%MGFG: "FM31.63",31.63,0,17,2,6,3,rU9sgiE='
        txt_example = '%MGFG: "FM31.63",31.63,0,17,2,6,1,"\\ADOl\\82!"'
        common_expected = {
            'name': 'FM31.63',
            'system_message_number': 31,
            'system_message_sequence': 63,
            'priority': 0,
            'sin': 17,
            'min': 173,
            'state': 2,
            'length': 6,
            'bytes': b'\x11\xadOl\x82!',
        }
        hex_expected = common_expected.copy()
        hex_expected['data_format'] = 2
        hex_expected['raw_payload'] = '11AD4F6C8221'
        b64_expected = common_expected.copy()
        b64_expected['data_format'] = 3
        b64_expected['raw_payload'] = 'Ea1PbIIh'
        txt_expected = common_expected.copy()
        txt_expected['data_format'] = 1
        txt_expected['raw_payload'] = '\\11\\ADOl\\82!'
        self.assertTrue(txt_expected == self.modem._message_mt_parse(txt_example, 1))
        self.assertTrue(hex_expected == self.modem._message_mt_parse(hex_example, 2))
        self.assertTrue(b64_expected == self.modem._message_mt_parse(b64_example, 3))

    def test_171_mock_message_mt_delete(self):
        self.display_tc_header()
        msg_name = self.mt_messages[0]
        mock_message_mt_delete = create_autospec(self.modem.message_mt_delete,
            return_value=True)
        success = run(mock_message_mt_delete(name=msg_name))
        self.assertTrue(success)

    def test_18_event_monitor_set(self):
        self.display_tc_header()
        events_to_monitor = [(3, 1), (3, 2)]
        success = run(self.modem.event_monitor_set(events_to_monitor))
        self.assertTrue(success)

    def test_19_event_monitor_get(self):
        self.display_tc_header()
        events_monitored = run(self.modem.event_monitor_get())
        pprint.pprint(events_monitored)
        self.assertTrue(isinstance(events_monitored, list))

    def test_20_event_get(self):
        self.display_tc_header()
        event_to_get = (3, 1)
        event_data = run(self.modem.event_get(event=event_to_get, raw=False))
        pprint.pprint(event_data)
        self.assertTrue(isinstance(event_data, dict))

    def test_21_notification_control_set(self):
        self.display_tc_header()
        event_map = {
            ('message_mt_received', True),
            ('message_mo_complete', True),
            ('event_cached', True),
        }
        success = run(self.modem.notification_control_set(event_map))
        self.assertTrue(success)

    def test_22_notification_control_get(self):
        self.display_tc_header()
        event_map = run(self.modem.notification_control_get())
        pprint.pprint(event_map)
        self.assertTrue(isinstance(event_map, dict))

    def test_23_notification_check(self):
        self.display_tc_header()
        notifications = run(self.modem.notification_check())
        pprint.pprint(notifications)
        self.assertTrue(isinstance(notifications, dict))

    def test_24_satellite_status(self):
        self.display_tc_header()
        status = run(self.modem.satellite_status())
        print('State: {} | C/N0: {} dB | Beam search: {}'.format(
            self.modem.sat_status_name(status['state']),
            status['snr'],
            self.modem.sat_beamsearch_name(status['beamsearch'])))
        self.assertTrue(status is not None)

    def test_25_time_utc(self):
        self.display_tc_header()
        time = run(self.modem.time_utc())
        print('Time: {}'.format(time))
        self.assertTrue(time is not None)

    def test_26_s_register_get(self):
        self.display_tc_header()
        reg = 54
        value = run(self.modem.s_register_get(reg))
        print('S{}: {}'.format(reg, value))
        self.assertTrue(value is not None)

    def test_27_s_register_get_all(self):
        self.display_tc_header()
        registers = run(self.modem.s_register_get_all())
        pprint.pprint(registers)
        self.assertTrue(registers is not None)

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
        'test_01_initialize'
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
