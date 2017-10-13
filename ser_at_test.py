import sys
import time
import serial
import subprocess


global _debug
global ser


def log(logType, logStr):
    print(logType + ' ' + logStr)


def at_clean(atLine, restoreCrLf=False):
    if restoreCrLf:
        return atLine.replace('<cr>','\r').replace('<lf>','\n')
    else:
        return atLine.replace('<cr>','').replace('<lf>','').strip()


def at_getresponse(atCmd, atTimeout=10):
    """ Takes a single AT command, applies CRC if enabled, sends to the modem and waits for response completion
      Parses the response, line by line, until a result code is received or atTimeout is exceeded
      Assumes Quiet mode is disabled, and will not pass 'Quiet enable' (ATQ1) to the modem
      Sets modem object properties (Echo, CRC, Verbose, Quiet) by inference from AT response
      Returns a dictionary containing:
          echo       - the AT command sent (including CRC if applied) or empty string if Echo disabled
          response   - a list of strings representing multi-line response
                        if _debug is enabled, applies <cr> and <lf> printable tags in place of \r and \n
                        calling function may subsequently call at_clean to remove printable tags
          resultCode - a string returned after the response when Quiet mode is disabled
                        'OK' or 'ERROR' if Verbose is enabled on the modem, 
                        or a numeric error code that can be looked up in modem.atErrorResultCodes or at_handleresultcode
    """

    global _debug
    global ser
    # global modem

    atEcho = ''
    atResponse = []  # container for multi-line response
    atResultCode = ''
    atResCrc = ''

    # Rejection cases.  TODO: improve error handling
    if ";" in atCmd:
        log('warning', 'Multiple AT commands not supported: ' + atCmd)
        return {'echo': atEcho, 'response': atResponse, 'resultCode': atResultCode}
    if 'ATQ1' in atCmd:
        log('warning', 'Command rejected - quiet mode unsupported')
        return {'echo': atEcho, 'response': atResponse, 'resultCode': atResultCode}

    # Garbage collection
    orphanResponse = ''
    while ser.inWaiting() > 0:
        rChar = ser.read(1)
        if _debug:
            if rChar == '\r':
                rChar = '<cr>'
            elif rChar == '\n':
                rChar = '<lf>'
        orphanResponse += rChar
    if orphanResponse != '':
        log('warning', 'Orphaned response: ' + orphanResponse)
    ser.flushInput()  # clear pre-existing buffer

    '''
    if modem.atConfig['CRC']:
        toSend = atCmd + '*' + getcrc(atCmd)
    else:
        toSend = atCmd
    if "AT%CRC=1" in atCmd.upper():
        modem.atConfig['CRC'] = True
        if _debug: print("CRC enabled for next command")
    elif "AT%CRC=0" in atCmd.upper():
        modem.atConfig['CRC'] = False
        if _debug: print("CRC disabled for next command")
    ''' # '''

    toSend = atCmd
    if _debug: log('debug', 'Sending ' + toSend)
    ser.write(toSend + '\r\n')
    atSendTime = time.time()

    nLines = 0
    resLine = ''  # each line of response
    rawResLine = ''  # used for verbose debug purposes only
    atRxStart = False
    atRxComplete = False
    CHAR_WAIT = 0.05
    atTick = 0
    while not atRxComplete:
        time.sleep(CHAR_WAIT)
        while ser.inWaiting() > 0:
            if not atRxStart: atRxStart = True
            rChar = ser.read(1)
            if rChar == '\r':
                if _debug:
                    resLine += '<cr>'
                    rawResLine += '<cr>'
                else:
                    resLine += rChar  # no <lf> yet
                if atCmd in resLine:
                    if atCmd.upper() == 'ATE0':
                        # modem.atConfig['Echo'] = False
                        if _debug: print("ATE0 -> Echo off next command")
                    else:
                        # modem.atConfig['Echo'] = True
                        pass
                    atEcho = resLine  # <echo><cr> will be followed by <text><cr><lf> or <cr><lf><text><cr><lf> or <numeric code><cr> or <cr><lf><verbose code><cr><lf>
                    resLine = ''  # remove <echo><cr> before continuing to parse
                elif ser.inWaiting() == 0 and at_clean(
                        resLine) != '':  # <numeric code><cr> since all other alternatives would have <lf> pending
                    # modem.atConfig['Verbose'] = False
                    atResultCode = resLine
                    atRxComplete = True
                    break
            elif rChar == '\n':  # <cr><lf>... or <text><cr><lf> or <cr><lf><text><cr><lf> or <cr><lf><verbose code><cr><lf> or <*crc><cr><lf>
                if _debug:
                    resLine += '<lf>'
                    rawResLine += '<lf>'
                else:
                    resLine += rChar
                if 'OK' in resLine or 'ERROR' in resLine:  # <cr><lf><verbose code><cr><lf>
                    atResultCode = resLine
                    if ser.inWaiting() == 0:  # no checksum pending
                        atRxComplete = True
                        break
                    else:
                        resLine = ''
                elif '*' in resLine and len(at_clean(resLine)) == 5:  # <*crc><cr><lf>
                    # modem.atConfig['CRC'] = True
                    atResCrc = at_clean(resLine).strip('*')
                    atRxComplete = True
                    break
                else:  # <cr><lf>... or <text><cr><lf> or <cr><lf><text><cr><lf>
                    # nLines += 1
                    if at_clean(resLine) == '':  # <cr><lf>... not done parsing yet
                        # modem.atConfig['Verbose'] = True
                        pass
                    else:
                        nLines += 1
                        atResponse.append(resLine)
                        resLine = ''  # clear for next line parsing
            else:  # not \r or \n
                resLine += rChar
                if _debug: rawResLine += rChar
        if atResultCode != '':
            # modem.atConfig['Quiet'] = False
            break
        elif int(time.time()) - atSendTime > atTimeout:
            log('warning', toSend + ' command response timed out')
            break
        # TODO: develop reliable handler for Quiet mode use cases. Likely based on ATS61?
        '''
        elif modem.atConfig['Quiet']:
            # Determine some way of knowning the command response is complete
            break
        '''
        if _debug and int(time.time()) > (atSendTime + atTick):
            atTick += 1
            print('Waiting AT response. Tick=' + str(atTick))

    bChecksumOk = True
    '''
    bChecksumOk = False
    if atResCrc == '':
        # modem.atConfig['CRC'] = False
        pass
    else:
        # modem.atConfig['CRC'] = True
        strToValidate = ''
        if len(atResponse) == 0 and atResultCode != '':
            strToValidate = atResultCode
        elif len(atResponse) > 0:
            for resLine in atResponse:
                strToValidate += resLine
            if atResultCode != '':
                strToValidate += atResultCode
        if crc_validate(at_clean(strToValidate, restoreCrLf=True), atResCrc):
            bChecksumOk = True
        else:
            expectedChecksum = getcrc(at_clean(strToValidate, restoreCrLf=True))
            log('error', 'Bad checksum received: *' + atResCrc + ' expected: *' + expectedChecksum)
    ''' # '''

    # Verbose debug shows complete response on console
    if _debug:
        if atEcho != '':
            print("Echo: " + atEcho)
        print("Raw response: " + rawResLine.replace(atEcho,''))
        resNo = 1
        for line in atResponse:
            print("Response [" + str(resNo) + "]: " + line)
            resNo += 1
        if atResultCode != '':
            print("Result Code: " + str(atResultCode))
        '''
        if modem.atConfig['CRC']:
            if bChecksumOk:
                print('CRC OK (' + atResCrc + ')')
            else:
                print('BAD CRC (expected: ' + expectedChecksum + ')')
        ''' # '''
    '''  # '''

    return {'echo': atEcho, 'response': atResponse, 'resultCode': atResultCode, 'checksum': atResCrc,
            'error': bChecksumOk}


