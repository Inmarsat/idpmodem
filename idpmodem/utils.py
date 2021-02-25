"""Helpers for operating a headless device for example Raspberry Pi.

"""

import inspect
import logging
from logging import Logger, INFO
from logging.handlers import RotatingFileHandler
from threading import Thread, Event
from time import gmtime, time
from typing import Callable, Tuple, Union

import serial.tools.list_ports as list_ports
import queue


class RepeatingTimer(Thread):
    """A repeating thread to call a function, can be stopped/restarted/changed.
    
    Embedded tasks can use threading for continuous repeated operations.  A
    *RepeatingTimer* can be started, stopped, restarted and reconfigured.

    A Thread that counts down seconds using sleep increments, then calls back 
    to a function with any provided arguments.
    Optional auto_start feature starts the thread and the timer, in this case 
    the user doesn't need to explicitly start() then start_timer().

    Attributes:
        name (str): An optional descriptive name for the Thread.
        interval (int): Repeating timer interval in seconds (0=disabled).
        sleep_chunk (float): The fraction of seconds between processing ticks.

    """
    def __init__(self,
                 seconds: int,
                 target: Callable,
                 args: Tuple = (),
                 kwargs: dict = {},
                 name: str = None,
                 logger: Logger = None,
                 log_level: int = INFO,
                 sleep_chunk: float = 0.25,
                 max_drift: int = None,
                 auto_start: bool = False,
                 defer: bool = True,
                 debug: bool = False,
                 daemon = True):
        """Sets up a RepeatingTimer thread.

        Args:
            seconds: Interval for timer repeat.
            target: The function to execute each timer expiry.
            args: Positional arguments required by the target.
            name: Optional thread name.
            logger: Optional external logger to use.
            sleep_chunk: Tick seconds between expiry checks.
            max_drift: Number of seconds clock drift to tolerate.
            auto_start: Starts the thread and timer when created.
            defer: Set if first target waits for timer expiry.
            verbose_debug: verbose logging of tick count
            kwargs: Optional keyword arguments to pass into the target.

        Raises:
            ValueError if seconds is not an integer.
        """
        if not (isinstance(seconds, int) and seconds >= 0):
            err_str = 'RepeatingTimer seconds must be integer >= 0'
            raise ValueError(err_str)
        super().__init__(daemon=daemon)
        self.name = name or '{}_timer_thread'.format(str(target))
        self._log = logger or get_wrapping_logger(name=self.name,
                                                  log_level=log_level)
        self.interval = seconds
        if target is None:
            self._log.warning('No target specified for RepeatingTimer {}'
                              .format(self.name))
        self.target = target
        self._exception = None
        self.args = args
        self.kwargs = kwargs
        self.sleep_chunk = sleep_chunk
        self._defer = defer
        self._debug = debug
        self._terminate_event = Event()
        self._start_event = Event()
        self._reset_event = Event()
        self._count = self.interval / self.sleep_chunk
        self._timesync = time()
        self.max_drift = max_drift
        if auto_start:
            self.start()
            self.start_timer()

    @property
    def sleep_chunk(self):
        return self._sleep_chunk

    @sleep_chunk.setter
    def sleep_chunk(self, value: float):
        if 1 % value != 0:
            raise ValueError('1 must be a multiple of sleep_chunk')
        self._sleep_chunk = value

    def _resync(self, max_drift: int = None) -> int:
        """Used to adjust the next countdown to account for drift.
        
        Untested.
        """
        if max_drift is not None:
            drift = time() - self._timesync % self.interval
            max_drift = 0 if max_drift < 1 else max_drift
            if drift > max_drift:
                self._log.warning('Detected drift of {}s'.format(drift))
                return drift
        return 0

    def run(self):
        """*Note: runs automatically, not meant to be called explicitly.*
        
        Counts down the interval, checking every ``sleep_chunk`` for expiry.
        """
        while not self._terminate_event.is_set():
            while (self._count > 0
                   and self._start_event.is_set()
                   and self.interval > 0):
                if self._debug:
                    if (self._count * self.sleep_chunk
                        - int(self._count * self.sleep_chunk)
                        == 0.0):
                        #: log debug message at reasonable interval
                        self._log.debug('{} countdown: {} ({}s @ step {})'
                                        .format(self.name,
                                        self._count,
                                        self.interval,
                                        self.sleep_chunk))
                if self._reset_event.wait(self.sleep_chunk):
                    self._reset_event.clear()
                    self._count = self.interval / self.sleep_chunk
                self._count -= 1
                if self._count <= 0:
                    try:
                        self.target(*self.args, **self.kwargs)
                        drift_adjust = (self.interval
                                        - self._resync(self.max_drift))
                        self._count = drift_adjust / self.sleep_chunk
                    except BaseException as e:
                        self._exception = e

    def start_timer(self):
        """Initially start the repeating timer."""
        self._timesync = time()
        if not self._defer and self.interval > 0:
            self.target(*self.args, **self.kwargs)
        self._start_event.set()
        if self.interval > 0:
            self._log.info('{} timer started ({} seconds)'.format(
                           self.name, self.interval))
        else:
            self._log.warning('{} timer will not trigger (interval 0)'.format(
                              self.name))

    def stop_timer(self):
        """Stop the repeating timer."""
        self._start_event.clear()
        self._log.info('{} timer stopped ({} seconds)'
                       .format(self.name, self.interval))
        self._count = self.interval / self.sleep_chunk

    def restart_timer(self):
        """Restart the repeating timer (after an interval change)."""
        if not self._defer and self.interval > 0:
            self.target(*self.args, **self.kwargs)
        if self._start_event.is_set():
            self._reset_event.set()
        else:
            self._start_event.set()
        if self.interval > 0:
            self._log.info('{} timer restarted ({} seconds)'.format(
                           self.name, self.interval))
        else:
            self._log.warning('{} timer will not trigger (interval 0)'.format(
                              self.name))

    def change_interval(self, seconds: int):
        """Change the timer interval and restart it.
        
        Args:
            seconds (int): The new interval in seconds.
        
        Raises:
            ValueError if seconds is not an integer.

        """
        if (isinstance(seconds, int) and seconds >= 0):
            self._log.info('{} timer interval changed (old:{} s new:{} s)'
                           .format(self.name, self.interval, seconds))
            self.interval = seconds
            self._count = self.interval / self.sleep_chunk
            self.restart_timer()
        else:
            err_str = 'RepeatingTimer seconds must be integer >= 0'
            self._log.error(err_str)
            raise ValueError(err_str)

    def terminate(self):
        """Terminate the timer. (Cannot be restarted)"""
        self.stop_timer()
        self._terminate_event.set()
        self._log.info('{} timer terminated'.format(self.name))
    
    def join(self):
        super(RepeatingTimer, self).join()
        if self._exception:
            raise self._exception
        return self.target


