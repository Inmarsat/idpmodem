"""
Data structure and operations for a SkyWave/ORBCOMM IDP modem
"""

from collections import OrderedDict

_debug = False

class IDPModem(object):
    """ Abstracts attributes and statistics related to an IDP modem """

    ctrlStates = [
        'Stopped',
        'Waiting for GNSS fix',
        'Starting search',
        'Beam search',
        'Beam found',
        'Beam acquired',
        'Beam switch in progress',
        'Registration in progress',
        'Receive only',
        'Downloading Bulletin Board',
        'Active',
        'Blocked',
        'Confirm previously registered beam',
        'Confirm requested beam',
        'Connect to confirmed beam'
        ]

    atErrResultCodes = {
        '0': 'OK',
        '4': 'UNRECOGNIZED',
        '100': 'INVALID CRC SEQUENCE',
        '101': 'UNKNOWN COMMAND',
        '102': 'INVALID COMMAND PARAMETERS',
        '103': 'MESSAGE LENGTH EXCEEDS PERMITTED SIZE FOR FORMAT',
        '104': 'RESERVED',
        '105': 'SYSTEM ERROR',
        '106': 'INSUFFICIENT RESOURCES',
        '107': 'MESSAGE NAME ALREADY IN USE',
        '108': 'TIMEOUT OCCURRED',
        '109': 'UNAVAILABLE',
        '110': 'RESERVED',
        '111': 'RESERVED',
        '112': 'ATTEMPT TO WRITE READ-ONLY PARAMETER'
        }

    wakeupIntervals = {
        '5 seconds': 0,
        '30 seconds': 1,
        '1 minute': 2,
        '3 minute': 3,
        '10 minute': 4,
        '30 minute': 5,
        '60 minute': 6,
        '2 minute': 7,
        '5 minute': 8,
        '15 minute': 9,
        '20 minute': 10
    }
    
    def geteventnotifybitmap(self):
        eventBitMapStr = ''
        for key in self.eventNotifyBitMap:
            eventBitMapStr += str(int(self.eventNotifyBitMap[key]))
        EventNotifyBitMapInt = int('0b' + eventBitMapStr[::-1], 2)  # invert bitmask so bit 00 is least significant
        return EventNotifyBitMapInt
    
    def seteventnotifybitmap(self, EventNotifyBitMapInt):
        # Expects that EventNotifyBitMapInt is an integer value for register S88 passed or returned on the AT interface
        # Truncates the bitmap if too large
        eventNotifyBitMapStr = bin(EventNotifyBitMapInt[2:])
        if len(eventNotifyBitMapStr) > len(self.eventNotifyBitMap):
            eventNotifyBitMapStr = eventNotifyBitMapStr[:len(self.eventNotifyBitMap)-1]
        while len(eventNotifyBitMapStr) < len(self.eventNotifyBitMap):   # pad to 11 bits
            eventNotifyBitMapStr = '0' + eventNotifyBitMapStr
        i = len(self.eventNotifyBitMap) - 1  # Set index to iterate backwards through bitmask from MSB to LSB
        for key in self.eventNotifyBitMap:
            self.eventNotifyBitMap[key] = int(eventBitMapStr[i])
            i -= 1

    def seteventnotifybit(self, bitKey, bitValue):
        if bitKey in self.eventNotifyBitMap and isinstance(bitValue, bool):
            self.eventNotifyBitMap[bitKey] = bitValue
            return True
        else:
            return False

    def __init__(self):
        self.mobileId = ''
        self.atConfig = {
            'CRC': False,
            'Echo': True,
            'Verbose': True,
            'Quiet': False
        }
        self.satStatus = {
            'Registered': False,
            'Blocked': False,
            'RxOnly': False,
            'BBWait': False,
            'CtrlState': 'Stopped'
        }
        self.eventNotifyBitMap = OrderedDict({
            ('newGnssFix', False),
            ('newMtMsg', False),
            ('MoComplete', False),
            ('modemRegistered', False),
            ('modemReset', False),
            ('jamCutState', False),
            ('modemResetPending', False),
            ('lowPowerChange', False),
            ('utcUpdate', False),
            ('fixTimeout', False),
            ('eventCached', False)
        })
        self.wakeupInterval = 0
        self.asleep = False
        self.antennaCut = False
        self.broadcastIDs = []
        self.systemStats = {
            'nGNSS': 0,
            'nRegistration': 0,
            'nBBAcquisition': 0,
            'nBlockage': 0,
            'lastGNSSStartTime': 0,
            'lastRegStartTime': 0,
            'lastBBStartTime': 0,
            'lastBlockStartTime': 0,
            'avgGNSSFixDuration': 0,
            'avgRegistrationDuration': 0,
            'avgBBReacquireDuration': 0,
            'avgBlockageDuration': 0,
            'lastATResponseTime_ms': 0,
            'avgATResponseTime_ms': 0,
            'avgMOMsgLatency_s': 0,
            'avgMOMsgSize': 0,
            'avgMTMsgSize': 0,
            'avgCN0': 0
        }
        self.GNSSStats = {
            'nGNSS': 0,
            'lastGNSSReqTime': 0,
            'avgGNSSFixDuration': 0
        }
        self.atCmdStats = {
            'lastResTime': 0,
            'avgResTime': 0
        }
        self.MOqueue = []
        self.MTqueue = []
        self.hardwareversion = '0'
        self.softwareversion = '0'
    
    def display_atconfig(self):
        print('Modem AT Configuration: ' + self.mobileId)
        print(' %CRC=' + str(int(self.atConfig['CRC'])))
        print('  ATE=' + str(int(self.atConfig['Echo'])))
        print('  ATV=' + str(int(self.atConfig['Verbose'])))
        print('  ATQ=' + str(int(self.atConfig['Quiet'])))

    def display_satstatus(self):
        print('Satellite Status: ' + self.mobileId)
        for subattr in self.satStatus:
            print('  ' + subattr + ": " + str(self.satStatus[subattr]))

    def get_statistics(self):
        statList = {
            'GNSS control fixes': str(self.systemStats['nGNSS']),
            'Average GNSS time to fix': str(self.systemStats['avgGNSSFixDuration']),
            'Registrations': str(self.systemStats['nRegistration']),
            'Average Registration time': str(self.systemStats['avgRegistrationDuration']),
            'BB acquisitions': str(self.systemStats['nBBAcquisition']),
            'Average BB acquisition time': str(self.systemStats['avgBBReacquireDuration']),
            'Blockages': str(self.systemStats['nBlockage']),
            'Average Blockage duration': str(self.systemStats['avgBlockageDuration']),
            'GNSS application fixes': str(self.GNSSStats['nGNSS']),
            'Average GNSS time to fix (application)': str(self.GNSSStats['avgGNSSFixDuration']),
            'Average AT response time [ms]': str(self.systemStats['avgATResponseTime_ms']),
            'Average Mobile-Originated message size [bytes]': str(self.systemStats['avgMOMsgSize']),
            'Average Mobile-Originated message latency [s]': str(self.systemStats['avgMOMsgLatency_s']),
            'Average Mobile-Terminated message size [bytes]': str(self.systemStats['avgMTMsgSize']),
            'Average C/N0': str(self.systemStats['avgCN0'])
        }
        return statList

    def display_statistics(self):
        print('System statistics: ' + self.mobileId)
        print('  GNSS fixes (for control): ' + str(self.systemStats['nGNSS']))
        print('    Average GNSS acquisition duration [s]: ' + str(self.systemStats['avgGNSSFixDuration']))
        print('  Registrations: ' + str(self.systemStats['nRegistration']))
        print('    Average Registration Duration [s]: ' + str(self.systemStats['avgRegistrationDuration']))
        print('  Bulletin Board Acquisitions: ' + str(self.systemStats['nBBAcquisition']))
        print('    Average BB Reacquisition Duration [s]: ' + str(self.systemStats['avgBBReacquireDuration']))
        print('  Blockages: ' + str(self.systemStats['nBlockage']))
        print('    Average Blockage Duration [s]: ' + str(self.systemStats['avgBlockageDuration']))
        print('  GNSS fixes (application): ' + str(self.GNSSStats['nGNSS']))
        print('    Average GNSS acquisition time [s]: ' + str(self.GNSSStats['avgGNSSFixDuration']))
        print('  Average AT Response Duration [ms]: ' + str(self.systemStats['avgATResponseTime_ms']))
        print('  Average MO message size [bytes]: ' + str(self.systemStats['avgMOMsgSize']))
        print('  Average MO message latency [s]: ' + str(self.systemStats['avgMOMsgLatency_s']))
        print('  Average MT message size [bytes]: ' + str(self.systemStats['avgMTMsgSize']))
        print('  Average C/N0: ' + str(self.systemStats['avgCN0']))


