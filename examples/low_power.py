"""
Incomplete work in progress
"""

'''
    POWER_ON
    |
+-> Wait_5s <---------------------------------+
|   |                                         |
|   GetStatus                                 |
|   |                                         |
|   <SatCtrlState == 10> -Y- READY.           |
|   |                                         |
|   <SatCtrlState < 3> -Y- <t < 3 minutes> -Y-+
|   |                                   |
+-Y-<SatCtrlState > 3>                  |
|   |                                   |
+-Y-<BeamSearch > 0>                    |
|   |                                   |
|   <BeamSearch duration > 2 minutes>-Y-+
|   |                                   !
|   <BeamSearch count > 2> ----------POWER_OFF
+---+

* Use RPI GPIO for hardware notifications
* Notifications for:
    * Forward message received
    * Return message complete/failed
    * Wakeup period changed
    * Event cached
* Events monitored:
    * Low Power Event (2.2)
    * Satellite Event (3.1)

* Log Daily statistics?
    * System Stats (2.3)
    * System SatCom Stats (2.4)
'''

from time import time, sleep

MAX_GPS_TIMEOUT = 180
BEAM_SEARCH_DURATION_THRESHOLD = 120
MAX_BEAM_SEARCH_COUNT = 2
ACQUIRE_STATES = {
    0: 'REGISTERED',
    1: 'GPS_BLOCKED',
    2: 'BEAM_ACQUIRE_FAILED_DURATION',
    3: 'BEAM_ACQUIRE_FAILED_ATTEMPTS',
    -1: 'UNKNOWN'
}

def acquire_satellite():
    start_time = time()
    beam_search_time = 0
    beam_search_count = 0
    sat_ctrl_state = 0
    beam_search_state = 0
    while True:
        # get_satctrl_beamsearch
        if sat_ctrl_state == 10:
            return True, 'REGISTERED'
        elif sat_ctrl_state < 3:
            if time() - start_time > MAX_GPS_TIMEOUT:
                return False, 'GPS_BLOCKED'
        elif sat_ctrl_state > 3:
            start_time = time()
            beam_search_count = 0
        elif beam_search_state > 0:
            if beam_search_time == 0:
                beam_search_count += 1
                beam_search_time = time()
            elif time() - beam_search_time >= BEAM_SEARCH_DURATION_THRESHOLD:
                return False, 'BEAM_ACQUIRE_FAILED_DURATION'
        elif beam_search_count > MAX_BEAM_SEARCH_COUNT:
            return False, 'BEAM_ACQUIRE_FAILED_ATTEMPTS'
        sleep(5)

def monitor_events():
    pass