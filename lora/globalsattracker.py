"""Object representation for GlobalSat LoRa Tracker"""


class Report(object):
    """Report sent by LoRa tracker"""

    ''' 
    Report Messages Format (network byte order aka Big Endian):
        Format_Type = 1 byte
        GPS_FixStatus_ReportType = 1 byte
        Battery_Capacity = 1 byte (percent)
        Latitude = 4 bytes (degrees * 1e6)
        Longitude = 4 bytes (degrees * 1e6)
    GPS_FixStatus_ReportType:
        GPS_FixStatus = bits[7..6]
        Report_Type = bits[5..0]
    '''

    format_types = {
        'V1': 0
    }

    gps_fix_statuses = {
       'None': 0,
       '2D': 1,
       '3D': 2
    }

    report_types = {
        'Periodic': 2,
        'Motion Static': 4,
        'Motion Soving': 5,
        'Motion Start': 6,
        'Motion Stop': 7,
        'Help': 14,
        'Low Battery': 15,
        'Power On Temperature': 17,
        'Power Off Battery': 19,
        'Power Off Temperature': 20,
        'Fall Advisory': 24,
        'Fpending': 27
    }

    def __init__(self):
        self.format_type = None
        self.gps_fix_status = self.gps_fix_statuses['None']
        self.report_type = self.report_types['Periodic']
        self.battery_level = 0
        self.latitude_de6 = 91 * 1e6
        self.longitude_de6 = 181 * 1e6
        self.payload = None

    def decode(self, payload):
        """Takes a payload received and populates the object
        :param:     payload ASCII-Hex format or bytearray
        :return:    err_code
                    err_str
        """
        if isintance(payload, bytearray):
            payload = ''.join(format(x, '02x') for x in payload)
        if isintance(payload, str):
            self.format_type = int(payload[0:2])
            if self.format_type in self.format_types:
                gfr = bin(int(payload[2:4])).replace('0b', '')
                self.gps_fix_status = int(gfr[0:2], 2)
                self.report_type = int(gfr[2:], 2)
                self.battery_level = int(payload[4:6])
                self.latitude_de6 = int(payload[6:10])
                self.longitude_de6 = int(payload[10:])
                err_code = 0
                err_str = "OK"
            else:
                err_code = 2
                err_str = "Undefined format type"
        else:
            err_code = 1
            err_str = "Invalid payload format, use ASCII-Hex or bytearray"
        return err_code, err_str


