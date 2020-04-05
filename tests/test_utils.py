#!/usr/bin/python3
"""Basic test cases for utils."""

import os 
from time import sleep

try:
    from utils import get_wrapping_logger, RepeatingTimer, validate_serial_port
    from utils import get_caller_name
except ImportError:
    from idpmodem.utils import get_wrapping_logger, RepeatingTimer
    from idpmodem.utils import validate_serial_port, get_caller_name


global log


def test_log(arg, kwarg=None):
    global log
    caller_name = get_caller_name(cls=True, mth=True)
    log.info('{} testing utilities with {}'.format(caller_name, arg))
    if kwarg is not None:
        log.warning('received kwarg {}'.format(kwarg))


def main():
    global log
    dir_path = os.path.dirname(os.path.realpath(__file__))
    filename = dir_path + '/test_utils.log'
    arg = 'test_arg'
    test_autostart = True
    log = get_wrapping_logger(
                              filename=filename,
                              file_size=0.001,
                              # debug=True,
                              )
    loop = RepeatingTimer(
                        2,   #: If not using positional arg, seconds=
                        test_log,  #: If not using positional arg, callback=
                        arg,   #: Test positional args in callback
                        name='test_utils',
                        auto_start=test_autostart,
                        defer=False,
                        kwarg='test_kwarg',   #: Test kwargs in callback
                        )
    if not test_autostart:
        loop.start()
        loop.start_timer()
    valid, desc = validate_serial_port('COM1')
    del valid   #: unused
    log.info(desc)
    cycles = 0
    while cycles < 25:
        #: TODO loop until 2 files are full
        cycles += 1
        sleep(1)
    loop.terminate()


if __name__ == '__main__':
    main()
