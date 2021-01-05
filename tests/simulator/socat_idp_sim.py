#!/usr/bin/env python

import threading
import subprocess
import serial
import time
import binascii
import base64


def socat(dte='./simdte', dce='./simdce'):
    '''
    Start a socat proxy for a given source to a given target
    '''
    cmd = 'socat -d -d -v pty,rawer,echo=0,link={} pty,rawer,echo=0,link={}'.format(
        dte, dce)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    #(output, err) = p.communicate()
    # print(output) if not err else print(err)


def simulate(dte_name='./simdte', dce_name='./simdce'):
    try:
        socat_thread = threading.Thread(
            target=socat, args=(dte_name, dce_name), daemon=True)
        socat_thread.start()
        time.sleep(1)

        dce = serial.Serial(port=dce_name, baudrate=9600)
        terminate = False

        def dce_write(data, delay=0):
            time.sleep(delay)
            dce.write(data.encode())

        mt_message_queue = []
        mo_message_queue = []

        ok_responses = ['AT', 'ATZ', 'AT&W']

        while dce.isOpen() and not terminate:
            if dce.inWaiting() > 0:
                rx_data = dce.read(dce.inWaiting()).decode().strip()
                print('Received: {}'.format(rx_data))
                if rx_data == 'QUIT':
                    terminate = True
                elif rx_data in ok_responses:
                    dce_write('\r\nOK\r\n')
                elif rx_data == 'AT&V':
                    delay = 2
                    response = '\r\nACTIVE CONFIGURATION:' \
                        '\r\nE1 Q0 V1 CRC=0' \
                        '\r\nS0:000 S3:013 S4:010 S5:008 S6:000 S7:000 S8:000 S10:000 ' \
                        'S31:00080 S32:00025 S33:000 S34:007 S35:000 S36:00000 S37:00200 ' \
                        'S38:001 S40:000 S41:00180 S42:65535 S50:000 S52:02500 S53:000 ' \
                        'S60:001 S61:000 S62:001 S63:000 S64:042 S88:00000 S90:000 S91:000 ' \
                        '\r\n\r\nOK\r\n'
                    dce_write(response, delay)
                elif rx_data == 'AT+GSN;+GMR':
                    response = '\r\n+GSN: 00000000MFREE3D\r\n\r\n+GMR: 3.003,3.1,8\r\n' \
                        '\r\nOK\r\n'
                    dce_write(response)
                elif rx_data == 'ATS39? S41? S51? S55? S56? S57?':
                    response = '\r\n010\r\n\r\n00180\r\n\r\n000\r\n' \
                        '\r\n000\r\n\r\n000\r\n\r\n009\r\n' \
                        '\r\nOK\r\n'
                    dce_write(response)
                elif rx_data == 'ATS90=3 S91=1 S92=1 S122? S116?':
                    # TODO: model different progressions from unregistered
                    response = '\r\n0000000010\r\n\r\n0000004093\r\n\r\nOK\r\n'
                    dce_write(response)
                elif rx_data == 'AT%MGFN':
                    # TODO: model none, one, some
                    response = '\r\n%MGFN: "FM22.03",22.3,0,255,2,2,2\r\n' \
                        '"FM23.03",23.3,0,255,2,2,2\r\n' \
                        '"FM24.03",24.3,0,255,2,2,2\r\n' \
                        '"FM25.03",25.3,0,255,2,2,2\r\n' \
                        '"FM26.03",26.3,0,255,2,2,2\r\n' \
                        '"FM27.03",27.3,0,255,2,2,2\r\n' \
                        '"FM26.04",26.4,0,255,2,2,2\r\n' \
                        '\r\nOK\r\n'
                    dce_write(response)
                elif 'AT%MGFG=' in rx_data:
                    msg_name, data_format = (rx_data.split('=')[1]).split(',')
                    # data_type = int(data_type)
                    msg_name = msg_name.replace('"', '')
                    if msg_name in mt_message_queue and data_format in ['1', '2', '3']:
                        major, minor = msg_name.replace('FM', '').split('.')
                        msg_num = '.'.join([major, str(int(minor))])
                        msg_sin = 255
                        msg_min = 255
                        payload = b'Hello World'
                        data_bytes = bytearray(
                            [msg_sin, msg_min]) + bytearray(payload)
                        priority = '0'
                        state = '2'
                        length = len(data_bytes)
                        if data_format == '1':
                            msg_data = '\"{}\"'.format(data_bytes.decode())
                        elif data_format == '2':
                            msg_data = binascii.hexlify(data_bytes)
                        else:
                            msg_data = base64.b64encode(data_bytes)
                        response = '\r\nAT%MGFG: \"{}\",{},{},{},{},{},{},{}' \
                            '\r\nOK\r\n'.format(msg_name, msg_num, priority,
                                                msg_sin, state, length, data_format, msg_data)
                    else:
                        response = '\r\nERROR\r\n'
                        # TODO: set "last error S register"
                    dce_write(response)
                elif 'AT%MGFM=' in rx_data:
                    msg_name = rx_data.split('=')[1].replace('"', '')
                    if msg_name in mt_message_queue:
                        response = '\r\nOK\r\n'
                    else:
                        response = '\r\nERROR\r\n'
                    dce_write(response)
                elif 'AT%MGRT=' in rx_data:
                    msg_name, priority, sin_min, data_format, data = rx_data[6:].split(
                        ',')
                    mo_message_queue.append(msg_name)
                    response = '\r\nOK\r\n'
                    dce_write(response)
                elif 'AT%MGRS' in rx_data:
                    # TODO: MGRS= is for single or without = means 'all'
                    # ormat(msg_name, msg_num, priority, sin, state, length, bytesPktd)
                    response = '%MGRS: '
                    msg_name = None
                    if '=' in rx_data:
                        msg_name = rx_data.split('=')[1].replace('"', '')
                    if msg_name is not None:
                        response += '\"{}\",0,0,255,2,2,2\r\n'.format(msg_name)
                    else:
                        for msg_name in mo_message_queue:
                            reponse += '\"{}\",0,0,255,2,2,2\r\n'.format(
                                msg_name)
                    response += '\r\nOK\r\n'
                    dce_write(response)
                elif 'AT%GPS=' in rx_data:
                    example_response = '\r\n%GPS: $GNRMC,221511.000,A,4517.1073,N,07550.9222,W,0.07,0.00,150320,,,A,V*10\r\n' \
                        '$GNGGA,221511.000,4517.1073,N,07550.9222,W,1,08,1.3,135.0,M,-34.3,M,,0000*7E\r\n' \
                        '$GNGSA,A,3,28,17,30,11,19,07,,,,,,,2.5,1.3,2.1,1*37\r\n' \
                        '$GNGSA,A,3,87,81,,,,,,,,,,,2.5,1.3,2.1,2*32\r\n' \
                        '$GPGSV,2,1,08,01,,,42,07,18,181,35,11,32,056,29,17,48,265,35,0*5D\r\n' \
                        '$GPGSV,2,2,08,19,24,256,37,28,71,317,30,30,42,209,45,51,29,221,40,0*69\r\n' \
                        '$GLGSV,1,1,04,81,22,232,36,86,00,044,,87,57,030,42,,,,37,0*40\r\n' \
                        '\r\nOK\r\n'
                    example_nofix = '\r\n%GPS: $GNRMC,014131.000,V,,,,,,,160320,,,N,V*29' \
                        '\r\n$GNGGA,014131.000,,,,,0,06,2.2,,,,,,0000*48' \
                        '\r\n$GNGSA,A,1,19,17,28,,,,,,,,,,4.5,2.2,3.9,1*3C\r\n' \
                        '$GNGSA,A,1,81,80,79,,,,,,,,,,4.5,2.2,3.9,2*34\r\n' \
                        '$GPGSV,2,1,08,02,50,263,35,06,,,38,12,,,38,17,47,104,38,0*66\r\n' \
                        '$GPGSV,2,2,08,19,68,088,30,28,11,164,41,46,33,210,37,51,29,221,40,0*6B\r\n' \
                        '$GLGSV,1,1,03,79,19,217,47,80,36,276,35,81,53,050,36,0*4C\r\n' \
                        '\r\nOK\r\n'
                    response = '%GPS: '
                    parts = rx_data.split(',')
                    for part in parts:
                        if part == 'GGA':
                            response += '\r\n' if response != '%GPS: ' else ''
                            response += '$GNGGA,221511.000,4517.1073,N,07550.9222,W,1,08,1.3,135.0,M,-34.3,M,,0000*7E\r\n'
                        elif part == 'RMC':
                            response += '\r\n' if response != '%GPS: ' else ''
                            response += '$GNRMC,221511.000,A,4517.1073,N,07550.9222,W,0.07,0.00,150320,,,A,V*10\r\n'
                        elif part == 'GSA':
                            response += '\r\n' if response != '%GPS: ' else ''
                            response += '$GNGSA,A,3,28,17,30,11,19,07,,,,,,,2.5,1.3,2.1,1*37\r\n' \
                                '$GNGSA,A,3,87,81,,,,,,,,,,,2.5,1.3,2.1,2*32\r\n'
                        elif part == 'GSV':
                            response += '\r\n' if response != '%GPS: ' else ''
                            response += '$GPGSV,2,1,08,01,,,42,07,18,181,35,11,32,056,29,17,48,265,35,0*5D\r\n' \
                                '$GPGSV,2,2,08,19,24,256,37,28,71,317,30,30,42,209,45,51,29,221,40,0*69\r\n'
                    response += '\r\nOK\r\n'
                    dce_write(response, 3)
                elif rx_data == 'ATS80?':   # last error code
                    response = '\r\n104\r\n\r\nOK\r\n'
                    dce_write(response)
                else:
                    # TODO: %CRC, %OFF, %TRK, %UTC
                    print('WARNING: {} command unsupported'.format(rx_data))
                    response = '\r\nERROR\r\n'
                    dce_write(response)

    except KeyboardInterrupt:
        print('\nKeyboard Interrupt')
    except Exception as e:
        print(e)
    finally:
        exit()


def main():
    dte_name = './simdte'
    dce_name = './simdce'
    try:
        socat_thread = threading.Thread(
            target=socat, args=(dte_name, dce_name), daemon=True)
        socat_thread.start()
        time.sleep(1)
        dte = serial.Serial(port=dte_name, baudrate=9600)
        dce = serial.Serial(port=dce_name, baudrate=9600)
        cycles = 0
        while dte.isOpen() and dce.isOpen():
            if dce.inWaiting() > 0:
                print('\nRead: {}'.format(dce.read(dce.inWaiting()).decode()))
            if cycles == 3:
                print('Write:', end=' ')
                dte.write('TEST'.encode())
                cycles = 0
            cycles += 1
            time.sleep(1)
    except KeyboardInterrupt:
        print('Keyboard interrupt')
    except Exception as e:
        print(e)
    finally:
        # socat_thread.join()
        exit()


if __name__ == '__main__':
    main()
