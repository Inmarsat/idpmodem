import unittest
import time
from context import idpmodem
import inspect


class IdpModemTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        print("Setting up test case...")
        # TODO: Check why a "headless" log file is being created in the /tests directory
        try:
            cls.modem = idpmodem.Modem(serial_name='COM37', debug=True)
        except ValueError as e:
            print("Error trying COM38: {}".format(e))
            cls.modem = idpmodem.Modem(serial_name='COM38', debug=True)
        cls.event_callback = None
        cls.new_mt_messages = False
        cls.mo_msg_complete = False
        cls.mt_messages = []
        cls.mo_messages = []
        cls.location_pending = False
        cls.test_case = 0

    @classmethod
    def tearDownClass(cls):
        cls.modem.terminate()

    def setUp(self):
        # self.modem.on_connect = self.on_connect
        sleep_time = 5
        print("*** NEXT TEST CASE STARTING IN {}s ***".format(sleep_time))
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
        print("*** TEST CASE {} - {} ***".format(self.test_case, func_name))

    def action_prompt(self, message):
        message = '\n** ' + 'TEST CASE {} - '.format(self.test_case) + message + ' **\n'
        wrapper = '*' * len(message.strip())
        print('{}{}{}'.format(wrapper, message, wrapper))

    def on_connect(self):
        pass

    def test_1_connection(self):
        self.display_tc_header()
        while not self.modem.is_connected:
            pass
        self.assertTrue(self.modem.is_connected)

    def test_2_initialization(self):
        self.display_tc_header()
        while not self.modem.is_initialized:
            pass
        self.assertTrue(self.modem.is_initialized)

    # def test_n_at_failure_102(self):
    #     # TODO: build test cases for each AT error
    #     error_code = 102
    #     self.display_tc_header(more_info={
    #         'ErrorCode': error_code,
    #         'ErrorDesc': self.modem.at_err_result_codes(str(error_code))
    #     })
    #
    def test_3_satellite_status(self):
        self.display_tc_header(
            more_info={
                'initial': self.modem.sat_status.ctrl_state,
            })
        ref_time = time.time()
        tick = 5
        initial_status = self.modem.sat_status.ctrl_state
        while self.modem.sat_status.ctrl_state == initial_status:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                self.action_prompt("TRIGGER SATELLITE STATUS CHANGE (Trace Class 3 Subclass 1 Index 22 ({})"
                                   .format(self.modem.sat_status.ctrl_state))
        self.assertFalse(self.modem.sat_status.ctrl_state == initial_status)
        self.action_prompt("SATELLITE STATUS CHANGED TO: {}".format(self.modem.sat_status.ctrl_state))

    def test_4_event_notify_network_registration(self):
        self.display_tc_header()
        success, error = self.modem.register_event_callback(event='registered', callback=self.cb_sat_status)
        if not success:
            print error
            self.assertFalse(success)
        ref_time = time.time()
        tick = 5
        while self.event_callback is None:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                if self.modem.sat_status.ctrl_state == 'Active':
                    self.action_prompt("REVERT STATUS FROM ACTIVE (Trace Class 3 Subclass 1 Index 22 Value 10 ({})"
                                       .format(self.modem.sat_status.ctrl_state))
                else:
                    self.action_prompt("TRIGGER MODEM REGISTRATION Trace Class 3 Subclass 1 Index 22 Value 10 ({})"
                                       .format(self.modem.sat_status.ctrl_state))
        self.assertTrue(self.event_callback is not None)

    def cb_sat_status(self, sat_status='Unknown'):
        print "TEST CASE {} CALLBACK FROM SATELLITE STATUS RECEIVED: {}".format(self.test_case, sat_status)
        self.event_callback = sat_status

    def test_5_mo_message(self):
        self.display_tc_header()
        # TODO: different message types text, ascii-hex,
        #
        payload = bytearray([16, 1, 2, 3])
        msg_sin = None
        msg_min = None
        data_format = idpmodem.FORMAT_HEX
        #
        # payload = 'test'
        # msg_sin = 128
        # msg_min = 0
        # data_format = idpmodem.FORMAT_TEXT
        #
        # data_format = idpmodem.FORMAT_B64
        #
        name = "TESTMO"
        test_msg = idpmodem.MobileOriginatedMessage(name=name, payload=payload, msg_sin=msg_sin, msg_min=msg_min,
                                                    data_format=data_format, debug=self.modem.debug)
        q_name = self.modem.send_message(test_msg, callback=self.cb_mo_msg_complete)
        self.mo_msg_complete = False
        self.mo_messages.append(q_name)   # TODO: likely this is redundant unless tests are running in parallel
        while not self.mo_msg_complete:
            pass
        self.assertTrue(test_msg.state >= 6)

    def cb_mo_msg_complete(self, success, message):
        if success:
            name, q_name, state, size_bytes = message
            print "TEST CASE {} MESSAGE {}({}) STATE={} ({} bytes)"\
                .format(self.test_case, name, q_name, state, size_bytes)
        else:
            print "FAILED TO SUBMIT MO MESSAGE"
        self.mo_msg_complete = True

    def test_6_event_notify_mt_message(self):
        self.display_tc_header()
        success, error = self.modem.register_event_callback(event='new_mt_message', callback=self.cb_new_mt_message)
        if not success:
            print error
            self.assertFalse(success)
        ref_time = time.time()
        tick = 5
        while not self.new_mt_messages:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                self.action_prompt("SEND MOBILE-TERMINATED MESSAGE")
        self.assertTrue(self.new_mt_messages)

    def cb_new_mt_message(self, messages):
        for msg in messages:
            print("TEST CASE {} MT message pending: {}".format(self.test_case, vars(msg)))
        self.mt_messages = self.modem.mt_msg_queue
        self.new_mt_messages = True

    def test_7_mt_message_get(self):
        self.display_tc_header()
        ref_time = time.time()
        tick = 5
        while len(self.modem.mt_msg_queue) == 0:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                self.action_prompt("SEND MOBILE-TERMINATED MESSAGE")
        while len(self.modem.mt_msg_queue) > 0:
            self.get_next_mt_message()
        self.assertTrue(len(self.modem.mt_msg_queue) == 0)

    def get_next_mt_message(self):
        TEXT_SIN = 128
        MAX_HEX_SIZE = 10
        if len(self.modem.mt_msg_queue) > 0:
            msg = self.modem.mt_msg_queue[0]
            if msg.q_name not in self.mt_messages:
                self.mt_messages.append(msg.q_name)
                if msg.sin == TEXT_SIN:
                    data_format = idpmodem.FORMAT_TEXT
                elif msg.size <= MAX_HEX_SIZE:
                    data_format = idpmodem.FORMAT_HEX
                else:
                    data_format = idpmodem.FORMAT_B64
                success, error = self.modem.get_mt_message(msg_name=msg.q_name, data_format=data_format,
                                                           callback=self.cb_get_mt_message)
                if not success:
                    print error
        else:
            print("No more pending MT messages")

    def cb_get_mt_message(self, message):
        data_format = idpmodem.FORMAT_TEXT
        print("TEST CASE {} MT message {} retrieved ({} bytes) raw: 0x{}".format(self.test_case, message.name,
                                                                                 message.size,
                                                                                 message.data(data_format=data_format,
                                                                                              include_min=True,
                                                                                              include_sin=True)))
        self.mt_messages.remove(message.name)

    def test_8_get_location(self):
        self.display_tc_header()
        self.location_pending = True
        self.modem.get_location(callback=self.cb_get_location)
        while self.location_pending:
            pass
        self.assertFalse(self.location_pending)

    def cb_get_location(self, loc):
        print(vars(loc))
        self.location_pending = False


def suite():
    suite = unittest.TestSuite()
    available_tests = unittest.defaultTestLoader.getTestCaseNames(IdpModemTestCase)
    tests = []
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
