"""
Sample program to run on Raspberry Pi (headless) or Windows (using ORBCOMM/SkyWave Modem Simulator)
or Multitech Conduit with serial mCard (AP1 slot).
Periodically queries modem status, checks for incoming messages and sends location reports

Dependencies:
  - (REQUIRED) crcxmodem.py calculates CRC-16-CCITT xmodem
  - (REQUIRED) idpmodem.py contains object definitions for the modem
  - (optional) RPi.GPIO for running headless on Raspberry Pi
  - (optional) serialportfinder.py is used when running on Windows test environment (detect COM port)

Mobile-Originated location reports are 10 bytes using SIN 255 MIN 255
Mobile-Terminated location interval change uses SIN 255 MIN 1, plus 1 byte payload for the new interval in minutes.
  When a new interval is configured, a location report is generated immediately, thereafter at the
  new interval.
"""
#!/usr/bin/python
import time
import datetime
import serial       # PySerial 2.7
import sys
import logging
from logging.handlers import RotatingFileHandler
import threading
import crcxmodem
import base64
import operator
import argparse
import subprocess
import idpmodem

# global logfileName
global _debug
global log
global ser  # the serial port handle for AT communications
global modem    # the data structure for modem operating parameters and statistics defined in 'idpModem' module
global lockThread   # a lock to ensure that parallel threads do not mix up AT request/response operations
global trackingInterval_s
global SERIAL_NAME


class RepeatingTimer():
    """ A Timer class that does not stop, unless you want it to. 
     Used to call repeating functions at defined intervals.
    """

    def __init__(self, seconds, target, args=None, name=''):
        self._should_continue = False
        self.is_running = False
        self.seconds = seconds
        self.target = target
        self.args = args
        self.thread = None
        if name != '':
            self.name = name

    def _handle_target(self):
        self.is_running = True
        if self.args is not None:
            self.target(self.args)
        else:
            self.target()
        self.is_running = False
        self._start_timer()

    def _start_timer(self):
        if self._should_continue:
            self.thread = threading.Timer(self.seconds, self._handle_target)
            if self.name != '':
                self.thread.name = self.name
            self.thread.start()

    def start(self):
        if not self._should_continue and not self.is_running:
            self._should_continue = True
            self._start_timer()
        else:
            log('warning', 'Timer already started or running, process must wait')

    def cancel(self):
        if self.thread is not None:
            self._should_continue = False   # Just in case thread is running and cancel failed
            self.thread.cancel()
        else:
            log('warning', 'Timer never started or failed to initialize')


def print_log(logLvl, logStr):
    """ Writes a timestamped log file entry and prints it to console if _debug is enabled" 
    :param logLvl: log level mirrors the logging library
    :param logStr: the message to be logged
    """

    ts = '{:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.now())
    logStrVerbose = ts + ',(' + str(threading.currentThread().name) + '),'
    if logLvl == 'critical':
        logging.critical(logStr)
        logStrVerbose += '[CRITICAL],' + logStr
    elif logLvl == 'error':
        logging.error(logStr)
        logStrVerbose += '[ERROR],' + logStr
    elif logLvl == 'warning':
        logging.warning(logStr)
        logStrVerbose += '[WARNING],' + logStr
    elif logLvl == 'info':
        logging.info(logStr)
        logStrVerbose += '[INFO],' + logStr
    else:   # logLvl == 'debug'
        logging.debug(logStr)
        logStrVerbose += '[DEBUG],' + logStr
    if _debug: print(logStrVerbose)


def getcrc(atCmd):
    """ Returns the CRC-16-CCITT (initial value 0xFFFF) checksum """

    crcAtCmd = '{:04X}'.format(crcxmodem.crc(atCmd, 0xffff))
    return crcAtCmd


def at_clean(atLine, restoreCrLf=False):
    """ Removes debug tags used for visualizing <cr> and <lf> characters
    :param atLine: the AT command with debug characters included
    :param restoreCrLf: an option to restore <cr> and <lf>
    :return: the cleaned AT command without debug tags
    """

    if restoreCrLf:
        return atLine.replace('<cr>', '\r').replace('<lf>', '\n')
    else:
        return atLine.replace('<cr>', '').replace('<lf>', '')


def updatestats_atresponse(refAtReqTime, atCmd):
    """ Updates the last and average AT command response time statistics """
    global _debug
    global modem

    atResponseTime_ms = int((time.time() - refAtReqTime) * 1000)
    modem.systemStats['lastATResponseTime_ms'] = atResponseTime_ms
    if _debug:
        log.debug("Response time for " + atCmd + ": " + str(atResponseTime_ms) + " [ms]")
    if modem.systemStats['avgATResponseTime_ms'] == 0:
        modem.systemStats['avgATResponseTime_ms'] = atResponseTime_ms
    else:
        modem.systemStats['avgATResponseTime_ms'] = int((modem.systemStats['avgATResponseTime_ms'] + atResponseTime_ms) / 2)