def main():
    global _debug
    _debug = True
    global ser

    try:
        # Pre-initialization
        if sys.platform.startswith('linux'):
            try:
                import RPi.GPIO as GPIO
                SERIAL_NAME = '/dev/ttyUSB0'  # for Raspberry Pi pyserial version XX TODO: clarify what version/compatibility?
            except:
                SERIAL_NAME = '/dev/ttyAP1'
                subprocess.call('mts-io-sysfs store mfser/serial-mode rs232', shell=True)
                if _debug: print('MTS serial-mode: rs232')
        else:
            # LOGFILE_NAME = 'C:\Users\geoffbp\workspace\RPI-IDP\IdpRegistration.log'
            sys.exit('Unsupported platform, please run on linux')
        SERIAL_BAUD = 9600

        # TODO: more robust serial configuration
        ser = serial.Serial(SERIAL_NAME, SERIAL_BAUD)
        print('Connected to ' + ser.name + ' at ' + str(ser.baudrate) + ' baud' + '\n')

        if ser.isOpen():        #TODO: Optimize for NOT case
            sys.stdout.flush()
            ser.flush()
            ser.flushOutput()

            while True:
                # get keyboard input
                inputStr = raw_input("Input AT command >> ")
                if inputStr.lower() == 'exit':
                    sys.exit()
                else:
                    result = at_getresponse(inputStr)
                    if result['response'] != []:
                        resNo = 1
                        for line in result["response"]:
                            print("Response [" + str(resNo) + "]: " + at_clean(line))
                            resNo += 1

    except Exception as e:
        print('Error on line {}:'.format(sys.exc_info()[-1].tb_lineno), type(e), e)

    finally:
        if ser.isOpen():
            ser.close()
            print('Serial port closed')


if __name__ == "__main__":
    main()