class SearchableQueue(object):
    """Mimics relevant FIFO queue functions to avoid duplicate commands.
    
    Makes use of queue Exceptions to mimic a standard queue.

    Attributes:
        max_size: The maximum queue depth.
    """
    def __init__(self, max_size=100):
        self._queue = []
        self.max_size = max_size
    
    def contains(self, item):
        """Returns true if the queue contains the item."""
        for i in self._queue:
            if i == item: return True
        return False

    def put(self, item, index=None):
        """Adds the item to the queue.
        
        Args:
            item: The object to add to the queue.
            index: The queue position (None=end)
        """
        if len(self._queue) > self.max_size:
            raise queue.Full
        if index is None:
            self._queue.append(item)
        else:
            self._queue.insert(index, item)

    def put_exclusive(self, item):
        """Adds the item to the queue only if unique in the queue.
        
        Args:
            item: The object to add to the queue.
        
        Raises:
            queue.Full if a duplicate item is in the queue.
        """
        if not self.contains(item):
            self.put(item)
        else:
            raise queue.Full('Duplicate item in queue')

    def get(self):
        """Pops the first item from the queue.
        
        Returns:
            An object from the queue.
        
        Raises:
            queue.Empty if nothing in the queue.
        """
        if len(self._queue) > 0:
            return self._queue.pop(0)
        else:
            raise queue.Empty
    
    def qsize(self):
        """Returns the current size of the queue."""
        return len(self._queue)
    
    def empty(self):
        """Returns true if the queue is empty."""
        return len(self._queue) == 0


def is_logger(log: object) -> bool:
    """"Returns true if the object is a logger.
    
    Intended to be used by importing module without directly importing logging.
    """
    return isinstance(log, Logger)


def is_log_handler(logger: Logger, handler: object) -> bool:
    """Returns true if the handler is found in the logger.
    
    Args:
        logger: the logger parent of the handler
        handler: the handler to validate
    
    Returns:
        True if the handler is in the logger.

    """
    if not is_logger(logger):
        return False
    found = False
    for h in logger.handlers:
        if h.name == handler.name:
            found = True
            break
    return found


def get_caller_name(depth: int = 2,
                    mod: bool = True,
                    cls: bool =False,
                    mth: bool = False):
    """Returns the name of the calling function.

    For debugging/logging it is often helpful to have a sense of the parentage/flow 
    indicated by *caller_name*.
    
    Args:
        depth: Starting depth of stack inspection.
        mod: Include module name.
        cls: Include class name.
        mth: Include method name.
    
    Returns:
        Name (string) including module[.class][.method]

    """
    stack = inspect.stack()
    start = 0 + depth
    if len(stack) < start + 1:
        return ''
    parent_frame = stack[start][0]
    name = []
    module = inspect.getmodule(parent_frame)
    if module and mod:
        name.append(module.__name__)
    if cls and 'self' in parent_frame.f_locals:
        name.append(parent_frame.f_locals['self'].__class__.__name__)
    if mth:
        codename = parent_frame.f_code.co_name
        if codename != '<module>':
            name.append(codename)
    del parent_frame, stack
    return '.'.join(name)