def at_getresponse(atCmd, atTimeout=10):
    """ Takes a single AT command, applies CRC if enabled, sends to the modem and waits for response completion
      Parses the response, line by line, until a result code is received or atTimeout is exceeded
      Assumes Quiet mode is disabled, and will not pass 'Quiet enable' (ATQ1) to the modem
      Sets modem object properties (Echo, CRC, Verbose, Quiet) by inference from AT response
    :param  atCmd       the AT command to send
            atTimeout   the time in seconds to wait for a response
    :return a dictionary containing:
            echo        - the AT command sent (including CRC if applied) or empty string if Echo disabled
            response    - a list of strings representing multi-line response
                        if _debug is enabled, applies <cr> and <lf> printable tags in place of \r and \n
                        calling function may subsequently call at_clean to remove printable tags
            resultCode  - a string returned after the response when Quiet mode is disabled
                        'OK' or 'ERROR' if Verbose is enabled on the modem, 
                        or a numeric error code that can be looked up in modem.atErrorResultCodes
            checksum    - the CRC (if enabled) or None
            error       - Boolean if CRC is correct
            timeout     - Boolean if AT response timed out
    """
    global _debug
    global log
    global ser
    global modem

    atEcho = ''
    atResponse = []     # container for multi-line response
    atResultCode = ''
    atResCrc = ''
    timed_out = False

    # Rejection cases.  TODO: improve error handling
    if ";" in atCmd:
        log.warning("Multiple AT commands not supported: " + atCmd)
        return {'echo': atEcho, 'response': atResponse, 'resultCode': atResultCode}
    if 'ATQ1' in atCmd:
        log.warning(atCmd + " command rejected - quiet mode unsupported")
        return {'echo': atEcho, 'response': atResponse, 'resultCode': atResultCode}

    # Garbage collection
    orphanResponse = ''
    while ser.inWaiting() > 0:
        rChar = ser.read(1)
        if _debug:
            if rChar == '\r': rChar = '<cr>'
            elif rChar == '\n': rChar = '<lf>'
        orphanResponse += rChar
    if orphanResponse != '':
        log.warning("Orphaned response: " + orphanResponse)
    ser.flushInput()    # clear pre-existing buffer

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

    log.debug("Sending " + toSend)
    ser.write(toSend + '\r')
    atSendTime = time.time()

    nLines = 0
    resLine = ''        # each line of response
    rawResLine = ''     # used for verbose debug purposes only
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
                    resLine += rChar                                            # no <lf> yet
                if atCmd in resLine:
                    if atCmd.upper() == 'ATE0':
                        modem.atConfig['Echo'] = False
                        if _debug: print("ATE0 -> Echo off next command")
                    else:
                        modem.atConfig['Echo'] = True
                    atEcho = resLine                                            # <echo><cr> will be followed by <text><cr><lf> or <cr><lf><text><cr><lf> or <numeric code><cr> or <cr><lf><verbose code><cr><lf>
                    resLine = ''                                                # remove <echo><cr> before continuing to parse
                elif ser.inWaiting() == 0 and at_clean(resLine) != '':          # <numeric code><cr> since all other alternatives would have <lf> pending
                    modem.atConfig['Verbose'] = False
                    atResultCode = resLine
                    atRxComplete = True
                    break
            elif rChar == '\n':                                                 # <cr><lf>... or <text><cr><lf> or <cr><lf><text><cr><lf> or <cr><lf><verbose code><cr><lf> or <*crc><cr><lf>
                if _debug:
                    resLine += '<lf>'
                    rawResLine += '<lf>'
                else:
                    resLine += rChar
                if 'OK' in resLine or 'ERROR' in resLine:                       # <cr><lf><verbose code><cr><lf>
                    atResultCode = resLine
                    if ser.inWaiting() == 0:                                    # no checksum pending
                        atRxComplete = True
                        break
                    else:
                        resLine = ''
                elif '*' in resLine and len(at_clean(resLine)) == 5:             # <*crc><cr><lf>
                    modem.atConfig['CRC'] = True
                    atResCrc = at_clean(resLine).strip('*')
                    atRxComplete = True
                    break
                else:                                                            # <cr><lf>... or <text><cr><lf> or <cr><lf><text><cr><lf>
                    # nLines += 1
                    if at_clean(resLine) == '':                                  # <cr><lf>... not done parsing yet
                        modem.atConfig['Verbose'] = True
                    else:
                        nLines += 1
                        atResponse.append(resLine)
                        resLine = ''                                            # clear for next line parsing
            else:                                                               # not \r or \n            
                resLine += rChar
                if _debug:
                    rawResLine += rChar
        if atResultCode != '':
            modem.atConfig['Quiet'] = False
            break
        elif int(time.time()) - atSendTime > atTimeout:
            log('warning', toSend + ' command response timed out')
            timed_out = True
            break
        # TODO: develop reliable handler for Quiet mode use cases. Likely based on ATS61?
        '''
        elif modem.atConfig['Quiet']:
            # Determine some way of knowning the command response is complete
            break
        '''
        if _debug and int(time.time()) > (atSendTime + atTick):
            atTick += 1
            print("Waiting AT response. Tick=" + str(atTick))

    updatestats_atresponse(atSendTime, atCmd)

    bChecksumOk = False
    if atResCrc == '':
        modem.atConfig['CRC'] = False
    else:
        modem.atConfig['CRC'] = True
        strToValidate = ''
        if len(atResponse) == 0 and atResultCode != '':
            strToValidate = atResultCode
        elif len(atResponse) > 0:
            for resLine in atResponse:
                strToValidate += resLine
            if atResultCode != '':
                strToValidate += atResultCode
        if getcrc(at_clean(strToValidate, restoreCrLf=True)) == atResCrc:
            bChecksumOk = True
        else:
            expectedChecksum = getcrc(at_clean(strToValidate, restoreCrLf=True))
            log.error("Bad checksum received: *" + atResCrc + " expected: *" + expectedChecksum)
    
    # Verbose debug shows complete response on console
    '''
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
        if modem.atConfig['CRC']:
            if bChecksumOk:
                print('CRC OK (' + atResCrc + ')')
            else:
                print('BAD CRC (expected: ' + expectedChecksum + ')')
    ''' # '''

    return {'echo': atEcho,
            'response': atResponse,
            'resultCode': atResultCode,
            'checksum': atResCrc,
            'error': bChecksumOk,
            'timeout': timed_out}


def at_handleresultcode(resultCode):
    """ Queries the details of an error response on the AT command interface
    :param resultCode: the value returned by the AT command response
    :return: errCode - the specific error code
             errStr - the interpretation of the error code
    """
    global modem
    global lockThread

    if 'OK' in resultCode or at_clean(resultCode) == '0':
        return 0, 'OK'
    elif 'ERROR' in resultCode or at_clean(resultCode) == '':
        with lockThread:
            response = at_getresponse('ATS80?')
            if at_clean(response['response'][0]) != '':
                errCode = int(at_clean(response['response'][0]))
            else:
                errCode = 4
                log('error', 'No response to query of S80 (last error code)')
    else:
        errCode = int(at_clean(resultCode))
    if errCode > 0:
        errStr = modem.atErrResultCodes[str(errCode)]
        return errCode, errStr


