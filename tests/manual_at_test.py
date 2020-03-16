"""Test utility for manually sending generic AT commands to the modem"""

import idpmodem
import sys
import time
import serial
import subprocess


def main():

    ser = None
    debug = True
    try:
        # Pre-initialization
        if sys.platform.startswith('linux2'):
            # TODO: improve serial port detection
            try:
                import RPi.GPIO as GPIO
                serial_name = '/dev/ttyUSB0'
            except ImportError:
                serial_name = '/dev/ttyAP1'
                subprocess.call('mts-io-sysfs store ap1/serial-mode rs232', shell=True)
        elif sys.platform.startswith('win32'):
            try:
                import idpwindows
                res = idpwindows.initialize()
                serial_name = res['serial']
                debug = res['debug']
            except ImportError:
                sys.exit("Could not import idpwindows.py test utility")
        else:
            sys.exit('Unsupported platform')
        serial_baud = 9600

        ser = serial.Serial(port=serial_name, baudrate=serial_baud,
                            timeout=None, writeTimeout=0,
                            xonxoff=False, rtscts=False, dsrdtr=False)

        if ser.isOpen():
            print('Connected to %s at %d baud' % (ser.name, ser.baudrate))
            sys.stdout.flush()
            ser.flush()
            ser.flushOutput()

            modem = idpmodem.Modem(serial_port=ser, debug=debug)
            modem.at_initialize_modem()

            while True:
                # get keyboard input
                time.sleep(0.5)
                input_str = raw_input("Input AT command >> ")
                if input_str.lower() == 'exit':
                    modem.display_statistics()
                    sys.exit()
                else:
                    result = modem.at_get_response(input_str)
                    if len(result['response']) > 0:
                        res_no = 1
                        for line in result["response"]:
                            print("Response [" + str(res_no) + "]: " + line)
                            res_no += 1

    except Exception as e:
        print('Error on line {}:'.format(sys.exc_info()[-1].tb_lineno), type(e), e)

    finally:
        if ser is not None and ser.isOpen():
            ser.flushInput()
            ser.flushOutput()
            ser.close()
            print('Serial port closed')


if __name__ == "__main__":
    main()