def get_key_by_value(dictionary: dict, value: any) -> str:
    """Returns the key of the first matching value in the dictionary.
    
    Args:
        dictionary: the dictionary being searched
        value: the value being searched for
    
    Returns:
        The first key with matching value
    
    Raises:
        ValueError if value not found

    """
    for k, v in dictionary:
        if v == value:
            return k
    raise ValueError('Value {} not found in dictionary'.format(value))


def get_wrapping_logger(name: str = None,
                        filename: str = None,
                        file_size: int = 5,
                        max_files: int = 2,
                        debug: bool = False,
                        log_level: int = logging.INFO,
                        **kwargs) -> Logger:
    """Sets up a wrapping logger that writes to console and optionally a file.

    Logging is crucial for debugging embedded systems.  However you don't want
    to fill up an embedded drive with log files, so a *wrapping_logger* is
    useful that:

        * Initializes logging to console, and optionally a CSV formatted file
        * Log file wraps at a given maximum size (default 5 MB)
        * Is easy to configure a logging level (default INFO)
        * Uses UTC/GMT/Zulu timestamps
        * Provides a standardized CSV format
            * ``timestamp,[level],(thread),module.function:line,message``

    Args:
        name: Name of the logger (if None, uses name of calling module).
        filename: (optional) Name of the file/path if writing to a file.
        file_size: Max size of the file in megabytes, before wrapping.
        max_files: The maximum number of files in rotation.
        debug: *backward compatible* enable DEBUG logging
        log_level: A logging level (default INFO)
        kwargs: Optional overrides for RotatingFileHandler
    
    Returns:
        A logger with console stream handler and (optional) file handler.

    """
    FORMAT = ('%(asctime)s.%(msecs)03dZ,[%(levelname)s],(%(threadName)-10s),'
              '%(module)s.%(funcName)s:%(lineno)d,%(message)s')
    log_formatter = logging.Formatter(fmt=FORMAT,
                                      datefmt='%Y-%m-%dT%H:%M:%S')
    log_formatter.converter = gmtime

    if name is None:
        name = get_caller_name()
    logger = logging.getLogger(name)

    if debug or logger.getEffectiveLevel() == logging.DEBUG:
        log_lvl = logging.DEBUG
    else:
        log_lvl = log_level
    logger.setLevel(log_lvl)
    #: Set up log file
    if filename is not None:
        # TODO: validate that logfile is a valid path/filename
        mode = 'a'
        max_bytes = int(file_size * 1024 * 1024)
        backup_count = max_files
        encoding = None
        delay = 0
        for kw in kwargs:
            if kw == 'backupCount':
                backup_count = kwargs[kw]
            elif kw == 'delay':
                delay = kwargs[kw]
            elif kw == 'encoding':
                encoding = kwargs[kw]
            elif kw == 'mode':
                mode = kwargs[kw]
            elif kw == 'maxBytes':
                max_bytes = kwargs[kw]
        file_handler = RotatingFileHandler(
            filename=filename,
            mode=mode,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            delay=delay)
        file_handler.name = name + '_file_handler'
        file_handler.setFormatter(log_formatter)
        file_handler.setLevel(log_lvl)
        if not is_log_handler(logger, file_handler):
            logger.addHandler(file_handler)
    #: Set up console log
    console_handler = logging.StreamHandler()
    console_handler.name = name + '_console_handler'
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(log_lvl)
    if not is_log_handler(logger, console_handler):
        logger.addHandler(console_handler)

    return logger


def validate_serial_port(target: str, verbose: bool = False) -> Union[bool, tuple]:
    """Validates a given serial port as available on the host.

    When working with different OS and platforms, using a serial port to connect
    to a modem can be simplified by *validate_serial_port*.

    If target port is not found, a list of available ports is returned.
    Labels known FTDI and Prolific serial/USB drivers.

    Args:
        target: Target port name e.g. ``/dev/ttyUSB0``
    
    Returns:
        True or False if detail is False
        (valid: bool, description: str) if detail is True
    """
    found = False
    detail = ''
    ser_ports = [tuple(port) for port in list(list_ports.comports())]
    for port in ser_ports:
        if target == port[0]:
            found = True
            usb_id = str(port[2])
            if 'USB VID:PID=0403:6001' in usb_id:
                driver = 'Serial FTDI FT232 (RS485/RS422/RS232)'
            elif 'USB VID:PID=067B:2303' in usb_id:
                driver = 'Serial Prolific PL2303 (RS232)'
            else:
                driver = 'Serial vendor/device {}'.format(usb_id)
            detail = '{} on {}'.format(driver, port[0])
    if not found and len(ser_ports) > 0:
        for port in ser_ports:
            if len(detail) > 0:
                detail += ','
            detail += " {}".format(port[0])
        detail = 'Available ports:' + detail
    return (found, detail) if verbose else found


if __name__ == '__main__':
    print('This module is not meant to be run directly.')