def checksatstatus():
    """ Checks satellite status using Trace Log Mode to update state and statistics """
    global _debug
    global log
    global modem
    global lockThread

    AT_SATSTATUS_QUERY = 'ATS90=3 S91=1 S92=1 S122? S116?'

    with lockThread:
        if _debug:
            log.debug("Thread: Checking satellite status. Previous control state: " + modem.satStatus['CtrlState'])
        response = at_getresponse(AT_SATSTATUS_QUERY)
        errCode, errStr = at_handleresultcode(response['resultCode'])
        if errCode == 0:
            oldSatCtrlState = modem.satStatus['CtrlState']
            newSatCtrlState = modem.ctrlStates[int(at_clean(response['response'][0]))]
            if newSatCtrlState != oldSatCtrlState:
                log.info("Satellite control state change: OLD=" + oldSatCtrlState + " NEW=" + newSatCtrlState)
                modem.satStatus['CtrlState'] = newSatCtrlState

                # Key events for relevant state changes and statistics tracking
                if newSatCtrlState == 'Waiting for GNSS fix':
                    modem.systemStats['lastGNSSStartTime'] = int(time.time())
                    modem.systemStats['nGNSS'] += 1
                elif newSatCtrlState == 'Registration in progress':
                    modem.systemStats['lastRegStartTime'] = int(time.time())
                    modem.systemStats['nRegistration'] += 1
                elif newSatCtrlState == 'Downloading Bulletin Board':
                    modem.satStatus['BBWait'] = True
                    modem.systemStats['lastBBStartTime'] = time.time()
                elif newSatCtrlState == 'Registration in progress':
                    modem.systemStats['lastRegStartTime'] = int(time.time())
                elif newSatCtrlState == 'Active':
                    if modem.satStatus['Blocked'] == True:
                        log.info("Blockage cleared")
                        blockDuration = int(time.time() - modem.systemStats['lastBlockStartTime'])
                        if modem.systemStats['avgBlockageDuration'] > 0:
                            modem.systemStats['avgBlockageDuration'] = int((blockDuration + modem.systemStats['avgBlockageDuration'])/2)
                        else:
                            modem.systemStats['avgBlockageDuration'] = blockDuration
                    modem.satStatus['Registered'] = True
                    modem.satStatus['Blocked'] = False
                    modem.satStatus['BBWait'] = False
                    if modem.systemStats['lastRegStartTime'] > 0:
                        regDuration = int(time.time() - modem.systemStats['lastRegStartTime'])
                    else:
                        regDuration = 0
                    if modem.systemStats['avgRegistrationDuration'] > 0:
                        modem.systemStats['avgRegistrationDuration'] = int((regDuration + modem.systemStats['avgRegistrationDuration'])/2)
                    else:
                        modem.systemStats['avgRegistrationDuration'] = regDuration
                elif newSatCtrlState == 'Blocked':
                    modem.satStatus['Blocked'] = True
                    modem.systemStats['lastBlockStartTime'] = time.time()
                    log.info("Blockage started")

                # Other transitions for statistics tracking:
                if oldSatCtrlState == 'Waiting for GNSS fix' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                    gnssDuration = int(time.time() - modem.systemStats['lastGNSSStartTime'])
                    log.info("GNSS acquired in " + str(gnssDuration) + " seconds")
                    if modem.systemStats['avgGNSSFixDuration'] > 0:
                        modem.systemStats['avgGNSSFixDuration'] = int((gnssDuration + modem.systemStats['avgGNSSFixDuration'])/2)
                    else:
                        modem.systemStats['avgGNSSFixDuration'] = gnssDuration
                if oldSatCtrlState == 'Downloading Bulletin Board' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                    bbDuration = int(time.time() - modem.systemStats['lastBBStartTime'])
                    log.info("Bulletin Board downloaded in: " + str(bbDuration) + " seconds")
                    if modem.systemStats['avgBBReacquireDuration'] > 0:
                        modem.systemStats['avgBBReacquireDuration'] = int((bbDuration + modem.systemStats['avgBBReacquireDuration'])/2)
                    else:
                        modem.systemStats['avgBBReacquireDuration'] = bbDuration
                if oldSatCtrlState == 'Active' and newSatCtrlState != 'Stopped' and newSatCtrlState != 'Blocked':
                    modem.systemStats['lastRegStartTime'] = int(time.time())
                    modem.systemStats['nRegistration'] += 1

            CN0 = int(int(at_clean(response['response'][1])) / 100)
            if modem.systemStats['avgCN0'] == 0:
                modem.systemStats['avgCN0'] = CN0
            else:
                modem.systemStats['avgCN0'] = int((modem.systemStats['avgCN0'] + CN0) / 2)
        else:
            log.error("Bad response to satellite status query (" + errStr + ")")
    return


def parsetrackingcmd(msgContent, msgSIN=255, msgMIN=1):
    """ Expects to get SIN 255 MIN 1 'reconfigure tracking interval, in minutes, in a range from 1-1440 
    :param msgContent: Mobile-Terminated message payload with format <SIN><MIN><interval>, 1 byte each
     optional parameters msgSIN, msgMIN placeholders for future features
    """
    global log
    global trackingInterval_s

    if msgSIN == 255 and msgMIN == 1:
        newTrackingInterval_min = int(msgContent[2:], 16)
        if 0 <= newTrackingInterval_min <= 1440:
            for t in threading.enumerate():
                if t.name == 'GetSendLocation':
                    # TODO: not ideal, this gets the underlying timer handle directly, not the parent RepeatingTimer
                    t.cancel()
            trackingInterval_s = newTrackingInterval_min * 60
            log.info("Changing tracking interval to " + str(trackingInterval_s) + " seconds")
            if newTrackingInterval_min > 0:
                t = RepeatingTimer(trackingInterval_s, target=getsendlocation, name='GetSendLocation')
                getsendlocation()
                t.start()
        else:
            # TODO: send an error response indicating 'invalid interval' over the air
            pass
    else:
        log.warning("Unsupported command.")