class IDPMsg(object):
    """ Class intended for abstracting message characteristics """
    # TODO: unused
    
    msgPriorities = {
        'HIGH': 1,
        'MIDH': 2,
        'MIDL': 3,
        'LOW': 4
        }
    
    msgDataFormats = {
        'Reserved': 0,
        'Text': 1,
        '0xAA': 2,
        'Base64': 3
        }
    
    def __init__(self, msgName, msgSin=255, msgMin=255, msgPriority=4, msgFormat=1, msgPayload=''):
        self.name = msgName
        self.sin = msgSin
        
        msgSize = 1 # sin byte
        if msgPayload != '':
            if msgFormat == 1:                                              # Text
                if msgMin == '': msgMin = ord(msgPayload[1:1])
                else: msgSize += 1
                msgSize += len(msgPayload)
            elif msgFormat == 2:                                            # ASCII-Hex
                if msgMin == '': msgMin = msgPayload[1:2]
                else: msgSize += 1
                msgSize += int(len(msgPayload)/2)
            elif msgFormat == 3:                                            # base64
                if msgMin == '': msgMin = ord(base64.b64decode(msgPayload)[1:1])
                else: msgSize += 1
                msgSize += len(base64.b64decode(msgPayload))
        else:
            pass    # this case will generate a modem error invalid parameters (no min and no payload)
            
        self.min = msgMin
        self.priority = msgPriority
        self.format = msgFormat
        self.payload = msgPayload
        self.size = msgSize
    
    class MOMsg(object):
        'Class containing Mobile Originated (aka Forward) message properties'
        global moMsgStates
        moMsgStates = [
            'Unavailable',
            'Undefined1',
            'Undefined2',
            'Undefined3',
            'Ready',
            'Sending',
            'Complete',
            'Failed'
            ]
        
        def __init__(self, moState):
            self.moState = 'Unavailable'
            
    class MTMsg(object):
        'Class containing Mobile Originated (aka Forward) message properties'
        global mtMsgStates
        mtMsgStates = [
            'Unavailable',
            'Undefined1',
            'Complete',
            'Retrieved'
            ]
        
        def __init__(self, moState):
            self.moState = 'Unavailable'
            

if __name__ == "__main__":
    _debug = True
    modem = IDPModem()
    modem.mobileId = '00000000SKYEE3D'
    modem.display_atconfig()
    modem.display_satstatus()