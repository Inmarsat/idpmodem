#!/usr/bin/env python
"""
Periodically queries modem status

.. todo::

   * Restructure parse_args for automagic documentation with sphinx argparse extension

"""
from __future__ import absolute_import

import argparse
import binascii
import datetime
import sys
import traceback

from idpmodem import idpmodem, utils
from idpmodem.codecs import common as idpcodec
from idpmodem.utils import get_wrapping_logger
from idpmodem.idpmodem import FORMAT_TEXT, FORMAT_HEX, FORMAT_B64

__version__ = "1.1.0"


def main():
    modem = None
    try:
        modem = idpmodem.Modem(serial_name='/dev/ttyUSB1', baudrate=9600)
        while True:
            pass
    except KeyboardInterrupt:
        print('Interrupted by user')
    finally:
        if modem is not None:
            modem.terminate()


if __name__ == '__main__':
    main()