def checkmtmessages():
    """ Checks for Mobile-Terminated messages in modem queue and retrieves if present.
     Logs a record of the receipt, and handles supported messages
    """
    global _debug
    global log
    global lockThread
    global modem

    msgretrieved = False
    with lockThread:
        if _debug:
            log.debug("Thread: Checking for MT messages")
        response = at_getresponse('AT%MGFN')
        errCode, errStr = at_handleresultcode(response['resultCode'])
        if errCode == 0:
            msgSummary = at_clean(response['response'][0]).replace('%MGFN:', '').strip()
            if msgSummary:
                msgParms = msgSummary.split(',')
                msgName = msgParms[0]
                # msgNum = msgParms[1]
                # msgPriority = msgParms[2]
                msgSIN = int(msgParms[3])   # TODO: broken on RPi?
                msgState = int(msgParms[4])
                msgLen = int(msgParms[5])
                if msgState == 2: # Complete and not read
                    # TODO: more generic handling of dataType based on length, pass to helper functions for parsing
                    if msgSIN == 128:
                        dataType = '1'  # Text
                    elif msgSIN == 255:
                        dataType = '2'  # ASCII-Hex
                    else:
                        dataType = '3'  # base64
                    response = at_getresponse('AT%MGFG=' + msgName + "," + dataType)
                    errCode, errStr = at_handleresultcode(response['resultCode'])
                    if errCode == 0:
                        msgretrieved = True
                        msgEnvelope = at_clean(response['response'][0]).replace('%MGFG:', '').strip().split(',')
                        msgContent = msgEnvelope[7]
                        if dataType == '1':
                            msgContentStr = msgContent
                        elif dataType == '2':
                            msgMIN = int(msgContent[0:2])
                            msgContentStr = '0x' + str(msgContent)
                        elif dataType == '3':
                            msgContentStr = base64.b64decode(msgContent)
                        log.info(str(msgLen) + "-byte message received with content: SIN=" +
                                 str(msgSIN) + " " + msgContentStr)
                        if modem.systemStats['avgMTMsgSize'] == 0:
                            modem.systemStats['avgMTMsgSize'] = msgLen
                        else:
                            modem.systemStats['avgMTMsgSize'] = int(
                                (modem.systemStats['avgMTMsgSize'] + msgLen) / 2)
                    else:
                        log.error("Could not get MT message (" + errStr + ")")
        else:
            log.error("Could not get new MT message info (" + errStr + ")")

    # TODO: more elegant/generic processing with helper functions
    if msgretrieved:
        if msgSIN == 255:
            parsetrackingcmd(msgContent, msgSIN, msgMIN)
        else:
            log.info("Message SIN=" + str(msgSIN) + " MIN=" + str(msgMIN) + "not handled.")

    return


def sendmessage(dataString, dataFormat=1, SIN=128, MIN=1):
    """ Transmits a Mobile-Originated message. If ASCII-Hex format is used, 0-pads to nearest byte boundary
    :param dataString: data to be transmitted
    :param dataFormat: 1=Text (default), 2=ASCII-Hex, 3=base64
    :param SIN: first byte of message (default 128 "user")
    :param MIN: second byte of message (default 1 "user")
    :return: nothing
    """
    global _debug
    global log
    global lockThread
    global modem

    moMsgName = str(int(time.time()))[:8]
    moMsgPriority = 4
    moMsgSin = SIN
    moMsgMin = MIN
    moMsgFormat = dataFormat
    if dataFormat == 1:
        moMsgContent = '"' + dataString + '"'
    else:
        moMsgContent = dataString
        if dataFormat == 2 and len(dataString)%2 > 0:
            moMsgContent += '0'     # insert 0 padding to byte boundary
    with lockThread:
        response = at_getresponse(
            'AT%MGRT="' + moMsgName + '",' + str(moMsgPriority) + ',' + str(moMsgSin) + '.' + str(
                moMsgMin) + ',' + str(moMsgFormat) + ',' + moMsgContent)
        errCode, errStr = at_handleresultcode(response['resultCode'])
        moSubmitTime = time.time()
        if errCode == 0:
            msgComplete = False
            statusPollCount = 0
            while not msgComplete:
                time.sleep(1)
                statusPollCount += 1
                if _debug:
                    log.debug("MGRS queries: " + str(statusPollCount))
                response = at_getresponse('AT%MGRS="' + moMsgName + '"')
                errCode, errStr = at_handleresultcode(response['resultCode'])
                if errCode == 0:
                    resParms = at_clean(response['response'][0]).split(',')
                    # resHeader = resParms[0]
                    # resMsgNo = resParms[1]
                    # resPrio = int(resParms[2])
                    # resSin = int(resParms[3])
                    resState = int(resParms[4])
                    resSize = int(resParms[5])
                    # resSent = int(resParms[6])
                    if resState > 5:
                        msgComplete = True
                        if resState == 6:
                            msgLatency = int(time.time() - moSubmitTime)
                            log.info("MO message (" + str(resSize) + " bytes) completed in " +
                                     str(msgLatency) + ' seconds')
                            if modem.systemStats['avgMOMsgSize'] == 0:
                                modem.systemStats['avgMOMsgSize'] = resSize
                            else:
                                modem.systemStats['avgMOMsgSize'] = int(
                                    (modem.systemStats['avgMOMsgSize'] + resSize) / 2)
                            if modem.systemStats['avgMOMsgLatency_s'] == 0:
                                modem.systemStats['avgMOMsgLatency_s'] = msgLatency
                            else:
                                modem.systemStats['avgMOMsgLatency_s'] = int(
                                    (modem.systemStats['avgMOMsgLatency_s'] + msgLatency) / 2)
                        else:
                            log.info("MO message (" + str(resSize) + " bytes) failed after " +
                                     str(int(time.time() - moSubmitTime)) + " seconds")
                elif errCode == 109:
                    if _debug:
                        print("Message complete, Unavailable")
                    break
                else:
                    log.error("Error getting message state (" + errStr + ")")
        else:
            log.error("Message submit error (" + errStr + ")")


def getuserinput():
    """ Provides a GUI window (intended for Windows test environment) accepting AT commands or user data to send """
    # TODO: deprecate or create more robust threaded operation
    global log
    global lockThread

    try:
        import Tkinter

        def parseUserInput():
            userInput = str(e.get())
            if userInput.upper().startswith('AT'):
                with lockThread:
                    response = at_getresponse(userInput.upper())
                    errCode, errStr = at_handleresultcode(response['resultCode'])
                    if errCode == 0:
                        for res in response['response']:
                            log.info("Response to " + atCommand + ": " + res)
                    else:
                        log.error("AT response error (" + errStr + ")")
            elif userInput != '':
                sendmessage(userInput)
            else:
                log.warning("User entered no data or command")

        root = Tkinter.Tk()
        root.title = "AT Command Input"
        e = Tkinter.Entry()
        e.pack()
        b = Tkinter.Button(text="OK", command=parseUserInput)
        b.pack()
        root.mainloop()
        # root.destroy()    # TODO: removing this doesn't seem to do anything
    except Exception, err:
        log.error(str(err))


