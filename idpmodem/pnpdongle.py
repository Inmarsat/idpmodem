"""Class module for the Inmarsat IDP Plug-N-Play Developer Kit Dongle.
"""

from __future__ import absolute_import

# from atexit import register as on_exit
from logging import Logger
from typing import Callable

try:
    from gpiozero import DigitalInputDevice, DigitalOutputDevice
except ImportError:
    raise Exception('Missing dependency')

from .atcommand_async import IdpModemAsyncioClient
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
    RL1A_CTRL = 27
    RL1B_CTRL = 22
    RL2A_CTRL = 23

    MODES = {
        'master': {'rl1a': 'ON', 'rl1b': 'OFF', 'rl2a': 'OFF'},
        'transparent': {'rl1a': 'OFF', 'rl1b': 'OFF', 'rl2a': 'OFF'},
        'proxy': {'rl1a': 'OFF', 'rl1b': 'OFF', 'rl2a': 'ON'}
    }

    def __init__(self,
                 logger: Logger = None,
                 modem_event_callback: Callable = None,
                 external_reset_callback: Callable = None,
                 pps_pulse_callback: Callable = None,
                 mode: str = 'master'):
        """Initializes the dongle."""
        self._logger = logger or get_wrapping_logger()
        self._gpio_rl1a = DigitalOutputDevice(pin=self.RL1A_CTRL)
        self._gpio_rl1b = DigitalOutputDevice(pin=self.RL1B_CTRL)
        self._gpio_rl2a = DigitalOutputDevice(pin=self.RL2A_CTRL)
        self._gpio_modem_event = DigitalInputDevice(pin=self.EVENT_NOTIFY,
                                                    pull_up=None,
                                                    active_state=True)
        self._gpio_modem_event.when_activated = (
            modem_event_callback or self._event_activated)
        self._gpio_modem_reset = DigitalOutputDevice(pin=self.MODEM_RESET)
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
        self.modem = IdpModemAsyncioClient(port='/dev/ttyAMA0',
                                           crc=True,
                                           logger=self._logger)
        self.modem.lowpower_notifications_enable()
    
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
        if self.MODES[mode]['rl1a'] == 'ON':
            self._gpio_rl1a.on()
        else:
            self._gpio_rl1a.off()
        if self.MODES[mode]['rl1b'] == 'ON':
            self._gpio_rl1b.on()
        else:
            self._gpio_rl1b.off()
        if self.MODES[mode]['rl2a'] == 'ON':
            self._gpio_rl2a.on()
        else:
            self._gpio_rl2a.off()

    def _event_activated(self):
        self._logger.info('Modem event notification asserted')
        notifications = self.modem.lowpower_notification_check()
        for notification in notifications:
            self._logger.debug('Notification: {}'.format(notification))
    
    def modem_reset(self):
        """Resets the IDP modem."""
        self._logger.warning('Resetting IDP modem')
        self._gpio_modem_reset.blink(n=1)
    
    def pps_enable(self, enable=True):
        """Enables 1 pulse-per-second GNSS time output from the IDP modem."""
        self._logger.info('{} pulse per second IDP modem output'.format(
            'Enabling' if enable else 'Disabling'))
        response = self.modem.command('AT%TRK={}'.format(1 if enable else 0))
        if response[0] == 'OK':
            return True
        self._logger.error('Failed to {} 1s GNSS update'.format(
            'enable' if enable else 'disable'))
        return False


def main():
    RUN_TIME = 60   # seconds
    try:
        from time import time
        start_time = time()
        pnpdongle = PnpDongle()
        modem = pnpdongle.modem
        while time() - start_time > RUN_TIME:
            pass
    except KeyboardInterrupt:
        print('Interrupted by user input')


if __name__ == '__main__':
    main()
