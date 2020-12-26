"""Utilities for validating and parsing NMEA data into a `Location` object.

"""

from calendar import timegm
from datetime import datetime
from functools import reduce
from operator import xor
from typing import Tuple


class NmeaException(Exception):
    """An exception triggered during NMEA sentence parsing."""
    pass


class NmeaInvalid(NmeaException):
    """Invalid NMEA sentence."""
    pass


class NmeaChecksumError(NmeaException):
    """NMEA sentence checksum error."""
    pass


class Location:
    """A set of location-based information for a given point in time.
    
    Uses 90.0/180.0 if latitude/longitude are unknown

    Attributes:
        latitude: decimal degrees
        longitude: decimal degrees
        altitude: in metres
        speed: in knots
        heading: in degrees
        timestamp: in seconds since 1970-01-01T00:00:00Z
        satellites: in view at time of fix
        fix_type: 1=None, 2=2D or 3=3D
        pdop: Probability Dilution of Precision
        hdop: Horizontal Dilution of Precision
        vdop: Vertical Dilution of Precision
        time_iso: ISO 8601 formatted timestamp
        satellites_info: `GnssSatelliteInfo`

    """

    def __init__(self,
                 latitude=90.0,
                 longitude=180.0,
                 altitude=0.0,
                 speed=0.0,
                 heading=0.0,
                 timestamp=0,
                 satellites=0,
                 fix_type=1):
        """Initializes a Location with default latitude/longitude 90/180

        Args:
            latitude: (float) in decimal degrees (6 places precision)
            longitude: (float) in decimal degrees (6 places precision)
            altitude: (float) in metres
            speed: (float) in knots
            heading: (float) in degrees relative to North
            timestamp: (int) in seconds since 1970-01-01T00:00:00Z
            satellites: (int) number of satellites in view at time of fix
            fix_type: (int) 1=None, 2=2D or 3=3D

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
        """Information specific to a GNSS satellite.
        
        Attributes:
            prn: The PRN code (Pseudo-Random Number sequence)
            elevation: The satellite elevation
            azimuth: The satellite azimuth
            snr: The satellite Signal-to-Noise Ratio
        """
        def __init__(self,
                     prn: int,
                     elevation: int,
                     azimuth: int,
                     snr: int):
            """Initializes GNSS satellite details."""
            self.prn = prn
            self.elevation = elevation
            self.azimuth = azimuth
            self.snr = snr

    def _update_satellites_info(self, satellites_info: list):
        """Populates satellite information based on NMEA GSV data."""
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
    
    def _isotime_set(self):
        """Sets the `time_iso` attribute from `timestamp`"""
        self.time_iso = (datetime.utcfromtimestamp(self.timestamp).isoformat() +
                         'Z')


def validate_nmea_checksum(sentence: str) -> Tuple[bool, str]:
    """Validates NMEA sentence using checksum according to the standard.

    Args:
        sentence: NMEA sentence including checksum
    
    Returns:
       A tuple with Boolean result (checksum correct) and
       raw NMEA data string, with prefix $Gx and checksum suffix removed

    """
    sentence = sentence.strip('\n').strip('\r')
    nmeadata, cksum = sentence.split('*', 1)
    nmeadata = nmeadata.replace('$', '')
    xcksum = str("%0.2x" % (reduce(xor, (ord(c) for c in nmeadata), 0))).upper()
    return (cksum == xcksum), nmeadata[2:]


def location_get(nmea_data_set: list, degrees_resolution: int = 6) -> Location:
    """Returns a Location object based on an NMEA sentences data set.
    
    Args:
        nmea_data_set: A list of valid NMEA sentences
        degrees_resolution: The desired precision of latitude/longitude
    
    Returns:
        A `Location` object

    """
    loc = Location()
    for sentence in nmea_data_set:
        if not sentence.startswith('$G'):
            raise NmeaInvalid('Invalid NMEA string {}'.format(sentence))
        valid, nmea_data = validate_nmea_checksum(sentence)
        if not valid:
            raise NmeaChecksumError('Invalid NMEA checksum for {}'.format(sentence))
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
            loc.hdop = min(float(gga_hdop), 32.0) if gga_hdop != '' else 32.0

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
            loc.timestamp = int(timegm(dt.timetuple()))
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
            loc._isotime_set()

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
            loc.pdop = min(float(gsa_pdop), 32.0) if gsa_pdop != '' else 32.0
            loc.vdop = min(float(gsa_vdop), 32.0) if gsa_vdop != '' else 32.0

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
            loc._update_satellites_info(satellites_info)
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