class Location(object):
    """ A class containing a specific set of location-based information for a given point in time """

    def __init__(self, latitude=91*60*1000, longitude=181*60*1000, altitude=0,
                 speed=0, heading=0, timestamp=0, satellites=0, fixtype=1,
                 PDOP=0, HDOP=0, VDOP=0):
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


def sendlocation(loc):
    """ Prepares a specific binary-optimized location report using SIN=255, MIN=255
    :param loc: a Location object
    :return: nothing; calls sendmessage function
    """

    dataFields = [
        (loc.timestamp, '031b'),
        (loc.latitude, '024b'),
        (loc.longitude, '025b'),
        (loc.altitude, '08b'),
        (loc.speed, '08b'),
        (loc.heading, '09b'),
        (loc.satellites, '04b'),
        (loc.fixtype, '02b'),
        (loc.PDOP, '05b')
    ]

    binStr = ''
    for field in dataFields:
        if field[0] < 0:
            invBinField = format(-field[0], field[1])
            compBinField = ''
            i = 0
            while len(compBinField) < len(invBinField):
                if invBinField[i] == '0':
                    compBinField += '1'
                else:
                    compBinField += '0'
                i += 1
            binField = format(int(compBinField, 2) + 1, field[1])
        else:
            binField = format(field[0], field[1])
        binStr += binField
    padBits = len(binStr) % 8
    while padBits > 0:
        binStr += '0'
        padBits -= 1
    hexStr = ''
    indexByte = 0
    while len(hexStr)/2 < len(binStr)/8:
        hexStr += format(int(binStr[indexByte:indexByte+8], 2), '02X').upper()
        indexByte += 8
    sendmessage(hexStr, dataFormat=2, SIN=255, MIN=255)


def validateNMEAchecksum(sentence):
    """ Validates NMEA checksum according to the standard
    :param sentence: NMEA sentence including checksum
    :return: boolean result (checksum correct)
             raw NMEA data string, with prefix $Gx and checksum suffix removed
    """

    sentence = sentence.strip('\n')
    nmeadata, cksum = sentence.split('*', 1)
    nmeadata = nmeadata.replace('$', '')
    xcksum = str("%0.2x" % (reduce(operator.xor, (ord(c) for c in nmeadata), 0))).upper()
    return (cksum == xcksum), nmeadata[2:]


def parseNMEAtoLocation(sentence, loc):
    """ parses NMEA string(s) to populate a Location object
    Several sentence parameters are unused but remain as placeholders for completeness/future use
    :param sentence: NMEA sentence (including prefix and suffix)
    :param loc: the Location object to be populated
    :return: Boolean success of operation
             error string if not successful
    """

    errStr = ''
    res, nmeadata = validateNMEAchecksum(sentence)
    if res:
        sentenceType = nmeadata[0:3]
        if sentenceType == 'GGA':
            GGA = nmeadata.split(',')
            GGAutc_hhmmss = GGA[1]
            GGAlatitude_dms = GGA[2]
            GGAns = GGA[3]
            GGAlongitude_dms = GGA[4]
            GGAew = GGA[5]
            GGAqual = GGA[6]
            GGAFixQualities = [
                'invalid',
                'GPS fix',
                'DGPS fix',
                'PPS fix',
                'RTK',
                'Float RTK',
                'Estimated',
                'Manual',
                'Simulation'
            ]
            GGAsatellites = GGA[7]
            GGAhdop = GGA[8]
            GGAaltitude = GGA[9]
            GGAheightWGS84 = GGA[11]
            loc.altitude = int(GGAaltitude) # 545.4 = meters above mean sea level

        elif sentenceType == 'RMC':
            RMC = nmeadata.split(',')
            RMCutc_hhmmss = RMC[1]
            # RMCactive = RMC[2]
            RMClatitude_dms = RMC[3]        # 4807.038 = 48 deg 07.038'
            RMCns = RMC[4]
            RMClongitude_dms = RMC[5]       # 01131.000 = 11 deg 31.000'
            RMCew = RMC[6]
            RMCspeed_kn = RMC[7]            # 022.4 = 22.4 knots
            RMCheading_deg = RMC[8]         # 084.4 = 84.4 degrees True
            RMCdate_ddmmyy = RMC[9]
            # RMCmvmag = RMC[10]
            # RMCmvdir = RMC[11]
            year = int(RMCdate_ddmmyy[4:6]) + 2000
            month = int(RMCdate_ddmmyy[2:4])
            day = int(RMCdate_ddmmyy[0:2])
            hour = int(RMCutc_hhmmss[0:2])
            minute = int(RMCutc_hhmmss[2:4])
            second = int(RMCutc_hhmmss[4:6])
            dt = datetime.datetime(year, month, day, hour, minute, second)
            loc.timestamp = int(time.mktime(dt.timetuple()))
            loc.latitude = int((float(RMClatitude_dms[0:2]) + float(RMClatitude_dms[2:]) / 60) * 60 * 1000)
            if RMCns == 'S': loc.latitude *= -1
            loc.longitude = int((float(RMClongitude_dms[0:3]) + float(RMClongitude_dms[3:]) / 60) * 60 * 1000)
            if RMCew == 'W': loc.longitude *= -1
            loc.speed = int(float(RMCspeed_kn))
            loc.heading = int(float(RMCheading_deg))

        elif sentenceType == 'GSA':
            GSA = nmeadata.split(',')
            # GSAauto = GSA[1]
            GSAfixtype = GSA[2]
            # GSAfixtypes = {'none':1,'2D':2,'3D':3}
            prn = 1
            idx = 3
            GSAprns = ''
            while prn <= 12:
                GSAprns += GSA[idx]
                if prn < 12: GSAprns += ','
                prn += 1
                idx += 1
            GSApdop = GSA[15]
            GSAhdop = GSA[16]
            GSAvdop = GSA[17]
            loc.fixtype = int(GSAfixtype)
            loc.PDOP = max(int(float(GSApdop)), 32)     # values above 20 are bad; cap at 5-bit representation
            loc.HDOP = max(int(float(GSAhdop)), 32)
            loc.VDOP = max(int(float(GSAvdop)), 32)

        elif sentenceType == 'GSV':
            GSV = sentence.split(',')
            # GSVsentences = GSV[1]
            # GSVsentence = GSV[2]
            GSVsatellites = GSV[3]
            # GSVprn1 = GSV[4]
            # GSVel1 = GSV[5]
            # GSVaz1 = GSV[6]
            # GSVsnr1 = GSV[7]
            # up to 4 satellites total per sentence, each as above in successive indices
            loc.satellites = int(GSVsatellites)

        else:
            errStr = "NMEA sentence type not recognized"
    else:
        errStr = "invalid NMEA checksum"

    return errStr == '', errStr


