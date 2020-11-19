"""Utilities for parsing NMEA data into a location object

"""

from datetime import datetime
from functools import reduce
import operator
import time


class Location(object):
    """
    A class containing a specific set of location-based information for a given point in time.
    Uses 90.0/180.0 if latitude/longitude are unknown

    ..todo::
       Implement logging

    :param latitude: decimal degrees
    :param longitude: decimal degrees
    :param altitude: in metres
    :param speed: in knots
    :param heading: in degrees
    :param timestamp: in seconds since 1970-01-01T00:00:00Z
    :param satellites: in view at time of fix
    :param fix_type: 1=None, 2=2D or 3=3D

    """

    def __init__(self, latitude=90.0, longitude=180.0, altitude=0.0,
                 speed=0.0, heading=0.0, timestamp=0, satellites=0, fix_type=1):
        """
        Creates a Location instance with default latitude/longitude 91/181 *unknown*

        :param latitude: decimal degrees
        :param longitude: decimal degrees
        :param altitude: in metres
        :param speed: in knots
        :param heading: in degrees
        :param timestamp: in seconds since 1970-01-01T00:00:00Z
        :param satellites: in view at time of fix
        :param fix_type: 1=None, 2=2D or 3=3D

        """
        self.latitude = latitude
        self.longitude = longitude
        self.resolution = 6
        self.altitude = altitude  # metres
        self.speed = speed  # knots
        self.heading = heading  # degrees
        self.timestamp = timestamp  # seconds since 1/1/1970 unix epoch
        self.satellites = satellites
        self.fix_type = fix_type
        self.pdop = 99.9
        self.hdop = 99.9
        self.vdop = 99.9
        self.time_iso = datetime.utcfromtimestamp(timestamp).isoformat()
        self.satellites_info = []

    class GnssSatelliteInfo(object):
        def __init__(self, prn, elevation, azimuth, snr):
            self.prn = prn
            self.elevation = elevation
            self.azimuth = azimuth
            self.snr = snr

    def update_satellites_info(self, satellites_info):
        for satellite_info in satellites_info:
            if isinstance(satellite_info, self.GnssSatelliteInfo):
                new = True
                for info in self.satellites_info:
                    if info.prn == satellite_info.prn:
                        new = False
                        info = satellite_info
                        break
                if new:
                    self.satellites_info.append(satellite_info)
    
    def isotime(self):
        self.time_iso = (datetime.utcfromtimestamp(self.timestamp).isoformat() +
                         'Z')


def validate_nmea_checksum(sentence):
    """
    Validates NMEA sentence using checksum according to the standard.

    :param sentence: NMEA sentence including checksum
    :returns:
       - Boolean result (checksum correct)
       - raw NMEA data string, with prefix $Gx and checksum suffix removed

    """
    sentence = sentence.strip('\n').strip('\r')
    nmeadata, cksum = sentence.split('*', 1)
    nmeadata = nmeadata.replace('$', '')
    xcksum = str("%0.2x" % (reduce(operator.xor, (ord(c) for c in nmeadata), 0))).upper()
    return (cksum == xcksum), nmeadata[2:]


class NmeaException(Exception):
    pass


