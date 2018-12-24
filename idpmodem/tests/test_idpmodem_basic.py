import unittest
import time
from context import idpmodem
from context import headless


class IdpModemTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        print("Setting up test case...")
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
        cls.test_case = 0

    @classmethod
    def tearDownClass(cls):
        cls.modem.terminate()
        # print("**** TEST CASE {} COMPLETE ****".format(cls.test_case))
        # time.sleep(2)

    def setUp(self):
        sleep_time = 5
        self.test_case += 1
        print("***** TEST CASE {} STARTING IN {}s *****".format(self.test_case, sleep_time))
        time.sleep(sleep_time)

    def tearDown(self):
        print("***** TEST CASE {} COMPLETE *****".format(self.test_case))

    def test_1_connection(self):
        self.test_case = 1
        print("TEST CASE {} BASIC CONNECTION".format(self.test_case))
        while not self.modem.is_connected:
            pass
        self.assertTrue(self.modem.is_connected)

    def test_2_initialization(self):
        self.test_case = 2
        print("TEST CASE {} MODEM INITIALIZATION".format(self.test_case))
        while not self.modem.is_initialized:
            pass
        self.assertTrue(self.modem.is_initialized)

    def test_3_satellite_status(self):
        self.test_case = 3
        print("TEST CASE {} SATELLITE STATUS CHANGE (initial={})"
              .format(self.test_case, self.modem.sat_status.ctrl_state))
        ref_time = time.time()
        tick = 5
        initial_status = self.modem.sat_status.ctrl_state
        while self.modem.sat_status.ctrl_state == initial_status:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                wrapper = "*" * 65
                print("{}\n TRIGGER SATELLITE STATUS CHANGE Trace Class 3 Subclass 1 Index 22 ({}) \n{}"
                      .format(wrapper, self.modem.sat_status.ctrl_state, wrapper))
        self.assertFalse(self.modem.sat_status.ctrl_state == initial_status)
        print("*** TEST CASE {} STATUS CHANGE: {}".format(self.test_case, self.modem.sat_status.ctrl_state))

    def test_4_registration(self):
        self.test_case = 4
        print("TEST CASE {} MODEM REGISTRATION CALLBACK".format(self.test_case))
        success, error = self.modem.register_event_callback(event='registered', callback=self.cb_sat_status)
        if not success:
            print error
            self.assertFalse(success)
        ref_time = time.time()
        tick = 5
        while self.event_callback is None:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                wrapper = "*" * 65
                if self.modem.sat_status.ctrl_state == 'Active':
                    print("{}\n REVERT STATUS FROM ACTIVE Trace Class 3 Subclass 1 Index 22 Value 10 ({}) \n{}"
                          .format(wrapper, self.modem.sat_status.ctrl_state, wrapper))
                else:
                    print("{}\n TRIGGER MODEM REGISTRATION Trace Class 3 Subclass 1 Index 22 Value 10 ({}) \n{}"
                          .format(wrapper, self.modem.sat_status.ctrl_state, wrapper))
        self.assertTrue(self.event_callback is not None)

    def cb_sat_status(self, sat_status='Unknown'):
        print "TEST CASE {} CALLBACK FROM SATELLITE STATUS RECEIVED: {}".format(self.test_case, sat_status)
        self.event_callback = sat_status

    def test_5_mo_message(self):
        self.test_case = 5
        print("TEST CASE {} SEND MOBILE-ORIGINATED MESSAGE".format(self.test_case))
        # TODO: different message types text, ascii-hex,
        payload = bytearray([16, 1, 2, 3])
        msg_sin = None
        msg_min = None
        test_msg = idpmodem.MobileOriginatedMessage(payload=payload, msg_sin=msg_sin, msg_min=msg_min, debug=True)
        q_name = self.modem.send_message(test_msg, callback=self.cb_mo_msg_complete)
        self.mo_msg_complete = False
        self.mo_messages.append(q_name)
        while test_msg.state < 6:
            pass
        self.assertTrue(test_msg.state >= 6)

    def cb_mo_msg_complete(self, name, q_name, state, size_bytes):
        print "TEST CASE {} MESSAGE {}({}) STATE={} ({} bytes)".format(self.test_case, name, q_name, state, size_bytes)
        self.mo_msg_complete = True

    def test_6_mt_message_new(self):
        self.test_case = 6
        print("TEST CASE {} CHECK MOBILE-TERMINATED MESSAGES".format(self.test_case))
        success, error = self.modem.register_event_callback(event='new_mt_message', callback=self.cb_new_mt_message)
        if not success:
            print error
            self.assertFalse(success)
        ref_time = time.time()
        tick = 5
        while not self.new_mt_messages:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                wrapper = "*" * 65
                print("{}\n SEND MOBILE-TERMINATED MESSAGE\n{}".format(wrapper, wrapper))
        self.assertTrue(self.new_mt_messages)

    def cb_new_mt_message(self, messages):
        for msg in messages:
            print("TEST CASE {} MT message pending: {}".format(self.test_case, vars(msg)))
        self.mt_messages = self.modem.mt_msg_queue
        self.new_mt_messages = True

    def test_7_mt_message_get(self):
        self.test_case = 7
        print("TEST CASE {} RETRIEVE MOBILE-TERMINATED MESSAGE".format(self.test_case))
        ref_time = time.time()
        tick = 5
        while len(self.modem.mt_msg_queue) == 0:
            if time.time() - ref_time >= tick:
                ref_time = time.time()
                wrapper = "*" * 65
                print("{}\n SEND MOBILE-TERMINATED MESSAGE\n{}".format(wrapper, wrapper))
        for msg in self.modem.mt_msg_queue:
            if msg.sin == 128:
                data_format = 1
            elif msg.size <= 100:
                data_format = 2
            else:
                data_format = 3
            success, error = self.modem.get_mt_message(msg_name=msg.q_name, data_format=data_format,
                                                       callback=self.cb_get_mt_message)
            if not success:
                print error
                self.assertFalse(success)
        while len(self.modem.mt_msg_queue) > 0:
            pass
        self.assertTrue(len(self.modem.mt_msg_queue) == 0)

    def cb_get_mt_message(self, message):
        print("TEST CASE {} MT message retrieved: {}".format(self.test_case, vars(message)))
        self.mt_messages = self.modem.mt_msg_queue
        if len(self.mt_messages) == 0:
            self.new_mt_messages = False


def suite():
    # TODO: set up test suite(s)
    pass


if __name__ == '__main__':
    # runner = unittest.TextTestRunner()
    # runner.run(suite())
    unittest.main()