def getsendlocation():
    """ Queries GPS NMEA strings from the modem and submits to a send/processing routine. """
    global log
    global modem
    global lockThread
    global trackingInterval_s

    # TODO: Enable or disable AT%TRK tracking mode based on update interval, to improve fix times
    staleSecs = int(trackingInterval_s/2)
    waitSecs = int(min(45, staleSecs - 1))
    NMEAsentences = '"GGA","RMC","GSA","GSV"'
    loc = Location()
    with lockThread:
        if _debug:
            log.debug("Thread: Requesting location")
        modem.GNSSStats['nGNSS'] += 1
        modem.GNSSStats['lastGNSSReqTime'] = int(time.time())
        response = at_getresponse('AT%GPS=' + str(staleSecs) + ',' + str(waitSecs) + ',' + NMEAsentences, atTimeout=waitSecs + 5)
        errCode, errStr = at_handleresultcode(response['resultCode'])
        if errCode == 0:
            gnssFixDuration = int(time.time()) - modem.GNSSStats['lastGNSSReqTime']
            if _debug:
                print("GNSS response time [s]: " + str(gnssFixDuration))
            if modem.GNSSStats['avgGNSSFixDuration'] > 0:
                modem.GNSSStats['avgGNSSFixDuration'] = int((gnssFixDuration + modem.GNSSStats['avgGNSSFixDuration'])/2)
            else:
                modem.GNSSStats['avgGNSSFixDuration'] = gnssFixDuration
            for res in response['response']:
                if at_clean(res).startswith('$GP'):     # TODO: confirm if this works for all GNSS systems
                    NMEAsentence = at_clean(res)
                    success, err = parseNMEAtoLocation(NMEAsentence, loc)
                    if not success: log('error', err)
        else:
            log.error("Unable to get GNSS (" + errStr + ")")
        sendlocation(loc)
    if _debug:
        log.debug("Next location report in ~" + str(trackingInterval_s) + " seconds.")
    return


def at_wait_boot(wait=15):
    """ Waits for key strings output by the modem on (re)boot and returns boolean for success 
    :param wait: an optional timeout in seconds
    :return: Boolean success
             error string on failure
    """
    # TODO: UNUSED...deprecate or improve handling
    global _debug
    global log
    global ser

    errStr = ''
    BOOT_MSG = 'uC Loader'
    AT_INIT_MSG = 'AT Command I/F'
    log.info("Waiting for boot initialization...")
    initVerified = False
    initTick = 0
    INIT_TIMEOUT = wait
    nLines = 0
    serOutLine = ''
    while initTick < INIT_TIMEOUT and not initVerified:
        time.sleep(1)
        if _debug: print("Countdown: " + str(INIT_TIMEOUT - initTick))
        while ser.inWaiting() > 0:
            rChar = ser.read(1)
            if rChar == '\n':
                nLines += 1
                if _debug:
                    rChar = '<lf>'
                    serOutLine += rChar
                    print('Received line: ' + serOutLine)
                if BOOT_MSG in serOutLine:
                    log.info("Modem booting...")
                    if initTick < 5: initTick = 5  # add a bit of extra time to complete
                elif AT_INIT_MSG in serOutLine:
                    log.info("AT command mode ready")
                    initVerified = True
                serOutLine = ''     # clear for next line parsing
            elif rChar == '\r':
                if _debug: rChar = '<cr>'
            serOutLine += rChar
        initTick += 1

    return initVerified, errStr


def init_windows(default_log_name):
    """TODO: Initializes for Windows testing by retrieving a COM port and log file name.
    :param default_log_name the name that will be used if nothing is selected
    :returns serial port name e.g. 'COM1'
            log file name e.g. 'myLogFile.log'
    """
    global _debug

    try:
        import Tkinter as tk
        import tkFileDialog
    except ImportError:
        sys.exit("Error importing Tkinter (Python 2.7) for COM port selection.")

    try:
        import serialportfinder
        serialportlist = serialportfinder.listports()
        if len(serialportlist) == 0 or serialportlist[0] == '':
            sys.exit("No serial COM ports found.")
    except ImportError:
        sys.exit("Error importing serialportfinder for COM port detection.")

    print("Windows test environment detected, enabling verbose debug.")
    _debug = True

    global ser_name
    portSelector = tk.Tk()
    portSelector.title("Select COM port")
    portSelector.geometry("220x70+30+30")
    selection = tk.StringVar(portSelector)
    selection.set(serialportlist[0])
    option = apply(tk.OptionMenu, (portSelector, selection) + tuple(serialportlist))
    option.grid(row=0, column=0, columnspan=2, padx=5, pady=5)

    def okSelect():
        global ser_name
        ser_name = selection.get()
        portSelector.quit()

    def on_closing():
        sys.exit('COM port selection cancelled.')

    buttonOk = tk.Button(portSelector, text='OK', command=okSelect, width=10)
    buttonOk.grid(row=1, column=0, padx=5, sticky='EW')

    buttonCancel = tk.Button(portSelector, text="Cancel", command=on_closing, width=10)
    buttonCancel.grid(row=1, column=1, padx=5, sticky='EW')

    portSelector.protocol('WM_DELETE_WINDOW', on_closing)
    portSelector.mainloop()
    portSelector.destroy()

    myFormats = [('Log', '*.log'), ('Text', '*.txt')]
    logfileSelector = tk.Tk()
    logfileSelector.withdraw()
    filename = tkFileDialog.asksaveasfilename(defaultextension='.log', initialfile=default_log_name,
                                                 parent=logfileSelector, filetypes=myFormats,
                                                 title="Save log file as...")
    logfileSelector.destroy()
    return ser_name, filename


