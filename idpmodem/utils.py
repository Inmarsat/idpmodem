"""Helpers for operating a headless device for example Raspberry Pi.

Logging is crucial for debugging embedded systems.  However you don't want
to fill up an embedded drive with log files, so a *wrapping_logger* is useful.

For debugging/logging it is often helpful to have a sense of the parentage/flow 
indicated by *caller_name*.

Embedded tasks can use threading for repeated operations.  A
*RepeatingTimer* can be started, stopped, restarted and reconfigured.

When working with different OS and platforms, using a serial port to connect to 
a modem can be simplified by *validate_serial_port*.
"""

import inspect
import logging
from logging.handlers import RotatingFileHandler
from threading import Thread, Event
from time import gmtime
from typing import Callable, Union

import serial.tools.list_ports as list_ports
try:
    import queue
except ImportError:
    import Queue as queue


class RepeatingTimer(Thread):
    """A repeating thread to call a function, can be stopped/restarted/changed.
    
    A Thread that counts down seconds using sleep increments, then calls back 
    to a function with any provided arguments.
    Optional auto_start feature starts the thread and the timer, in this case 
    the user doesn't need to explicitly start() then start_timer().

    Attributes:
        interval: Repeating timer interval in seconds (0=disabled).
        log: A logger, with name inherited from calling function.
        defer: Set (default=True) if the function call waits
            for the first cycle expiry before calling back.
        sleep_chunk: The fraction of seconds between processing ticks.
    """
    def __init__(self,
                 seconds: int,
                 target: Callable,
                 *args,
                 name: str = None,
                 log: object = None,
                 sleep_chunk: float = 0.25,
                 auto_start: bool = False,
                 defer: bool = True,
                 debug: bool = False,
                 **kwargs):
        """Sets up a RepeatingTimer thread.

        Args:
            seconds: Interval for timer repeat.
            target: The function to execute each timer expiry.
            args: Positional arguments required by the target.
            name: Optional thread name.
            log: Optional external logger to use.
            sleep_chunk: Tick seconds between expiry checks.
            auto_start: Starts the thread and timer when created.
            defer: Set if first target waits for timer expiry.
            debug: verbose logging of tick count
            kwargs: Optional keyword arguments to pass into the target.

        Raises:
            ValueError if seconds is not an integer.
        """
        if not (isinstance(seconds, int) and seconds >= 0):
            err_str = 'RepeatingTimer seconds must be integer >= 0'
            raise ValueError(err_str)
        super(RepeatingTimer, self).__init__()
        if name is not None:
            self.name = name
        else:
            self.name = '{}_timer_thread'.format(str(target))
        if is_logger(log):
            self.log = log
        else:
            self.log = get_wrapping_logger(name=self.name, debug=debug)
        self.interval = seconds
        if target is None:
            self.log.warning('No target specified for RepeatingTimer {}'
                             .format(self.name))
        self._target = target
        self._exception = None
        self._args = args
        self._kwargs = kwargs
        self.sleep_chunk = sleep_chunk
        self._defer = defer
        self._debug = debug
        self._terminate_event = Event()
        self._start_event = Event()
        self._reset_event = Event()
        self._count = self.interval / self.sleep_chunk
        if auto_start:
            self.start()
            self.start_timer()

    def run(self):
        """Counts down the interval, checking every sleep_chunk for expiry."""
        while not self._terminate_event.is_set():
            while (self._count > 0
                and self._start_event.is_set()
                and self.interval > 0):
                if self._debug:
                    if (self._count * self.sleep_chunk
                        - int(self._count * self.sleep_chunk)
                        == 0.0):
                        #: log debug message at reasonable interval
                        self.log.debug('{} countdown: {} ({}s @ step {})'
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
                        self._target(*self._args, **self._kwargs)
                        self._count = self.interval / self.sleep_chunk
                    except BaseException as e:
                        self._exception = e

    def start_timer(self):
        """Initially start the repeating timer."""
        if not self._defer and self.interval > 0:
            self._target(*self._args, **self._kwargs)
        self._start_event.set()
        if self.interval > 0:
            self.log.info('{} timer started ({} seconds)'.format(
                          self.name, self.interval))
        else:
            self.log.warning('{} timer will not trigger (interval 0)'.format(
                             self.name))

    def stop_timer(self):
        """Stop the repeating timer."""
        self._start_event.clear()
        self.log.info('{} timer stopped ({} seconds)'
                      .format(self.name, self.interval))
        self._count = self.interval / self.sleep_chunk

    def restart_timer(self):
        """Restart the repeating timer (after an interval change)."""
        if not self._defer and self.interval > 0:
            self._target(*self._args, **self._kwargs)
        if self._start_event.is_set():
            self._reset_event.set()
        else:
            self._start_event.set()
        if self.interval > 0:
            self.log.info('{} timer restarted ({} seconds)'.format(
                          self.name, self.interval))
        else:
            self.log.warning('{} timer will not trigger (interval 0)'.format(
                             self.name))

    def change_interval(self, seconds):
        """Change the timer interval and restart it.
        
        Args:
            seconds (int): The new interval in seconds.
        
        Raises:
            ValueError if seconds is not an integer.

        """
        if (isinstance(seconds, int) and seconds >= 0):
            self.log.info('{} timer interval changed (old:{} s new:{} s)'
                          .format(self.name, self.interval, seconds))
            self.interval = seconds
            self._count = self.interval / self.sleep_chunk
            self.restart_timer()
        else:
            err_str = 'RepeatingTimer seconds must be integer >= 0'
            self.log.error(err_str)
            raise ValueError(err_str)

    def terminate(self):
        """Terminate the timer. (Cannot be restarted)"""
        self.stop_timer()
        self._terminate_event.set()
        self.log.info('{} timer terminated'.format(self.name))
    
    def join(self):
        super(RepeatingTimer, self).join()
        if self._exception:
            raise self._exception
        return self._target


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
    return isinstance(log, logging.Logger)


def is_log_handler(logger: logging.Logger, handler: object) -> bool:
    """Returns true if the handler is found in the logger.
    
    Args:
        logger (logging.Logger)
        handler (logging handler)
    
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
                        debug: bool = False,
                        log_level: int = logging.INFO,
                        **kwargs):
    """Sets up a wrapping logger that writes to console and optionally a file.

    Initializes logging to console, and optionally a CSV formatted file.
    CSV format: timestamp,[level],(thread),module.function:line,message
    Default logging level is INFO.
    Timestamps are UTC/GMT/Zulu.

    Args:
        name: Name of the logger (if None, uses name of calling module).
        filename: Name of the file/path if writing to a file.
        file_size: Max size of the file in megabytes, before wrapping.
        debug: enable DEBUG logging (default INFO)
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
        backup_count = 2
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

    If target port is not found, a list of available ports is returned.
    Labels known FTDI and Prolific serial/USB drivers.

    Args:
        target: Target port name e.g. '/dev/ttyUSB0'
    
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
