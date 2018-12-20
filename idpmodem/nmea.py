import operator
import time
import datetime


class Location(object):
    """
    A class containing a specific set of location-based information for a given point in time.
    Uses 91/181 if lat/lon are unknown

    :param latitude: in 1/1000th minutes (approximately 1 m resolution)
    :param longitude: in 1/1000th minutes (approximately 1 m resolution)
    :param altitude: in metres
    :param speed: in knots
    :param heading: in degrees
    :param timestamp: in seconds since 1970-01-01T00:00:00Z
    :param satellites: in view at time of fix
    :param fixtype: None, 2D or 3D
    :param PDOP: Probability Dilution of Precision
    :param HDOP: Horizontal DOP
    :param VDOP: Vertical DOP

    """

    def __init__(self, latitude=91 * 60 * 1000, longitude=181 * 60 * 1000, altitude=0,
                 speed=0, heading=0, timestamp=0, satellites=0, fixtype=1,
                 PDOP=0, HDOP=0, VDOP=0):
        """
        Creates a Location instance with default lat/lng 91/181 *unknown*

        :param latitude: in 1/1000th minutes (approximately 1 m resolution)
        :param longitude: in 1/1000th minutes (approximately 1 m resolution)
        :param altitude: in metres
        :param speed: in knots
        :param heading: in degrees
        :param timestamp: in seconds since 1970-01-01T00:00:00Z
        :param satellites: in view at time of fix
        :param fixtype: None, 2D or 3D
        :param PDOP: Probability Dilution of Precision
        :param HDOP: Horizontal DOP
        :param VDOP: Vertical DOP

        """
        self.latitude = latitude  # 1/1000th minutes
        self.longitude = longitude  # 1/1000th minutes
        self.altitude = altitude  # metres
        self.speed = speed  # knots
        self.heading = heading  # degrees
        self.timestamp = timestamp  # seconds since 1/1/1970 unix epoch
        self.satellites = satellites
        self.fixtype = fixtype
        self.pdop = PDOP
        self.hdop = HDOP
        self.vdop = VDOP
        self.lat = latitude / 60000
        self.lng = longitude / 60000
        self.time_readable = datetime.datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


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


def parse_nmea_to_location(sentence, loc):
    """
    Parses a NMEA string to partially populate a ``Location`` object.
    Several sentence parameters are unused but remain as placeholders for completeness/future use.

    :param sentence: NMEA sentence (including prefix and suffix)
    :param loc: the Location object to be populated
    :returns:
       - Boolean success of operation
       - error string if not successful

    """
    err_str = ''
    res, NMEA_data = validate_nmea_checksum(sentence)
    if res:
        sentence_type = NMEA_data[0:3]
        if sentence_type == 'GGA':
            GGA = NMEA_data.split(',')
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
            loc.satellites = int(GGAsatellites)
            if loc.satellites > 3:
                loc.fixtype = 3
            elif int(GGAqual) > 0:
                loc.fixtype = 2
            loc.altitude = int(float(GGAaltitude))
            loc.HDOP = max(int(float(GGAhdop)), 32)

        elif sentence_type == 'RMC':
            RMC = NMEA_data.split(',')
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
            # Update human-readable attributes
            loc.lat = round(float(loc.latitude) / 60000.0, 6)
            loc.lng = round(float(loc.longitude) / 60000.0, 6)
            loc.time_readable = datetime.datetime.utcfromtimestamp(loc.timestamp).strftime('%Y-%m-%d %H:%M:%S')

        elif sentence_type == 'GSA':
            GSA = NMEA_data.split(',')
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
            # loc.HDOP = max(int(float(GSAhdop)), 32)
            loc.VDOP = max(int(float(GSAvdop)), 32)

        elif sentence_type == 'GSV':
            GSV = sentence.split(',')
            # GSVsentences = GSV[1]
            # GSVsentence = GSV[2]
            GSVsatellites = GSV[3]
            # GSVprn1 = GSV[4]
            # GSVel1 = GSV[5]
            # GSVaz1 = GSV[6]
            # GSVsnr1 = GSV[7]
            # up to 4 satellites total per sentence, each as above in successive indices
            # loc.satellites = int(GSVsatellites)

        else:
            err_str = "NMEA sentence type not recognized"
    else:
        err_str = "Invalid NMEA checksum"

    return err_str == '', err_str
