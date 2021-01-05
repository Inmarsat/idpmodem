import unittest

from idpmodem import nmea


class NmeaTestCase(unittest.TestCase):
    def test_01_location(self):
        loc = nmea.Location()
        self.assertTrue(isinstance(loc, nmea.Location))
    
    def test_02_validate_nmea_checksum(self):
        good = '$GPRMC,184241.000,A,4517.1075,N,07550.9234,W,0.22,83.49,211120,,,A,V*34'
        bad = '$GPRMC,184241.000,A,4517.1075,N,07550.9234,W,0.22,83.49,211120,,,A,V*35'
        bool1, sen1 = nmea.validate_nmea_checksum(good)
        bool2, sen2 = nmea.validate_nmea_checksum(bad)
        del sen2   #: unused
        self.assertTrue(bool1 and sen1 == 'RMC,184241.000,A,4517.1075,N,07550.9234,W,0.22,83.49,211120,,,A,V')
        self.assertFalse(bool2)

    def test_03_get_location(self):
        nmea_data = [
            '$GPRMC,184849.000,A,4517.1094,N,07550.9143,W,0.20,0.00,211120,,,A,V*0E',
            '$GPGGA,184849.000,4517.1094,N,07550.9143,W,1,07,1.6,119.2,M,-34.3,M,,0000*67',
            '$GPGSA,A,3,32,10,31,21,23,01,20,,,,,,2.3,1.6,1.6,1*2D',
            '$GPGSV,2,1,08,01,20,310,22,10,54,161,34,20,19,154,32,21,24,288,38,0*69',
            '$GPGSV,2,2,08,23,24,149,29,25,39,120,23,31,39,223,43,32,75,332,34,0*68',
        ]
        expected = {
            'altitude': 119.2,
            'fix_type': 3,
            'hdop': 1.6,
            'heading': 0.0,
            'latitude': 45.285157,
            'longitude': -75.848572,
            'pdop': 2.3,
            'resolution': 6,
            'satellites': 8,
            'speed': 0.2,
            'time_iso': '2020-11-21T18:48:49Z',
            'timestamp': 1605984529,
            'vdop': 1.6,
            'satellites_info': [
                {'prn': 1, 'elevation': 20, 'azimuth': 310, 'snr': 22},
                {'prn': 10, 'elevation': 54, 'azimuth': 161, 'snr': 34},
                {'prn': 20, 'elevation': 19, 'azimuth': 154, 'snr': 32},
                {'prn': 21, 'elevation': 24, 'azimuth': 288, 'snr': 38},
                {'prn': 23, 'elevation': 24, 'azimuth': 149, 'snr': 29},
                {'prn': 25, 'elevation': 39, 'azimuth': 120, 'snr': 23},
                {'prn': 31, 'elevation': 39, 'azimuth': 223, 'snr': 43},
                {'prn': 32, 'elevation': 75, 'azimuth': 332, 'snr': 34},
            ]
        }
        loc = nmea.location_get(nmea_data)
        match = True
        for v in vars(loc):
            if isinstance(vars(loc)[v], list):
                for satinfo in vars(loc)[v]:
                    prn = satinfo.prn
                    for i in expected['satellites_info']:
                        if i['prn'] == prn:
                            satinfo_expected = i
                            break
                    for vv in vars(satinfo):
                        if vars(satinfo)[vv] != satinfo_expected[vv]:
                            match = False
                            break
                    if not match:
                        break
            elif vars(loc)[v] != expected[v]:
                match = False
                break
        self.assertTrue(isinstance(loc, nmea.Location) and match)


def suite():
    suite = unittest.TestSuite()
    available_tests = unittest.defaultTestLoader.getTestCaseNames(NmeaTestCase)
    tests = [
        # Add test cases above as strings or leave empty to test all cases
    ]
    if len(tests) > 0:
        for test in tests:
            for available_test in available_tests:
                if test in available_test:
                    suite.addTest(NmeaTestCase(available_test))
    else:
        for available_test in available_tests:
            suite.addTest(NmeaTestCase(available_test))
    return suite


if __name__ == '__main__':
    runner = unittest.TextTestRunner()
    runner.run(suite())