def location_get(nmea_data_set, degrees_resolution=6):
    loc = Location()
    for sentence in nmea_data_set:
        if not sentence.startswith('$G'):
            raise Exception('location_get found invalid NMEA string {}'.format(sentence))
        valid, nmea_data = validate_nmea_checksum(sentence)
        if not valid:
            raise Exception('Invalid NMEA checksum for {}'.format(sentence))
        sentence_type = nmea_data[0:3]
        if sentence_type == 'GGA':          # GGA is essential fix information for 3D location and accuracy
            gga = nmea_data.split(',')      # $GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*4
            '''
            gga_utc_hhmmss = gga[1]         # Fix taken at 12:35:19 UTC
            gga_latitude_dms = gga[2]       # Latitude 48 deg 07.038'
            gga_latitude_ns = gga[3]        # Latitude N
            gga_longitude_dms = gga[4]      # Longitude 11 deg 31.000'
            gga_longitude_ew = gga[5]       # Longitude E
            gga_quality = gga[6]            # Fix quality
            gga_fix_qualities = (
                'invalid',                  # 0 = invalid
                'GPS fix',                  # 1 = GPS fix (SPS)
                'DGPS fix',                 # 2 = DGPS fix
                'PPS fix',                  # 3 = PPS fix
                'RTK',                      # 4 = Real Time Kinematic
                'Float RTK',                # 5 = Float RTK
                'Estimated',                # 6 = estimated (dead reckoning)
                'Manual',                   # 7 = Manual input mode
                'Simulation'                # 8 = Simulation mode
            )
            '''
            gga_satellites = gga[7]         # Number of satellites being tracked
            gga_hdop = gga[8]               # Horizontal dilution of precision
            gga_altitude = gga[9]           # Altitude above mean sea level
            '''
            gga_altitude_unit = gga[10]     # Altitude units (M)eters
            gga_height_wgs84 = gga[11]      # Height of geoid (mean sea level) above WGS84 ellipsoid
            gga_height_unit = gga[12]       # Height units (M)eters
            gga_dgps_update_time = gga[13]  # Time in seconds since last DGPS update
            gga_dgps_station = gga[14]      # DGPS station ID number
            '''
            satellites = int(gga_satellites)
            if loc.satellites < satellites:
                loc.satellites = satellites
            else:
                # TODO: log this case; should be limited to GPS simulation in Modem Simulator (3 satellites)
                pass
            loc.altitude = float(gga_altitude) if gga_altitude != '' else 0.0
            loc.hdop = max(float(gga_hdop), 32.0) if gga_hdop != '' else 32.0

        elif sentence_type == 'RMC':          # RMC Recommended Minimum is used for most location information
            rmc = nmea_data.split(',')        # $GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A
            rmc_fixtime_utc_hhmmss = rmc[1]   # fix taken at 12:35:19 UTC
            # rmc_active = RMC[2]             # status (A)ctive or (V)oid
            rmc_latitude_dms = rmc[3]         # 4807.038 = 48 deg 07.038'
            rmc_latitude_ns = rmc[4]          # (N)orth or (S)outh
            rmc_longitude_dms = rmc[5]        # 01131.000 = 11 deg 31.000'
            rmc_longitude_ew = rmc[6]         # (E)east or (W)est
            rmc_speed_knots = rmc[7]          # 022.4 = 22.4 knots
            rmc_heading_deg_true = rmc[8]     # 084.4 = 84.4 degrees True
            rmc_date_ddmmyy = rmc[9]          # date 23rd of March 1994
            '''
            rmc_mag_var_mag = rmc[10]         # Magnetic Variation (magnitude)
            rmc_mag_var_dir = rmc[11]         # Magnetic Variation (direction)
            '''
            # Convert text values to workable numbers
            year = int(rmc_date_ddmmyy[4:6]) + 2000
            month = int(rmc_date_ddmmyy[2:4])
            day = int(rmc_date_ddmmyy[0:2])
            hour = int(rmc_fixtime_utc_hhmmss[0:2])
            minute = int(rmc_fixtime_utc_hhmmss[2:4])
            second = int(rmc_fixtime_utc_hhmmss[4:6])
            dt = datetime(year, month, day, hour, minute, second)
            loc.timestamp = int(time.mktime(dt.timetuple()))
            # Convert to decimal degrees latitude/longitude
            if rmc_longitude_dms != '' and rmc_longitude_dms != '':
                loc.latitude = round(float(rmc_latitude_dms[0:2]) + float(rmc_latitude_dms[2:]) / 60.0, degrees_resolution)
                if rmc_latitude_ns == 'S':
                    loc.latitude *= -1
                loc.longitude = round(float(rmc_longitude_dms[0:3]) + float(rmc_longitude_dms[3:]) / 60.0, degrees_resolution)
                if rmc_longitude_ew == 'W':
                    loc.longitude *= -1
            loc.speed = float(rmc_speed_knots) if rmc_speed_knots != '' else 0.0  # multiply by 1.852 for kph
            loc.heading = float(rmc_heading_deg_true) if rmc_heading_deg_true != '' else 0.0
            loc.isotime()

        elif sentence_type == 'GSA':                    # GSA is used for DOP and active satellites
            gsa = nmea_data.split(',')                  # $GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39
            #: gsa_auto = gsa[1]                           # Auto selection of 2D or 3D fix (M = manual)
            gsa_fix_type = gsa[2]                       # 3D fix type
            '''
            gsa_fix_types = {
                'none': 1,
                '2D': 2,
                '3D': 3
            }
            '''
            gsa_prns = []                               # PRNs of satellites used for fix (space for 12)
            for prn in range(1, 12):
                gsa_prns.append(gsa[prn+2])             # offset of prns in the split array is [3]
            gsa_pdop = gsa[15]                          # Probability dilution of precision (DOP), above 20 is bad
            #: gsa_hdop = gsa[16]                          # Horizontal DOP
            gsa_vdop = gsa[17]                          # Vertical DOP
            # Use GSA for fix_type, PDOP, VDOP (HDOP comes from GGA)
            loc.fix_type = int(gsa_fix_type) if gsa_fix_type != '' else 0
            loc.pdop = max(float(gsa_pdop), 32.0) if gsa_pdop != '' else 32.0
            loc.vdop = max(float(gsa_vdop), 32.0) if gsa_vdop != '' else 32.0

        elif sentence_type == 'GSV':         # Satellites in View
            gsv = nmea_data.split(',')       # $GPGSV,2,1,08,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45*75
            '''
            gsv_sentences = gsv[1]           # Number of sentences for full data
            gsv_sentence = gsv[2]            # Sentence number (up to 4 satellites per sentence)
            '''
            gsv_satellites = gsv[3]          # Number of satellites in view
            # following supports up to 4 satellites per sentence
            satellites_info = []
            if (len(gsv) - 4) % 4 > 0:
                # TODO: warn/log this case of extra GSV data in sentence
                pass
            num_satellites_in_sentence = int((len(gsv)-4)/4)
            for i in range(1, num_satellites_in_sentence+1):
                prn = int(gsv[i*4]) if gsv[i*4] != '' else 0             # satellite PRN number
                elevation = int(gsv[i*4+1]) if gsv[i*4+1] != '' else 0   # Elevation in degrees
                azimuth = int(gsv[i*4+2]) if gsv[i*4+2] != '' else 0     # Azimuth in degrees
                snr = int(gsv[i*4+3]) if gsv[i*4+3] != '' else 0         # Signal to Noise Ratio
                satellites_info.append(Location.GnssSatelliteInfo(prn, elevation, azimuth, snr))
            loc.update_satellites_info(satellites_info)
            satellites = int(gsv_satellites) if gsv_satellites != '' else 0
            if loc.satellites < satellites:
                loc.satellites = satellites
            else:
                # TODO: log this case; should be limited to GPS simulation in Modem Simulator (3 satellites)
                pass

        else:
            error = "{} NMEA sentence type not recognized".format(sentence[0:3])
            raise NmeaException(error)
    return loc

