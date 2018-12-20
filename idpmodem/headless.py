#!/usr/bin/env python
"""
Functions and classes useful for operating a headless device e.g. Raspberry Pi
"""

import time
import logging
from logging.handlers import RotatingFileHandler
import threading
import serial.tools.list_ports


def is_logger(log):
    return type(log) is logging.Logger


def get_wrapping_log(logfile=None, file_size=5, debug=False):
    """
    Initializes logging to console, and optionally a wrapping CSV formatted file of defined size.
    Default logging level is INFO.
    Timestamps are GMT/Zulu.

    :param logfile: the name of the file
    :param file_size: the max size of the file in megabytes, before wrapping occurs
    :param debug: Boolean to enable tick_log DEBUG logging (default INFO)
    :return: ``log`` object

    """
    FORMAT = ('%(asctime)s.%(msecs)03dZ,[%(levelname)s],(%(threadName)-10s),'
              '%(module)s.%(funcName)s:%(lineno)d,%(message)s')
    log_formatter = logging.Formatter(fmt=FORMAT,
                                      datefmt='%Y-%m-%dT%H:%M:%S')
    log_formatter.converter = time.gmtime
    if logfile is not None:
        logger = logging.getLogger(logfile)
        file_handler = RotatingFileHandler(logfile, mode='a', maxBytes=file_size * 1024 * 1024,
                                           backupCount=2, encoding=None, delay=0)
        file_handler.setFormatter(log_formatter)
        if not logger.handlers:
            logger.addHandler(file_handler)
    else:
        logger = logging.getLogger()
    if debug:
        log_lvl = logging.DEBUG
    else:
        log_lvl = logging.INFO
    logger.setLevel(log_lvl)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(log_lvl)
    if not logger.handlers:
        logger.addHandler(console_handler)
    return logger


class RepeatingTimer(threading.Thread):
    """
    A Thread class that repeats function calls like a Timer but can be stopped, restarted and change interval.
    Thread starts automatically on initialization, but timer must be started explicitly with ``start_timer()``.

    :param seconds: interval for timer repeat
    :param name: used to identify the thread
    :param sleep_chunk: tick cycle in seconds between state checks
    :param callback: the callback to execute each timer expiry
    :param args: optional arguments to pass into the callback
    :param kwargs: optional keyword arguments to pass into the callback (UNTESTED)
    :param tick_log: verbose logging of tick count

    """
    def __init__(self, seconds, name=None, sleep_chunk=0.25, auto_start=True, defer=True, tick_log=False,
                 callback=None, *args, **kwargs):
        """
        Initialization of the subclass.

        :param seconds: interval for timer repeat
        :param name: used to identify the thread
        :param sleep_chunk: tick cycle in seconds between state checks
        :param callback: the callback to execute each timer expiry
        :param args: **UNTESTED** optional arguments to pass into the callback
        :param kwargs: optional keyword arguments to pass into the callback (UNTESTED)
        :param tick_log: verbose logging of tick count

        """
        threading.Thread.__init__(self)
        self.log = get_wrapping_log(__name__)
        if name is not None:
            self.name = name
        else:
            self.name = str(callback) + "_timer_thread"
        self.interval = seconds
        if callback is None:
            self.log.warning("No callback specified for RepeatingTimer " + self.name)
        self.callback = callback
        self.callback_args = args
        self.callback_kwargs = kwargs
        self.sleep_chunk = sleep_chunk
        self.defer = defer
        self.tick_log = tick_log
        self.terminate_event = threading.Event()
        self.start_event = threading.Event()
        self.reset_event = threading.Event()
        self.count = self.interval / self.sleep_chunk
        if auto_start:
            self.start()

    def run(self):
        """Counts down the interval, checking every ``sleep_chunk`` the desired state."""
        if not self.defer:
            self.callback(*self.callback_args, **self.callback_kwargs)
        while not self.terminate_event.is_set():
            while self.count > 0 and self.start_event.is_set() and self.interval > 0:
                if self.tick_log:
                    if (self.count * self.sleep_chunk - int(self.count * self.sleep_chunk)) == 0.0:
                        self.log.debug("%s countdown: %d (%ds @ step %02f"
                                       % (self.name, self.count, self.interval, self.sleep_chunk))
                if self.reset_event.wait(self.sleep_chunk):
                    self.reset_event.clear()
                    self.count = self.interval / self.sleep_chunk
                self.count -= 1
                if self.count <= 0:
                    self.callback(*self.callback_args, **self.callback_kwargs)
                    self.count = self.interval / self.sleep_chunk

    def start_timer(self):
        """Initially start the repeating timer."""
        self.start_event.set()
        self.log.info("{} timer started ({} seconds)".format(self.name, self.interval))

    def stop_timer(self):
        """Stop the repeating timer."""
        self.start_event.clear()
        self.count = self.interval / self.sleep_chunk
        self.log.info("{} timer stopped ({} seconds)".format(self.name, self.interval))

    def restart_timer(self):
        """Restart the repeating timer (after an interval change)."""
        if self.start_event.is_set():
            self.reset_event.set()
        else:
            self.start_event.set()
        self.log.info("{} timer restarted ({} seconds)".format(self.name, self.interval))

    def change_interval(self, seconds):
        """Change the timer interval and restart it."""
        if isinstance(seconds, int) and seconds > 0:
            self.log.info("{} timer interval changed ({} seconds)".format(self.name, self.interval))
            self.interval = seconds
            self.restart_timer()
        else:
            self.log.error("Invalid interval requested...must be integer > 0")

    def terminate(self):
        """Terminate the timer. (Cannot be restarted)"""
        self.terminate_event.set()
        self.log.info(self.name + " timer terminated")


def validate_serial_port(target):
    """
    Validates a given serial port as available on the host.

    :param target: (string) the target port name e.g. '/dev/ttyUSB0'
    :returns:

       * (Boolean) validity result
       * (String) descriptor

    """
    found = False
    detail = ""
    ser_ports = [tuple(port) for port in list(serial.tools.list_ports.comports())]
    for port in ser_ports:
        if target == port[0]:
            found = True
            if 'USB VID:PID=0403:6001' in port[2] and target == port[0]:
                detail = "Serial FTDI FT232 (RS485/RS422/RS232) on {port}".format(port=port[0])
            elif 'USB VID:PID=067B:2303' in port[2] and target == port[0]:
                detail = "Serial Prolific PL2303 (RS232) on {port}".format(port=port[0])
            elif target == port[0]:
                usb_id = str(port[2])
                detail = "Serial vendor/device {id} on {port}".format(id=usb_id, port=port[0])
    if not found and len(ser_ports) > 0:
        for port in ser_ports:
            detail += ", {}".format(port[0]) if len(detail) > 0 else " {}".format(port[0])
        detail = "Available ports:" + detail
    return found, detail
