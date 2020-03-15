#!/usr/bin/env python
"""
Periodically queries modem status, checks for incoming messages and sends location reports

Dependencies:
  - (REQUIRED) crcxmodem.py calculates CRC-16-CCITT xmodem
  - (REQUIRED) idpmodem.py contains object definitions for the modem
  - (optional) RPi.GPIO for running headless on Raspberry Pi
  - (optional) serialportfinder.py is used when running on Windows test environment (detect COM port)

Mobile-Originated location reports are 17 bytes using SIN 255 MIN 255
Mobile-Terminated location interval change uses SIN 255 MIN 1, plus 1 byte payload for the new interval in minutes.
When a new interval is configured, a location report is generated immediately, thereafter at the new interval.

.. todo::

   * Restructure parse_args for automagic documentation with sphinx argparse extension

"""
__version__ = "1.0.1"

import datetime
import sys
import traceback
import binascii
import argparse
from .headless import get_wrapping_logger
from . import idpmodem
from . import idpcodec

# GLOBALS
global log                  # the log object used by most functions and classes
global modem                # the class instance of IDP modem object defined in 'idpmodem' module
global tracking_interval    # an interval that can be changed remotely to drive location reporting
global shutdown_flag        # a flag triggered by an interrupt from a parallel service (e.g. RPi GPIO input)

'''
class Location(object):
    """
    A class containing a specific set of location-based information for a given point in time.
    Uses 91/181 if lat/lon are unknown

    :param latitude: in 1/1000th minutes (approximately 1 m resolution)
    :param longitude: in 1/1000th minutes (approximately 1 m resolution)
    :param altitude: in metres
    :param speed: in knots
    :param heading: in degrees
    :param timestamp: in seconds since 1970-01-01T00:00:00Z
    :param satellites: in view at time of fix
    :param fixtype: None, 2D or 3D
    :param PDOP: Probability Dilution of Precision
    :param HDOP: Horizontal DOP
    :param VDOP: Vertical DOP

    """

    def __init__(self, latitude=91*60*1000, longitude=181*60*1000, altitude=0,
                 speed=0, heading=0, timestamp=0, satellites=0, fixtype=1,
                 PDOP=0, HDOP=0, VDOP=0):
        """
        Creates a Location instance with default lat/lng 91/181 *unknown*

        :param latitude: in 1/1000th minutes (approximately 1 m resolution)
        :param longitude: in 1/1000th minutes (approximately 1 m resolution)
        :param altitude: in metres
        :param speed: in knots
        :param heading: in degrees
        :param timestamp: in seconds since 1970-01-01T00:00:00Z
        :param satellites: in view at time of fix
        :param fixtype: None, 2D or 3D
        :param PDOP: Probability Dilution of Precision
        :param HDOP: Horizontal DOP
        :param VDOP: Vertical DOP

        """
        self.latitude = latitude                # 1/1000th minutes
        self.longitude = longitude              # 1/1000th minutes
        self.altitude = altitude                # metres
        self.speed = speed                      # knots
        self.heading = heading                  # degrees
        self.timestamp = timestamp              # seconds since 1/1/1970 unix epoch
        self.satellites = satellites
        self.fixtype = fixtype
        self.PDOP = PDOP
        self.HDOP = HDOP
        self.VDOP = VDOP
        self.lat_dec_deg = latitude / 60000
        self.lon_dec_deg = longitude / 60000
        self.time_readable = datetime.datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


class HMI(object):
    """
    A Human Machine Interface element that can be used for headless operation.  Examples: LED indicator, buzzer.

    .. todo::
       Non-functional.  Develop this concept.

    :returns: HMI object (non-functional)

    """
    def __init__(self):
        pass

    def indicate_normal_operation(self):
        pass

    def indicate_com_issue(self):
        pass

    def trigger_shutdown(self):
        pass


class ModemGPIO(object):
    """
    Physical input/output interfaces for the modem e.g. reset, interrupts

    .. todo::
       Non-functional. Develop this concept, perhaps internalized within ``idpmodem``.

    :returns: GPIO object (non-functional)

    """
    def __init__(self):
        pass

    def assert_modem_reset(self):
        pass

    def monitor_reset_out(self):
        pass

    def monitor_notification_pin(self):
        pass

    def monitor_pps(self):
        pass
'''

def handle_mt_tracking_command(message):
    """
    Expects to get SIN 255 MIN 1 to reconfigure tracking interval, in minutes, in a range from 1-1440.

    :param message: ``dictionary`` for Mobile-Terminated message with

       - ``sin`` Service Identifier Number
       - ``min`` Message Identifier Number
       - ``payload`` (including MIN byte)
       - ``size`` in bytes including SIN, MIN
    :param data_type: 1 = Text (string), 2 = Hex (bytearray), 3 = base64 (bytearray)

    """
    # TODO: Additional testing
    global log
    global modem
    global tracking_interval

    if message['sin'] == 255 and message['min'] == 1 and message['data_format'] == 2:
        # Format: <SIN><MIN><tracking_interval> where tracking_interval is a 2-byte value in minutes
        payload = binascii.hexlify(bytearray(message['payload']))
        new_interval_minutes = int(payload[2:], 16)
        if (0 <= new_interval_minutes <= 1440) and ((new_interval_minutes * 60) != tracking_interval):
            log.info("Changing tracking interval to %d seconds" % (new_interval_minutes * 60))
            tracking_interval = new_interval_minutes * 60
            modem.tracking_setup(interval=tracking_interval)
        else:
            log.warning("Invalid tracking interval requested (%d minutes not in range 0..1440)" % new_interval_minutes)
            # TODO: send an error response indicating 'invalid interval' over the air
    else:
        log.warning("Unsupported command SIN=%d MIN=%d" % (message['sin'], message['min']))