'''
def parse_nmea_to_location(nmea_data_set, loc=None, degrees_resolution=6):
    """
    TODO: cleanup and exception handling
    Parses a NMEA string to partially populate a ``Location`` object.
    Several sentence parameters are unused but remain as placeholders for completeness/future use.

    :param nmea_data_set: list of NMEA sentences (including prefix and suffix)
    :param loc: the Location object to be populated
    :param degrees_resolution: (int) the number of decimal places to use for latitude/longitude
    :returns:
       - Boolean success of operation
       - error string if not successful

    """
    if loc is None:
        loc = Location()
    err_str = ''
    for sentence in nmea_data_set:
        res, nmea_data = validate_nmea_checksum(sentence)
        if res:
            sentence_type = nmea_data[0:3]
            if sentence_type == 'GGA':          # GGA is essential fix information for 3D location and accuracy
                gga = nmea_data.split(',')      # $GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*4
                gga_utc_hhmmss = gga[1]         # Fix taken at 12:35:19 UTC
                gga_latitude_dms = gga[2]       # Latitude 48 deg 07.038'
                gga_latitude_ns = gga[3]        # Latitude N
                gga_longitude_dms = gga[4]      # Longitude 11 deg 31.000'
                gga_longitude_ew = gga[5]       # Longitude E
                gga_quality = gga[6]            # Fix quality
                gga_fix_qualities = (
                    'invalid',                  # 0 = invalid
                    'GPS fix',                  # 1 = GPS fix (SPS)
                    'DGPS fix',                 # 2 = DGPS fix
                    'PPS fix',                  # 3 = PPS fix
                    'RTK',                      # 4 = Real Time Kinematic
                    'Float RTK',                # 5 = Float RTK
                    'Estimated',                # 6 = estimated (dead reckoning)
                    'Manual',                   # 7 = Manual input mode
                    'Simulation'                # 8 = Simulation mode
                )
                gga_satellites = gga[7]         # Number of satellites being tracked
                gga_hdop = gga[8]               # Horizontal dilution of precision
                gga_altitude = gga[9]           # Altitude above mean sea level
                gga_altitude_unit = gga[10]     # Altitude units (M)eters
                gga_height_wgs84 = gga[11]      # Height of geoid (mean sea level) above WGS84 ellipsoid
                gga_height_unit = gga[12]       # Height units (M)eters
                gga_dgps_update_time = gga[13]  # Time in seconds since last DGPS update
                gga_dgps_station = gga[14]      # DGPS station ID number
                satellites = int(gga_satellites)
                if loc.satellites < satellites:
                    loc.satellites = satellites
                else:
                    # TODO: log this case; should be limited to GPS simulation in Modem Simulator (3 satellites)
                    pass
                loc.altitude = float(gga_altitude) if gga_altitude != '' else 0.0
                loc.hdop = max(float(gga_hdop), 32.0) if gga_hdop != '' else 32.0

            elif sentence_type == 'RMC':          # RMC Recommended Minimum is used for most location information
                rmc = nmea_data.split(',')        # $GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A
                rmc_fixtime_utc_hhmmss = rmc[1]   # fix taken at 12:35:19 UTC
                # rmc_active = RMC[2]             # status (A)ctive or (V)oid
                rmc_latitude_dms = rmc[3]         # 4807.038 = 48 deg 07.038'
                rmc_latitude_ns = rmc[4]          # (N)orth or (S)outh
                rmc_longitude_dms = rmc[5]        # 01131.000 = 11 deg 31.000'
                rmc_longitude_ew = rmc[6]         # (E)east or (W)est
                rmc_speed_knots = rmc[7]          # 022.4 = 22.4 knots
                rmc_heading_deg_true = rmc[8]     # 084.4 = 84.4 degrees True
                rmc_date_ddmmyy = rmc[9]          # date 23rd of March 1994
                rmc_mag_var_mag = rmc[10]         # Magnetic Variation (magnitude)
                rmc_mag_var_dir = rmc[11]         # Magnetic Variation (direction)
                # Convert text values to workable numbers
                year = int(rmc_date_ddmmyy[4:6]) + 2000
                month = int(rmc_date_ddmmyy[2:4])
                day = int(rmc_date_ddmmyy[0:2])
                hour = int(rmc_fixtime_utc_hhmmss[0:2])
                minute = int(rmc_fixtime_utc_hhmmss[2:4])
                second = int(rmc_fixtime_utc_hhmmss[4:6])
                dt = datetime(year, month, day, hour, minute, second)
                loc.timestamp = int(time.mktime(dt.timetuple()))
                # Convert to decimal degrees latitude/longitude
                if rmc_longitude_dms != '' and rmc_longitude_dms != '':
                    loc.latitude = round(float(rmc_latitude_dms[0:2]) + float(rmc_latitude_dms[2:]) / 60.0, degrees_resolution)
                    if rmc_latitude_ns == 'S':
                        loc.latitude *= -1
                    loc.longitude = round(float(rmc_longitude_dms[0:3]) + float(rmc_longitude_dms[3:]) / 60.0, degrees_resolution)
                    if rmc_longitude_ew == 'W':
                        loc.longitude *= -1
                loc.speed = float(rmc_speed_knots) if rmc_speed_knots != '' else 0.0  # multiply by 1.852 for kph
                loc.heading = float(rmc_heading_deg_true) if rmc_heading_deg_true != '' else 0.0
                # Update human-readable attributes
                loc.time_readable = datetime.utcfromtimestamp(loc.timestamp).strftime('%Y-%m-%d %H:%M:%S')

            elif sentence_type == 'GSA':                    # GSA is used for DOP and active satellites
                gsa = nmea_data.split(',')                  # $GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39
                gsa_auto = gsa[1]                           # Auto selection of 2D or 3D fix (M = manual)
                gsa_fix_type = gsa[2]                       # 3D fix type
                gsa_fix_types = {
                    'none': 1,
                    '2D': 2,
                    '3D': 3
                }
                gsa_prns = []                               # PRNs of satellites used for fix (space for 12)
                for prn in range(1, 12):
                    gsa_prns.append(gsa[prn+2])             # offset of prns in the split array is [3]
                gsa_pdop = gsa[15]                          # Probability dilution of precision (DOP), above 20 is bad
                gsa_hdop = gsa[16]                          # Horizontal DOP
                gsa_vdop = gsa[17]                          # Vertical DOP
                # Use GSA for fix_type, PDOP, VDOP (HDOP comes from GGA)
                loc.fix_type = int(gsa_fix_type) if gsa_fix_type != '' else 0
                loc.pdop = max(float(gsa_pdop), 32.0) if gsa_pdop != '' else 32.0
                loc.vdop = max(float(gsa_vdop), 32.0) if gsa_vdop != '' else 32.0

            elif sentence_type == 'GSV':         # Satellites in View
                gsv = nmea_data.split(',')       # $GPGSV,2,1,08,01,40,083,46,02,17,308,41,12,07,344,39,14,22,228,45*75
                gsv_sentences = gsv[1]           # Number of sentences for full data
                gsv_sentence = gsv[2]            # Sentence number (up to 4 satellites per sentence)
                gsv_satellites = gsv[3]          # Number of satellites in view
                # following supports up to 4 satellites per sentence
                satellites_info = []
                if (len(gsv) - 4) % 4 > 0:
                    # TODO: warn/log this case of extra GSV data in sentence
                    pass
                num_satellites_in_sentence = int((len(gsv)-4)/4)
                for i in range(1, num_satellites_in_sentence+1):
                    prn = int(gsv[i*4]) if gsv[i*4] != '' else 0             # satellite PRN number
                    elevation = int(gsv[i*4+1]) if gsv[i*4+1] != '' else 0   # Elevation in degrees
                    azimuth = int(gsv[i*4+2]) if gsv[i*4+2] != '' else 0     # Azimuth in degrees
                    snr = int(gsv[i*4+3]) if gsv[i*4+3] != '' else 0         # Signal to Noise Ratio
                    satellites_info.append(Location.GnssSatelliteInfo(prn, elevation, azimuth, snr))
                loc.update_satellites_info(satellites_info)
                satellites = int(gsv_satellites) if gsv_satellites != '' else 0
                if loc.satellites < satellites:
                    loc.satellites = satellites
                else:
                    # TODO: log this case; should be limited to GPS simulation in Modem Simulator (3 satellites)
                    pass

            else:
                err_str += "{}{} NMEA sentence type not recognized".format(';' if err_str != '' else '', sentence[0:3])
        else:
            err_str = "{}Invalid NMEA checksum on {}".format(';' if err_str != '' else '', sentence[0:3])
    if loc.latitude == 90.0 and loc.longitude == 180.0:
        err_str += 'Unable to get valid location from NMEA'
    if err_str != '':
        raise Exception(err_str)
    return err_str == '', err_str
'''