class LoraTracker(object):
    """LoRa Tracker"""

    # models dictionary of tuples (<name>, <gps>, <fall>)
    models = {
        ('LT100x', True, False),
        ('LT100xP', True, True),
        ('LT100xS', False, False)
    }

    '''
    Command Format:
        Header = 3 bytes (0x0C 08 00)
        Data_Length = 1 byte (length of command code plus CRLF)
        Command_CodeWord_Parameters (code word string 2 chars, parameters string in brackets with =)
        Trailer = 2 bytes (CRLF = 0x0D 0A)
    
    Example: Vibrate and beep device 5 seconds: N3(OD=5,OE=5) 
    -> <0C 08 00> <0F> <4E 33 28 4F 44 3D 35 2C 4F 45 3D 35 29> <0D 0A>
    '''

    # Command dictionary of tuples (<code>, <operation>)
    commands = {
        ('M7', 'Set Standby Mode'),
        ('M2', 'Set Periodic Mode'),
        ('M4', 'Set Motion Mode'),
        ('N1', 'Ping'),
        ('N3', 'Trigger Vibration Beep'),
        ('Na', 'Dismiss Help'),
        ('Nf', 'Dismiss Fall'),
        ('LA', 'Restore Defaults'),
        # Others?
    }

    # parameters of format (<command_code>, <parameter_code>, <name>, <data_type>)
    command_parameters = {
        ('M2', 'P0', 'Periodic Interval', 'u32'),  # >=10, 60=default (seconds)
        ('M4', 'R0', 'Motion Static Interval', 'u32'),  # >=10, default=3600(seconds)
        ('M4', 'R1', 'Motion Moving Interval', 'u32'),  # >= 10, default=30(seconds)
        ('M4', 'RH', 'Motion GPS Always On', 'bool'),  # default=1
        ('N3', 'OD', 'Beep Interval', 'u16'),    # 0~60000, 0=disable, 60000=continuous(default)
        ('N3', 'OE', 'Vibrate Interval', 'u16'),     # 0~60000, 0=disable, 60000=continuous(default)
        ('MainDevice', 'O0', 'Enable Power Key', 'bool'),  # default=1
        ('MainDevice', 'O4', 'Power On Mode', 'u8'),  # 2=Periodic(default), 4=Motion
        ('MainDevice', 'O7', 'Get FW Version', 'char28'),
        ('MainDevice', 'O8', 'Enable Battery Low LED', 'bool'),  # default=1
        ('MainDevice', 'O9', 'Enable GPS LoRa LED', 'bool'),  # default=1
        ('MainPower', 'J8', 'Enable Auto On Charged', 'bool'),  # default=1
        ('MainOther', 'Gt', 'G-sensor Sensitivity', 'u8'),  # 5=high, 10=med(default) 25=low
        ('MainOther', 'O1', 'Motion Sensor Interval', 'u16'),  # 1~100 seconds, default=5
        ('GPSGPS', 'C0', 'Enable GPS Always On', 'bool'),  # default=0, enabled if report interval(s) <30 seconds
        ('GPSGPS', 'C1', 'GPS Time to Cold Fix', 'u16'),  # 60~600, default=120
        ('GPSGPS', 'C2', 'GPS Time to Warm Fix', 'u16'),  # 10~120, default=30
        ('GPSGPS', 'C3', 'GPS Fix time to first report', 'u16'),  # 0~600, 0=disable first message, default=30
        ('GPSGPS', 'C8', 'GPS Max Off Time', 'u16'),  # 0~65535, 10800=default
        ('CommLoRa', 'D0', 'LoRaWAN Device Address', 'char8'),  # Read only uses last 8 digits of MAC as DevAddr
        ('CommLoRa', 'D5', 'Enable LoRaWAN ADR', 'bool'),  # Adaptive Data Rate, default=1
        ('CommLoRa', 'D8', 'LoRa FW Version', 'char20'),
        ('CommLoRa', 'D9', 'LoRaWAN DevEUI', 'char16'),
        ('CommLoRa', 'DC', 'LoRaWAN Class', 'u8'),  # 0=Class A, 2=Class C
        ('CommLoRa', 'DD', 'Enable Fpending', 'bool'),  # default=1, requests network to deliver pending messages
        ('CommAck', 'A1', 'Enable Ack', 'bool'),  # default=0
        ('CommAck', 'A6', 'Retransmits', 'u8'),  # 1~8, 2=default
        ('Help', 'G0', 'Help Interval', 'u16'),  # >=1 default=30(seconds)
        ('FallDown', 'JF', 'Local Fall Alarm Action', 'u8'),  # 0=off, 1=beep, 2=vibration, 3=beep+vibe(default)
        ('FallDown', 'JH', 'Enable Fall', 'bool'),  # 1=default
        ('FallDown', 'JD', 'Fall Impact', 'u8'),  # 16~128, 1G=16, 2G=32 ... 8G=128
        ('FallDown', 'JG', 'Movement Post Impact', 'u16'),  # 20ms increments, default=500(1 second)
        ('FallDown', 'JI', 'Static Post Impact', 'u16'),  # 20ms increments, default=250(0.5 seconds)
        ('FallDown', 'JK', 'Angle Static to Fall', 'u8'),  # 0~70 degrees, default=60
    }

    def __init__(self, model='LT100x', cmd_callback=None):
        """LoRa Tracker properties
        :param:     model name from a list of supported models
        :param:     cmd_callback used by command operations (e.g. LoRa downlink function)
        """
        self.lora_mac = ''
        if model in self.models:
            self.model = model
        else:
            self.model = 'LT100x'
        self.cmd_callback = cmd_callback
        self.power_on_mode = 2  # Periodic(default)
        self.reporting_mode = None
        self.periodic_interval = None
        self.motion_static_interval = None
        self.motion_moving_interval = None
        self.ack_enabled = False
        self.ack_retransmits = 2
        self.lora_config = {
            'ADR': 0,
            'JoinMode': 0
        }

    def set_power_on_mode(self, mode=2):
        """Sets the power-on mode
        :param:     mode 2=periodic, 4=motion
        :return:    err_code (0 = none)
                    err_str
        """
        if mode == 2 or mode == 4:
            err_code = 0
            err_str = "OK"
            self.power_on_mode = mode
            # TODO: send command to mote e.g. ??(O4=2)
        else:
            err_code = 1
            err_str = "Invalid mode defined"

        return err_code, err_str

    def set_periodic_interval(self, seconds=60):
        """Sets the Periodic Mode reporting interval
        :param:     seconds
        """
        if 10 <= seconds <= 86400:
            err_code = 0
            err_str = "OK"
            self.periodic_interval = seconds
            # TODO: send cmd_callback command M2(P0=<seconds>)
        else:
            err_code = 1
            err_str = "Invalid interval %d (range 10..86400)" % seconds
        return err_code, err_str

    def set_motion_moving_interval(self, seconds=30):
        """Sets the Motion Mode moving reporting interval
        :param:     seconds
        """
        if 10 <= seconds <= 86400:
            if seconds <= self.motion_static_interval:
                err_code = 0
                err_str = "OK"
                self.motion_moving_interval = seconds
                # TODO: send cmd_callback command M4(R1=<seconds>)
            else:
                err_code = 2
                err_str = "Attempted to set moving interval greater than static"
        else:
            err_code = 1
            err_str = "Invalid interval %d (range 10..86400)" % seconds
        return err_code, err_str

    def set_motion_static_interval(self, seconds=3600):
        """Sets the Motion Mode static reporting interval
        :param:     seconds
        """
        if 10 <= seconds <= 86400:
            err_code = 0
            err_str = "OK"
            self.motion_static_interval = seconds
            # TODO: send cmd_callback command M4(R0=<seconds>)
        else:
            err_code = 1
            err_str = "Invalid interval %d (range 10..86400)" % seconds
        return err_code, err_str

    def assert_beep_vibe(self, beep_seconds=0, vibe_seconds=0):
        """Buzzes and/or vibrates the unit
        :param:     buzz_seconds
        :param:     vibe_seconds
        """
        if 0 <= beep_seconds <= 60000 and 0 <= vibe_seconds <= 60000:
            err_code = 0
            err_str = "OK"
            # TODO: send cmd_callback command N3(OD=<beep_seconds>,OE=<vibe_seconds>)
        else:
            err_code = 1
            err_str = "Invalid duration %d/%d (range 0..60000)" % (beep_seconds, vibe_seconds)
        return err_code, err_str
