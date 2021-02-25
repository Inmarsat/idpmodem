#!/usr/bin/python3
"""Basic test cases for utils."""

# TODO: adapt for unittest

import os
from time import sleep
# import unittest

from idpmodem.utils import RepeatingTimer, get_wrapping_logger, is_logger, is_log_handler, get_caller_name, validate_serial_port

'''
class UtilsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repeating_timer_count = 0

    def repeating_timer_callable(self):
        print('repeating timer triggered')
        self.repeating_timer_count +=1

    def test_01_repeating_timer_basic(self):
        # TODO: not working, not sure why...
        test_timer = RepeatingTimer(seconds=2, target=self.repeating_timer_callable)
        test_timer.start()
        test_timer.start_timer()
        while self.repeating_timer_count < 3:
            pass
        self.assertTrue(True)

    def test_02_get_wrapping_logger(self):
        pass

    def test_03_is_logger(self):
        pass

    def test_04_is_log_handler(self):
        pass

    def test_05_get_caller_name(self):
        pass

    def test_06_validate_serial_port(self):
        pass


def suite():
    suite = unittest.TestSuite()
    available_tests = unittest.defaultTestLoader.getTestCaseNames(UtilsTestCase)
    tests = [
        'test_01_repeating_timer',
        # Add test cases above as strings or leave empty to test all cases
    ]
    if len(tests) > 0:
        for test in tests:
            for available_test in available_tests:
                if test in available_test:
                    suite.addTest(UtilsTestCase(available_test))
    else:
        for available_test in available_tests:
            suite.addTest(UtilsTestCase(available_test))
    return suite


if __name__ == '__main__':
    runner = unittest.TextTestRunner()
    runner.run(suite())
'''

global log


def test_log(arg, kwarg=None):
    global log
    caller_name = get_caller_name(cls=True, mth=True)
    log.info('{} testing utilities with {}'.format(caller_name, arg))
    if kwarg is not None:
        log.warning('received kwarg: {}'.format(kwarg))


def main():
    global log
    dir_path = os.path.dirname(os.path.realpath(__file__))
    filename = dir_path + '/test_utils.log'
    arg = 'test_arg'
    kwargs = {'kwarg': True}
    test_autostart = True
    daemon = True
    log = get_wrapping_logger(filename=filename,
                              file_size=0.001,
                              # debug=True,
                              )
    loop = RepeatingTimer(seconds=2,
                          target=test_log,
                          args=(arg,),
                          kwargs=kwargs,
                          name='test',
                          auto_start=test_autostart,
                          defer=False,
                          daemon=daemon)
    if not test_autostart:
        loop.start()
        loop.start_timer()
    valid, desc = validate_serial_port('COM1', verbose=True)
    del valid   #: unused
    log.info(desc)
    cycles = 0
    while cycles < 5:
        #: TODO loop until 2 files are full
        cycles += 1
        sleep(1)
    if not daemon:
        loop.terminate()


if __name__ == '__main__':
    main()
