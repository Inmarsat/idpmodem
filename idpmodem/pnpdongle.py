"""Class module for the Inmarsat IDP Plug-N-Play Developer Kit Dongle.
"""

from __future__ import absolute_import

from asyncio import AbstractEventLoop, gather, run
from queue import Queue
from queue import Empty as QueueEmpty
from atexit import register as on_exit
from logging import Logger, INFO, DEBUG
from time import sleep
from typing import Callable

try:
    from gpiozero import DigitalInputDevice, DigitalOutputDevice
except ImportError as e:
    raise Exception('Missing dependency {}'.format(e))

from .atcommand_async import IdpModemAsyncioClient
from .constants import NOTIFICATION_BITMASK, FORMAT_B64, CONTROL_STATES, BEAMSEARCH_STATES
from .utils import get_wrapping_logger


class PnpDongle:
    """Represents the Raspberry Pi Zero W dongle for IDP modem communications.

    Attributes:
        mode: The mode of communication with the IDP modem (ST2100)
                `master` allows the Pi0W to communicate
                `transparent` allows a separate device to communicate
                `proxy` allows the Pi0W UART to intercept modem output then
                send to the separate device
        modem: The IsatData Pro modem (ST2100)

    """
    
    EVENT_NOTIFY = 9
    PPS_PULSE = 10
    EXTERNAL_RESET = 11
    MODEM_RESET = 26
    RL1A_DIR = 27
    RL1B_DIR = 22
    RL2A_DIR = 23
    RL2B_DIR = 24
    TRS3221E_ON = 7
    TRS3221E_OFF = 8
    TRS3221E_INVALID_NOT = 25

    MODES = ['master', 'proxy', 'transparent']

    def __init__(self,
                 logger: Logger = None,
                 log_level: int = INFO,
                 modem_event_callback: Callable = None,
                 external_reset_callback: Callable = None,
                 pps_pulse_callback: Callable = None,
                 mode: str = 'master',
                 modem_crc: bool = False,
                 loop: AbstractEventLoop = None):
        """Initializes the dongle.
        
        Args:
            logger: Optional logger, one will be created if not supplied.
            log_level: The default logging level.
            modem_event_callback: Optional callback when notification asserts.
            external_reset_callback: Optional callback triggered by remote reset.
            pps_pulse_callback: Optional receiver for GNSS pulse per second.
            mode: `master`, `proxy` or `transparent`.
            modem_crc: Enables CRC-16 on modem interactions.
            
        """
        on_exit(self._cleanup)
        self._logger = logger or get_wrapping_logger(log_level=log_level)
        self._gpio_rl1a = DigitalOutputDevice(pin=self.RL1A_DIR,
                                              initial_value=None)
        self._gpio_rl1b = DigitalOutputDevice(pin=self.RL1B_DIR,
                                              initial_value=None)
        self._gpio_rl2a = DigitalOutputDevice(pin=self.RL2A_DIR,
                                              initial_value=None)
        self._gpio_rl2b = DigitalOutputDevice(pin=self.RL2B_DIR,
                                              initial_value=None)
        self._gpio_232on = DigitalOutputDevice(pin=self.TRS3221E_ON,
                                               initial_value=None)
        self._gpio_232offnot = DigitalOutputDevice(pin=self.TRS3221E_OFF,
                                                   initial_value=None)
        self._gpio_232valid = DigitalInputDevice(pin=self.TRS3221E_INVALID_NOT,
                                                 pull_up=None,
                                                 active_state=True)
        # self._gpio_232valid.when_activated = self._rs232valid
        self._gpio_modem_event = DigitalInputDevice(pin=self.EVENT_NOTIFY,
                                                    pull_up=None,
                                                    active_state=True)
        self.modem_event_callback = modem_event_callback
        self._gpio_modem_event.when_activated = (
            modem_event_callback or self._event_activated)
        self._event_data_last = None
        self._gpio_modem_reset = DigitalOutputDevice(pin=self.MODEM_RESET,
                                                     initial_value=False)
        self._gpio_external_reset = DigitalInputDevice(pin=self.EXTERNAL_RESET,
                                                       pull_up=None,
                                                       active_state=True)
        self._gpio_external_reset.when_activated = (
            external_reset_callback)
        self._gpio_pps_pulse = DigitalInputDevice(pin=self.PPS_PULSE,
                                                  pull_up=None,
                                                  active_state=True)
        self._gpio_pps_pulse.when_activated = pps_pulse_callback
        if pps_pulse_callback:
            self.pps_enable()
        self.mode = None
        self.mode_set(mode)
        self.loop = loop
        self.event_queue = Queue()
        self.modem = IdpModemAsyncioClient(port='/dev/ttyS0',
                                           crc=modem_crc,
                                           logger=self._logger,
                                           loop=self.loop)
    
    def _cleanup(self):
        """Resets the dongle to transparent mode and enables RS232 shutdown."""
        self._logger.debug('Reverting to transparent mode' +
                           ' and RS232 auto-shutdown')
        self.mode_set(mode='transparent')

    def _rs232valid(self):
        """Detects reception of RS232 data."""
        self._logger.debug('RS232 data received')

    def mode_set(self, mode: str = 'master'):
        """Configures the dongle (default: master)
        
        Args:
            mode: `master` or `transparent`
        
        Raises:
            Exception if mode is unsupported.
        
        """
        if mode not in self.MODES:
            raise Exception('Unsupported mode: {}'.format(mode))
        self._logger.debug('Setting Raspberry Pi UART as {}'.format(mode))
        self.mode = mode
        if mode == 'master':
            self._logger.debug('Energizing RL1A/RL1B')
            self._gpio_rl1a.blink(n=1, background=False)
            self._logger.debug('Forcing on RS232')
            self._gpio_232on.on()
        elif mode == 'transparent':
            self._logger.debug('Resetting RL1')
            self._gpio_rl1b.blink(n=1, background=False)
            self._logger.debug('Resetting RL2')
            self._gpio_rl2b.blink(n=1, background=False)
            self._logger.debug('Enabling RS232 auto-shutdown')
            self._gpio_232on.off()
        else:   #: mode == 'proxy'
            self._logger.debug('Resetting RL1')
            self._gpio_rl1b.blink(n=1, background=False)
            self._logger.debug('Energizing RL2A')
            self._gpio_rl2a.blink(n=1, background=False)
            self._logger.debug('Forcing on RS232')
            self._gpio_232on.on()
        sleep(0.25)

    def _event_activated(self):
        """Queues an event triggered by modem event notification pin.
        
        Spawns a dummy thread to query which notifications asserted the pin
        and stores in self.event_queue.

        """
        self._logger.debug('Modem event notification asserted')
        notifications = run(self.modem.lowpower_notifications_check())
        for notification in notifications:
            self._logger.info('Notification: {}'.format(notification))
            self.event_queue.put_nowait(notification)
    
    def _process_event_queue(self):
        self._logger.debug('Processing event queue')
        try:
            event_type = self.event_queue.get()
            if event_type == 'message_mt_received':
                messages = self.process_message_mt_waiting()
                for message in messages:
                    self._logger.info('Message received: {}'.format(message))
            elif event_type == 'message_mo_complete':
                messages = self.process_message_mo_complete()
                for message in messages:
                    if message['state'] > 5:
                        self._logger.info('Message completed: {}'.format(message))
            elif event_type == 'event_cached':
                changed = False
                event_data = self.process_event_cached()
                if self._event_data_last is not None:
                    for event in event_data:
                        if event not in self._event_data_last:
                            changed = True
                            self._logger.info('New event cached: {}'.format(event))
                if changed or self._event_data_last is None:
                    self._event_data_last = event_data
        except QueueEmpty:
            pass

    def process_message_mt_waiting(self):
        self._logger.debug('Request to process forward message event')
        messages = None
        messages_waiting = run(self.modem.message_mt_waiting())
        if not isinstance(messages_waiting, list):
            self._logger.warning('No MT messages waiting')
        else:
            messages = []
            for meta in messages_waiting:
                message = run(self.modem.message_mt_get(name=meta['name'],
                                                        data_format=FORMAT_B64,
                                                        verbose=True))
                messages.append(message)
        return messages

    def process_message_mo_complete(self):
        self._logger.debug('Request to process return message event')
        messages_queued = run(self.modem.message_mo_state())
        if not isinstance(messages_queued, list):
            self._logger.warning('No MO messages queued or completed')
        return messages_queued

    def process_event_cached(self, class_subclass: tuple = None):
        self._logger.debug('Request to process cached modem event')
        event_data = None
        if class_subclass is not None:
            event_data = [run(self.modem.event_get(class_subclass))]
        else:
            event_data = []
            events_available = run(self.modem.event_monitor_get())
            for event in events_available:
                if event.endswith('*'):
                    tup = event.replace('*', '').split('.')
                    if tup == (3, 1):
                        event_data.append(self.satellite_status)
                    else:
                        event_data.append(run(self.modem.event_get(event=tup)))
        return event_data

    def satellite_status(self):
        ctrl_state, c_n, beamsearch_state = run(self.modem.satellite_status())
        return {
            'ctrl_state': CONTROL_STATES[ctrl_state],
            'c_n': c_n,
            'beamsearch_state': BEAMSEARCH_STATES[beamsearch_state]
        }

    def modem_reset(self):
        """Resets the IDP modem."""
        self._logger.warning('Resetting IDP modem')
        self._gpio_modem_reset.blink(n=1, background=False)
    
    def pps_enable(self, enable=True):
        """Enables 1 pulse-per-second GNSS time output from the IDP modem."""
        self._logger.info('{} pulse per second IDP modem output'.format(
            'Enabling' if enable else 'Disabling'))
        response = run(self.modem.command('AT%TRK={}'.format(
            1 if enable else 0)))
        if response[0] == 'OK':
            return True
        self._logger.error('Failed to {} 1s GNSS update'.format(
            'enable' if enable else 'disable'))
        return False