def send_idp_location(loc):
    """
    Prepares a specific binary-optimized location report using SIN=255, MIN=255.

    :param loc: a Location object
    :return: Boolean success

    """
    global modem
    msg_sin = 255
    msg_min = 255
    payload = idpcodec.CommonMessageFormat(msg_sin=msg_sin, msg_min=msg_min, name='location')
    payload.add_field('timestamp', 'uint_32', loc.timestamp, '031b')
    payload.add_field('latitude', 'int_32', loc.latitude, '024b')
    payload.add_field('longitude', 'int_32', loc.longitude, '025b')
    payload.add_field('speed', 'int_16', loc.speed, '08b')
    payload.add_field('heading', 'int_16', loc.heading, '09b')
    payload.add_field('satellites', 'int_8', loc.satellites, '04b')
    payload.add_field('fixtype', 'int_8', loc.fixtype, '02b')
    payload.add_field('pdop', 'int_8', loc.PDOP, '04b')
    payload.delete_field('pdop')
    data_str = payload.encode_idp(data_format=3)
    message_name = 'LOC'
    message = idpmodem.MobileOriginatedMessage(name=message_name, payload=data_str, data_format=3, msg_sin=msg_sin, msg_min=msg_min)
    modem.send_message(message)


def parse_args(argv):
    """
    Parses the command line arguments.

    :param argv: An array containing the command line arguments
    :returns: A dictionary containing the command line arguments and their values

    """
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")

    parser.add_argument('-l', '--log', dest='logfile', type=str, default='idpmodemsample',
                        help="the log file name with optional extension (default extension .log)")

    parser.add_argument('-s', '--logsize', dest='log_size', type=int, default=5,
                        help="the maximum log file size, in MB (default 5 MB)")

    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        help="enable verbose debug logging (default OFF)")

    parser.add_argument('-c', '--crc', dest='use_crc', action='store_true',
                        help="force use of CRC on serial port (default OFF)")

    parser.add_argument('-t', '--track', dest='tracking', type=int, default=15,
                        help="location reporting interval in minutes (0..1440, default = 15, 0 = disabled)")

    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB0',
                        help="the serial port of the IDP modem")

    return vars(parser.parse_args(args=argv[1:]))


def main():
    """
    Sets up timer_threads for polling satellite status, incoming over-the-air messages, and location updates.
    Monitors the serial connection to the modem and re-initializes on reconnect.
    """
    global log
    global modem
    global tracking_interval
    global shutdown_flag

    shutdown_flag = False
    SERIAL_BAUD = 9600
    modem = None

    # Timer intervals (seconds)
    SAT_STATUS_INTERVAL = 5
    MT_MESSAGE_CHECK_INTERVAL = 15

    # Derive run options from command line
    user_options = parse_args(sys.argv)
    serial_name = user_options['port']
    if '.' not in user_options['logfile']:
        logfile = user_options['logfile'] + '.log'
    else:
        logfile = user_options['logfile']
    log_size = user_options['log_size']
    debug = user_options['debug']
    if 0 <= user_options['tracking'] <= 1440:
        tracking_interval = int(user_options['tracking'] * 60)
    else:
        sys.exit("Invalid tracking interval, must be in range 0..1440")

    # Set up log file
    log = get_wrapping_logger(filename=logfile)
    sys.stdout.flush()

    log.debug("**** PROGRAM STARTING ****")

    ever_connected = False
    start_time = str(datetime.datetime.utcnow())
    try:
        modem = idpmodem.Modem(serial_name=serial_name, log=log)
        success, error = modem.register_event_callback(event='new_mt_message', callback=handle_mt_tracking_command)
        # TODO: modem.register_event_callback(event='blocked', callback=tbd)
        modem.tracking_setup(interval=tracking_interval, on_location=send_idp_location)

    except KeyboardInterrupt:
        log.info("Execution stopped by keyboard interrupt.")

    except Exception:
        err_str = "Exception in user code:" + '-' * 40 + '\n' + traceback.format_exc()
        # err_str = "Error on line {}:".format(sys.exc_info()[-1].tb_lineno) + ',' + str(type(e)) + ',' + str(e)
        log.error(err_str)
        raise

    finally:
        end_time = str(datetime.datetime.utcnow())
        if modem is not None:
            log.info("*** Statistics from %s to %s ***" % (start_time, end_time))
            modem.log_statistics()
            modem.terminate()
        log.debug("\n\n*** END PROGRAM ***\n\n")


if __name__ == "__main__":
    main()
