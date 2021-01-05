# -*- coding: utf-8 -*-
"""IsatData Pro modem constants.

This module provides mapping of constants used within an IDP modem.

"""

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

BEAMSEARCH_STATES = {
    0: 'Idle',
    1: 'Search for any traffic channel',
    2: 'Search for last acquired traffic channel',
    3: 'reserved',
    4: 'Search for another traffic channel',
    5: 'Search for bulletin board',
    6: 'Delay traffic channel search'
}

MESSAGE_STATES = {
    0: 'UNAVAILABLE',
    1: 'RX_PENDING',
    2: 'RX_COMPLETE',
    3: 'RX_RETRIEVED',
    4: 'TX_READY',
    5: 'TX_SENDING',
    6: 'TX_COMPLETE',
    7: 'TX_FAILED',
    8: 'TX_CANCELLED'
}

AT_ERROR_CODES = {
    0: 'OK',
    4: 'ERROR',
    100: 'ERR_INVALID_CRC_SEQUENCE',
    101: 'ERR_UNKNOWN_COMMAND',
    102: 'ERR_INVALID_COMMAND_PARAMETERS',
    103: 'ERR_MESSAGE_LENGTH_EXCEEDS_FORMAT_SIZE',
    104: 'ERR_RESERVED_104',
    105: 'ERR_SYSTEM_ERROR',
    106: 'ERR_QUEUE_INSUFFICIENT_RESOURCES',
    107: 'ERR_MESSAGE_NAME_ALREADY_IN_USE',
    108: 'ERR_TIMEOUT_OCCURRED',
    109: 'ERR_UNAVAILABLE',
    110: 'ERR_RESERVED_110',
    111: 'ERR_RESERVED_111',
    112: 'ERR_ATTEMPT_TO_WRITE_READ_ONLY_PARAMETER'
}

WAKEUP_PERIODS = {
    0: 'SECONDS_5',
    1: 'SECONDS_30',
    2: 'MINUTES_1',
    3: 'MINUTES_3',
    4: 'MINUTES_10',
    5: 'MINUTES_30',
    6: 'MINUTES_60',
    7: 'MINUTES_2',
    8: 'MINUTES_5',
    9: 'MINUTES_15',
    10: 'MINUTES_20'
}

POWER_MODES = {
    0: 'MOBILE_POWERED',
    1: 'FIXED_POWERED',
    2: 'MOBILE_BATTERY',
    3: 'FIXED_BATTERY',
    4: 'MOBILE_MINIMAL',
    5: 'MOBILE_PARKED'
}

GNSS_MODES = {
    'GPS': 0,               # HW v4
    'GLONASS': 1,           # HW v5
    'BEIDOU': 2,            # HW v5.2
    'GPS+GLONASS': 10,      # UBX-M80xx
    'GPS+BEIDOU': 11,       # UBX-M80xx
    'GLONASS+BEIDOU': 12    # UBX-M80xx
}

GNSS_DPM_MODES = {
    0: 'PORTABLE',
    2: 'STATIONARY',
    3: 'PEDESTRIAN',
    4: 'AUTOMOTIVE',
    5: 'SEA',
    6: 'AIR_1G',
    7: 'AIR_2G',
    8: 'AIR_4G'
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

NOTIFICATION_BITMASK_2 = {
    0: 'GNSS_NEW_FIX',
    1: 'MESSAGE_MT_RECEIVED',
    2: 'MESSAGE_MO_COMPLETE',
    3: 'NETWORK_REGISTERED',
    4: 'MODEM_RESET',
    5: 'JAMMING_ANTENNA_CHANGE',
    6: 'MODEM_RESET_PENDING',
    7: 'WAKEUP_PERIOD_CHANGED',
    8: 'TIME_UTC_SYNC',
    9: 'GNSS_TIMEOUT',
    10: 'TRACE_EVENT_CACHED',
    11: 'NETWORK_PING_ACKNOWLEDGED'
}

TRANSMIT_STATUS = {
    4: 'RX_ONLY_NOT_REGISTERED',
    5: 'OK',
    6: 'SUSPENDED',
    7: 'MUTED',
    8: 'BLOCKED'
}

SATELLITE_GENERAL_TRACE = (
    {'subframe_number': 'uint'},
    {'traffic_channel_id': 'uint'},
    {'configuration_id': 'uint'},
    {'beam_number': 'uint'},
    {'reserved04': 'uint'},
    {'reserved05': 'uint'},
    {'operator_tx_access': 'uint'},
    {'user_tx_mute': 'uint'},
    {'tx_suspend_flags': {0: 'beam_registration', 1: 'beam_switch', 2: 'reserved', 3: 'blocked'}},
    {'active_tx_messages': 'uint'},
    {'total_tx_messages': 'uint'},
    {'tx_state': ('active', 'suspending', 'suspended')},
    {'active_rx_messages': 'uint'},
    {'beamswitch_averaging_window': 'uint'},
    {'beamswitch_averaging_count': 'uint'},
    {'c_n_x100': 'uint'},
    {'beamsample_threshold': 'uint'},
    {'beamsample_timer': 'uint'},
    {'flags': {
        0: 'registered',
        1: 'sending_beam_registration',
        4: 'beam_search',
        5: 'need_beam_sample',
        6: 'beam_switch_pending',
        8: 'gnss_valid',
        9: 'need_gnss',
        10: 'requested_gnss'
    }},
    {'gnss_state_timer': 'uint'},
    {'reserved21': 'uint'},
    {'satellite_control_state': (
        'stopped',
        'gnss_wait',
        'beam_search_start',
        'beam_search',
        'beam_found',
        'beam_acquired',
        'beam_switch_pending',
        'registration_pending',
        'receive_only',
        'bulletinboard_receive',
        'active',
        'blocked',
        'confirm_previous_registered_beam',
        'confirm_requested_beam',
        'connect_confirmed_beam'
    )},
    {'beam_search_state': (
        'idle',
        'search_any_traffic',
        'search_last_traffic',
        'reserved',
        'search_other_traffic',
        'search_bulletinboard',
        'delay_search_traffic'
    )}
)