def main():

    global _debug
    global log
    # global logfileName
    global ser
    global modem
    global lockThread
    global trackingInterval_s
    global SERIAL_NAME

    SERIAL_NAME = ''
    SERIAL_BAUD = 9600

    ser = None
    modem = None

    logfileName = 'IDP_at_interface.log'
    rpiHeadless = False
    useGUI = False

    AT_USE_CRC = False
    AT_WAIT = 0.1  # time between initialization commands

    # Timer intervals (seconds)
    SAT_STATUS_INTERVAL = 5
    MT_MESSAGECHECK_INTERVAL = 15
    trackingInterval_s = 900

    # Thread lock for background processes to avoid overlapping AT requests
    lockThread = threading.RLock()
    threads = []

    # Derive run options from command line
    parser = argparse.ArgumentParser(description="Interface with an IDP modem.")
    parser.add_argument('--log', dest='logfile',
                        help="the log file name with optional extension (default extension .log)")
    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        help="enable verbose debug logging (default OFF)")
    parser.add_argument('-t', '--track', dest='tracking', type=int, default=None,
                        help="location reporting interval in minutes (0..1440, default = 15, 0 = disabled)")
    parser.add_argument('--crc', dest='forceCRC', action='store_true',
                        help="force use of CRC on serial port (default OFF)")
    args = parser.parse_args()
    if args.logfile is not None:
        if not '.' in args.logfile:
            logfileName = args.logfile + '.log'
        else:
            logfileName = args.logfile
    _debug = args.debug
    if args.tracking is not None:
        if 0 <= args.tracking <= 1440:
            trackingInterval_s = int(args.tracking * 60)
        else:
            sys.exit("Invalid tracking interval, must be in range 0..1440")
    if args.forceCRC:
        AT_USE_CRC = True

    # Pre-initialization of platform
    try:
        # GPIO bindings (headless Raspberry Pi using FishDish I/O board)
        import RPi.GPIO as GPIO     # Successful import of this module implies running on Raspberry Pi
        # FishDish GPIO mapping
        GPIO_LED_GRN = 4
        GPIO_LED_ON = GPIO.HIGH
        GPIO_LED_OFF = GPIO.LOW
        # Other GPIO connected to modem for advanced use cases
        # GPIO_IDP_RESET_OUT = 21         # Assumed to connect to a relay (NC) hard reboot for modem power supply
        # GPIO_IDP_RESET_ASSERT = GPIO.HIGH
        # GPIO_IDP_NOTIFY_IN = TBD  # TODO: configure and optimize for use of the modem notification pin
        # GPIO_IDP_NOTIFY_ASSERT = GPIO.LOW
        logfileName = '/home/pi/' + logfileName
        SERIAL_NAME = '/dev/ttyUSB0'  # for Raspberry Pi pyserial version XX TODO: clarify what version/compatibility?
        rpiHeadless = True

    except ImportError:     # TODO: improve robustness...assumes exception is failure to import RPi.GPIO
        if sys.platform.lower().startswith('win32'):
            SERIAL_NAME, logfileName = init_windows(logfileName)

        elif sys.platform.lower().startswith('linux2'):
            log.debug("Linux environment detected. Assuming use on MultiTech Conduit AEP with serial mCard on AP1")
            logfileName = '/home/root/' + logfileName    # TODO: validate path availability
            subprocess.call('mts-io-sysfs store mfser/serial-mode rs232', shell=True)
            SERIAL_NAME = '/dev/ttyAP1'

        else:
            sys.exit('ERROR: Operation undefined on current platform. Please use Windows, RPi/GPIO or MultiTech AEP.')

    # Set up log file
    LOG_MAX_MB = 5
    log_formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d,(%(threadName)-10s),' \
                                          '[%(levelname)s],%(funcName)s(%(lineno)d),%(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
    log_handler = RotatingFileHandler(logfileName, mode='a', maxBytes=LOG_MAX_MB * 1024 * 1024,
                                      backupCount=2, encoding=None, delay=0)
    log_handler.setFormatter(log_formatter)
    log_handler.setLevel(logging.DEBUG)
    log = logging.getLogger(logfileName)
    log.setLevel(logging.DEBUG)
    log.addHandler(log_handler)

    if _debug:
        print("\n\n\n**** PROGRAM STARTING ****\n\n\n")

    try:
        if rpiHeadless:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(GPIO_LED_GRN, GPIO.OUT)
            GPIO.setup(GPIO_IDP_RESET, GPIO.OUT)
            GPIO.output(GPIO_LED_GRN, GPIO_LED_ON) # turn on LED
            rpiIndicatorOn = True

        # TODO: more robust serial configuration
        ser = serial.Serial(SERIAL_NAME, SERIAL_BAUD)
        log.info("Connected to " + ser.name + " at " + str(ser.baudrate) + " baud")

        if ser.isOpen():
            sys.stdout.flush()
            ser.flushInput()
            ser.flushOutput()

            modem = idpmodem.IDPModem()

            # Attempt to solicit AT response for some time before exiting
            # TODO: move this out to a watchdog recovery function, called after serial comms is lost (timeouts)
            initVerified = False
            initTick = 0
            INIT_TIMEOUT = 300  # 5 minutes to connect/reboot modem
            while initTick < INIT_TIMEOUT and not initVerified:
                response = at_getresponse('AT', atTimeout=3)
                # errCode, errStr = at_handleresultcode(response['resultCode'])
                if 'OK' in response['resultCode'] or at_clean(response['resultCode']) == '0' or (at_clean(response['resultCode']) == 'ERROR'):
                    initVerified = True
                    log.info("AT command mode confirmed")
                else:
                    if _debug: print("Attempting to establish AT response. Countdown: " + str(INIT_TIMEOUT - initTick))
                    initTick += 1
                # Flash visual indicator on RPi while waiting
                if rpiHeadless:
                    if rpiIndicatorOn:
                        GPIO.output(GPIO_LED_GRN, GPIO_LED_OFF)
                        rpiIndicatorOn = False
                    else:
                        GPIO.output(GPIO_LED_GRN, GPIO_LED_ON)
                        rpiIndicatorOn = True
                time.sleep(1)
            if not initVerified:
                errStr = 'Terminal AT mode could not be confirmed...exiting'
                log.error(errStr)
                sys.exit(errStr)

            # solid LED while running
            if rpiHeadless:
                GPIO.output(GPIO_LED_GRN, GPIO_LED_ON)

            # Restore saved defaults - modem AT config will also be inferred
            time.sleep(AT_WAIT)
            atVerified = False
            atVerAttempts = 0
            while not atVerified and atVerAttempts < 2:
                atVerAttempts += 1
                response = at_getresponse('ATZ')
                errCode, errStr = at_handleresultcode(response['resultCode'])
                if errCode == 100 and modem.atConfig['CRC'] == False:
                    modem.atConfig['CRC'] = True
                    log.info("ATZ CRC error; retrying with CRC enabled")
                elif errCode != 0:
                    errStr = "Failed to restore saved defaults - exiting (" + errStr + ")"
                    log.error(errStr)
                    sys.exit(errStr)
                else:
                    atVerified = True
                    log.info("ATZ response verified")

            # Enable CRC if desired
            if AT_USE_CRC:
                response = at_getresponse('AT%CRC=1')
                errCode, errStr = at_handleresultcode(response['resultCode'])
                if errCode == 0:
                    # modem.atConfig['CRC'] = True  # TODO: remove, handed by at_getresponse inference
                    log.info("CRC enabled")
                elif errCode == 100 and modem.atConfig['CRC']:
                    log.info("Attempted to set CRC when already set")
                else:
                    log.error("CRC enable failed (" + errStr + ")")
            elif modem.atConfig['CRC']:
                response = at_getresponse('AT%CRC=0')
                errCode, errStr = at_handleresultcode(response['resultCode'])
                if errCode == 0:
                    log.info("CRC disabled")
                else:
                    log.error("CRC disable failed (" + errStr + ")")

            # Ensure Quiet mode is disabled to receive response codes
            time.sleep(AT_WAIT)
            response = at_getresponse('ATS61?')     # S61 = Quiet mode
            errCode, errStr = at_handleresultcode(response['resultCode'])
            if errCode == 0:
                if response['response'][0] == '1':
                    response = at_getresponse('ATQ0')
                    errCode, errStr = at_handleresultcode(response['resultCode'])
                    if errCode != 0:
                        log.error("Could not disable Quiet mode (" + errStr + ")")
                        sys.exit("Failed to disable Quiet mode.")
                    else:
                        log.info("Quiet mode disabled")
            else:
                log.error("Failed query of Quiet mode S-register ATS61? (" + errStr + ")")
                sys.exit('Query of Quiet mode S-register failed.')
            modem.atConfig['Quiet'] = False

            # Enable echo to validate receipt of AT commands
            time.sleep(AT_WAIT)
            response = at_getresponse('ATE1')
            errCode, errStr = at_handleresultcode(response['resultCode'])
            if errCode == 0:
                log.info("Echo enabled")
            else:
                log.error("Echo enable failed (" + errStr + ")")

            # Enable verbose error code (OK / ERROR) setting TODO: precludes advanced handling of specific error cases
            time.sleep(AT_WAIT)
            response = at_getresponse('ATV1')
            errCode, errStr = at_handleresultcode(response['resultCode'])
            if errCode == 0:
                log.info("Verbose enabled")
                modem.atConfig['Verbose'] = True
            else:
                log.error("Verbose enable failed (" + errStr + ")")

            # Get modem ID
            time.sleep(AT_WAIT)
            response = at_getresponse('AT+GSN')
            errCode, errStr = at_handleresultcode(response['resultCode'])
            if errCode == 0:
                mobileID = at_clean(response["response"][0]).lstrip('+GSN:').strip()
                if mobileID != '':
                    log.info("Mobile ID: " + str(mobileID))
                    modem.mobileId = mobileID
                else:
                    log.warning("Mobile ID not returned")
            else:
                log.error("Get Mobile ID failed (" + errStr + ")")

            # (Proxy) Timer threads for background processes

            t = RepeatingTimer(SAT_STATUS_INTERVAL, target=checksatstatus, name='CheckSatStatus')
            checksatstatus()
            t.start()
            threads.append(t.name)

            t = RepeatingTimer(MT_MESSAGECHECK_INTERVAL, target=checkmtmessages, name='CheckMTMessages')
            t.start()
            threads.append(t.name)

            t = RepeatingTimer(trackingInterval_s, target=getsendlocation, name='GetSendLocation')
            getsendlocation()
            t.start()
            threads.append(t.name)

            ''' # TODO: User window interface thread to insert AT commands or send text messages
            if _debug and not rpiHeadless and useGUI:
                getuserinput()
            # '''

            while True:
                # TODO: handle loss and recovery of modem communications gracefully
                pass    # run forever

    except KeyboardInterrupt:
        log.info("Execution stopped by keyboard interrupt.")

    except Exception, e:
        errStr = "Error on line {}:".format(sys.exc_info()[-1].tb_lineno) + ',' + str(type(e)) + ',' + str(e)
        log.error(errStr)
        raise

    finally:
        log.info("idpmodemsample.py exiting")
        if modem is not None:
            statsList = modem.get_statistics()
            for stat in statsList:
                log.info(stat + ":" + str(statsList[stat]))
        for t in threading.enumerate():
            if t.name in threads:
                t.cancel()
        if rpiHeadless:
            GPIO.output(GPIO_LED_GRN, GPIO_LED_OFF)
            GPIO.cleanup()
        if ser is not None and ser.isOpen():
            ser.close()
            if _debug:
                print("Closing serial port " + SERIAL_NAME)
        if _debug:
            print("\n\n*** END PROGRAM ***\n\n")


if __name__ == "__main__":
    main()
