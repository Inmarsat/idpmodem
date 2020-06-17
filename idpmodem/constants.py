from collections import OrderedDict

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

CONTROL_STATES = {
    0: 'Stopped',
    1: 'Waiting for GNSS fix',
    2: 'Starting search',
    3: 'Beam search',
    4: 'Beam found',
    5: 'Beam acquired',
    6: 'Beam switch in progress',
    7: 'Registration in progress',
    8: 'Receive only',
    9: 'Downloading Bulletin Board',
    10: 'Active',
    11: 'Blocked',
    12: 'Confirm previously registered beam',
    13: 'Confirm requested beam',
    14: 'Connect to confirmed beam'
}

AT_ERROR_CODES = {
    '0': 'OK',
    '4': 'ERROR',
    '100': 'ERR_INVALID_CRC_SEQUENCE',
    '101': 'ERR_UNKNOWN_COMMAND',
    '102': 'ERR_INVALID_COMMAND_PARAMETERS',
    '103': 'ERR_MESSAGE_LENGTH_EXCEEDS_FORMAT_SIZE',
    '104': 'ERR_RESERVED_104',
    '105': 'ERR_SYSTEM_ERROR',
    '106': 'ERR_INSUFFICIENT_RESOURCES',
    '107': 'ERR_MESSAGE_NAME_ALREADY_IN_USE',
    '108': 'ERR_TIMEOUT_OCCURRED',
    '109': 'ERR_UNAVAILABLE',
    '110': 'ERR_RESERVED_110',
    '111': 'ERR_RESERVED_111',
    '112': 'ERR_ATTEMPT_TO_WRITE_READ_ONLY_PARAMETER'
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

NOTIFICATION_BITMASK = (
    'gnss_fix_new',
    'message_mt_received',
    'message_mo_complete',
    'network_registered',
    'modem_reset',
    'jamming_antenna_change',
    'modem_reset_pending',
    'wakeup_period_changed',
    'utc_time_set',
    'gnss_fix_timeout',
    'event_cached',
    'network_ping_acknowledged'
)
