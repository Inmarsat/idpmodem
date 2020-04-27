# Message Priorities and Data Formats
PRIORITY_MT, PRIORITY_HIGH, PRIORITY_MEDH, PRIORITY_MEDL, PRIORITY_LOW = (
    0, 1, 2, 3, 4)
FORMAT_TEXT, FORMAT_HEX, FORMAT_B64 = (1, 2, 3)
# Message States
UNAVAILABLE = 0
RX_COMPLETE = 2
RX_RETRIEVED = 3
TX_READY = 4
TX_SENDING = 5
TX_COMPLETE = 6
TX_FAILED = 7
# Wakeup Intervals
WAKEUP_5_SEC = 0
WAKEUP_30_SEC = 1
WAKEUP_1_MIN = 2
WAKEUP_3_MIN = 3
WAKEUP_10_MIN = 4
WAKEUP_30_MIN = 5
WAKEUP_60_MIN = 6
WAKEUP_2_MIN = 7
WAKEUP_5_MIN = 8
WAKEUP_15_MIN = 9
WAKEUP_20_MIN = 10
WAKEUP_INTERVALS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
# Power Mode settings (S50)
POWER_MODE_MOBILE_POWERED = 0
POWER_MODE_FIXED_POWERED = 1
POWER_MODE_MOBILE_BATTERY = 2
POWER_MODE_FIXED_BATTERY = 3
POWER_MODE_MOBILE_MINIMAL = 4
POWER_MODE_MOBILE_STATIONARY = 5
POWER_MODES = (0, 1, 2, 3, 4, 5)
# GNSS Modes
GNSS_MODES = (0, 1, 2, 10, 11, 12)
GNSS_MODE_GPS = 0
GNSS_MODE_GLONASS = 1
GNSS_MODE_BEIDOU = 2
GNSS_MODE_GPS_GLONASS = 10
GNSS_MODE_GPS_BEIDOU = 11
GNSS_MODE_GLONASS_BEIDOU = 12
# GNSS Dynamic Platform Models
GNSS_DPM_MODES = (0, 2, 3, 4, 5, 6, 7, 8)
GNSS_DPM_PORTABLE = 0
GNSS_DPM_STATIONARY = 2
GNSS_DPM_PEDESTRIAN = 3
GNSS_DPM_AUTOMOTIVE = 4
GNSS_DPM_SEA = 5
GNSS_DPM_AIR_1G = 6
GNSS_DPM_AIR_2G = 7
GNSS_DPM_AIR_4G = 8

CONTROL_STATES = (
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
)

at_err_result_codes = {
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

wakeup_intervals = {
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

power_modes = {
    'Mobile Powered': 0,
    'Fixed Powered': 1,
    'Mobile Battery': 2,
    'Fixed Battery': 3,
    'Mobile Minimal': 4,
    'Mobile Stationary': 5
}

gnss_modes = {
    'GPS': 0,               # HW v4
    'GLONASS': 1,           # HW v5
    'BEIDOU': 2,            # HW v5.2
    'GPS+GLONASS': 10,      # UBX-M80xx
    'GPS+BEIDOU': 11,       # UBX-M80xx
    'GLONASS+BEIDOU': 12    # UBX-M80xx
}

gnss_dpm_modes = {
    'Portable': 0,
    'Stationary': 2,
    'Pedestrian': 3,
    'Automotive': 4,
    'Sea': 5,
    'Air 1g': 6,
    'Air 2g': 7,
    'Air 4g': 8
}
