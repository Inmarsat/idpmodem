
class Message(object):
    """
    Class intended for abstracting message attributes.

    :param payload: one of the following:

       * (bytearray) including SIN and MIN bytes as first 2 in the array if not explicitly set in the call
       * (list) of integer bytes (0..255) including SIN and MIN if not specified explicitly in the call
       * (string) ASCII-HEX which includes SIN and MIN if not specified explicitly in the call
       * (string) Text which requires both SIN and MIN explictly specified in the call

    :param name: (string) optional up to 8 characters. A message name will be generated if not supplied
    :param msg_sin: integer (0..255)
    :param msg_min: integer (0..255)
    :param priority: (1=high, 4=low, 0=mobile-terminated)
    :param data_format: (optional) 1=FORMAT_TEXT, 2=FORMAT_HEX, 3=FORMAT_B64
    :param log: (optional) logger object
    :param debug: (optional) sets logging level to DEBUG

    """

    MAX_HEX_SIZE = 100

    def __init__(self, name, payload, msg_sin=None, msg_min=None, priority=PRIORITY_LOW,
                 data_format=FORMAT_HEX, size=None, log=None, debug=False):
        if is_logger(log):
            self.log = log
        else:
            self.log = get_wrapping_logger(debug=debug)
        if name is not None:
            self.name = name
        else:
            # TODO: create a name
            raise ValueError("Message must have a valid name (not None)")
        if msg_min is not None:
            if msg_sin is None:
                raise ValueError("SIN must be specified if MIN is specified")
            elif isinstance(msg_min, int) and msg_min in range(0, 255+1):
                self.min = msg_min
                # assume that payload does not also include MIN
            else:
                self.log.warning(
                    "Invalid MIN value {} must be integer in range 0..255".format(msg_min))
        elif payload is not None:
            if isinstance(payload, bytearray):
                self.min = None
            else:
                raise ValueError(
                    "Payload must be bytearray type if MIN is not specified")
        else:
            raise ValueError("Payload cannot be None if MIN is not specified")
        if msg_sin is not None:
            if isinstance(msg_sin, int) and msg_sin in range(16, 256):
                self.sin = msg_sin
            else:
                raise ValueError(
                    "Invalid SIN value {}, must be integer in range 16..255".format(msg_sin))
        elif payload is not None:
            if isinstance(payload, bytearray):
                if payload[0] > 15:
                    self.sin = payload[0]
                    self.log.debug(
                        "Received bytearray with implied SIN={}".format(self.sin))
                    payload = payload[1:] if msg_min is None else payload[2:]
                else:
                    raise ValueError(
                        "Invalid payload, first byte (SIN) must be integer in range 16..255")
            else:
                raise ValueError(
                    "Payload must be bytearray type if SIN is not specified")
        else:
            raise ValueError("Payload cannot be None if SIN is not specified")
        self.raw_payload = bytearray(0)
        if self.sin is not None and payload is not None:
            if isinstance(payload, str):
                if data_format == FORMAT_TEXT:
                    if msg_sin is not None and msg_min is not None:
                        payload = bytearray(payload)
                    else:
                        raise ValueError(
                            "Function call with text string payload must include SIN and MIN")
                elif data_format == FORMAT_HEX:
                    if _is_hex_string(payload):
                        payload = _hex_to_bytearray(payload)
                    else:
                        raise ValueError(
                            "Hex format received with invalid characters")
                elif data_format == FORMAT_B64:
                    if msg_sin is not None and msg_min is not None:
                        payload = _b64_to_bytearray(payload)
                    else:
                        raise ValueError(
                            "Function call with base64 string payload must include SIN and MIN")
                else:
                    raise ValueError(
                        "Unrecognized data_format: {}".format(data_format))
            elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255+1)) for i in payload):
                payload = bytearray(payload)
            elif not isinstance(payload, bytearray):
                raise ValueError(
                    "Invalid payload type, must be text or hex string, integer list or bytearray")
            self.raw_payload = bytearray(payload)
            if msg_min is not None:
                self.raw_payload = bytearray([self.min]) + self.raw_payload
            if self.sin is not None:
                self.raw_payload = bytearray([self.sin]) + self.raw_payload
        self.size = len(self.raw_payload)
        if size is not None and size != self.size:
            self.log.warning(
                "Size {} passed during init does not match derived size {}".format(size, self.size))
        self.priority = priority
        self.data_format = data_format
        # self.log.debug("New message created: {}".format(vars(self)))

    def data(self, data_format=FORMAT_HEX, include_min=True, include_sin=False):
        """
        Returns the data of the message, typically for use when sending MO messages

        :param data_format: (int) 1=FORMAT_TEXT, 2=FORMAT_HEX (default), 3=FORMAT_B64
        :param include_min: (boolean) whether to include MIN byte in the data (used when not specifying MIN explicitly)
        :param include_sin: (boolean) whether to include SIN byte (not part of data for MO messages)
        :return:
        """
        if len(self.raw_payload) > 0:
            if include_sin:
                if not include_min:
                    raise ValueError("Must include MIN when including SIN")
                else:
                    payload = self.raw_payload
            else:
                payload = self.raw_payload[1:] if include_min else self.raw_payload[2:]
            if data_format == FORMAT_TEXT:
                self.log.warning(
                    "This is broken when using to submit MO messages")
                data = '\"{}\"'.format(_bytearray_to_str(payload))
            elif data_format == FORMAT_HEX:
                data = _bytearray_to_hex(payload)
            else:
                data = _bytearray_to_b64(payload)
            return data
        else:
            raise ValueError("No data to return")


class MobileOriginatedMessage(Message):
    """
    Subclass of Message containing Mobile Originated (aka Return) message properties.
    Mobile-Originated state (starting=None) is represented as an attribute:

       - ``UNAVAILABLE``: 0
       - ``TX_READY``: 4
       - ``TX_SENDING``: 5
       - ``TX_COMPLETE``: 6
       - ``TX_FAILED``: 7

    :param name: (string) user identifier for the message
    :param payload: follows the structure of the Message superclass
    :param data_format: follows the structure of the Message superclass
    :param msg_sin: Service Identification Number (1st byte of payload)
    :param msg_min: Message Identification Number (2nd byte of payload)
    :param kwargs: follows the structure of the Message superclass

    """

    def __init__(self, name, payload, data_format=FORMAT_HEX, msg_sin=None, msg_min=None, **kwargs):
        """

        :param name: (string) user identifier for the message
        :param payload: follows the structure of the Message superclass
        :param data_format: follows the structure of the Message superclass
        :param msg_sin: Service Identification Number (1st byte of payload)
        :param msg_min: Message Identification Number (2nd byte of payload)
        :param **kwargs: follows the structure of the Message superclass

        """
        if isinstance(payload, str):
            if _is_hex_string(payload):
                payload = bytearray.fromhex(payload)
            else:
                if msg_sin is not None and msg_min is not None:
                    if data_format is None or data_format != FORMAT_TEXT:
                        payload = bytearray(payload)
                else:
                    raise ValueError(
                        "Function call with text string payload must include SIN and MIN")
        elif isinstance(payload, list) and all((isinstance(i, int) and i in range(0, 255+1)) for i in payload):
            payload = bytearray(payload)
        elif not isinstance(payload, bytearray):
            raise ValueError(
                "Invalid payload type, must be text or hex string, integer list or bytearray")
        super(MobileOriginatedMessage, self).__init__(name=name, payload=payload, msg_sin=msg_sin, msg_min=msg_min,
                                                      data_format=data_format, **kwargs)
        self.state = None


class MobileTerminatedMessage(Message):
    """
    Subclass of Message containing Mobile-Terminated (MT aka Forward) message properties.
    Initializes MT message with state = ``RX_RETRIEVED``
    MT message state represented as an attribute:

       - ``UNAVAILABLE``: 0
       - ``COMPLETE``: 2
       -  ``RETRIEVED``: 3

    :param name: (string) name assigned by the modem
    :param payload: follows the structure of the Message superclass
    :param data_format: follows the structure of the Message superclass
    :param msg_num: (string) message number assigned by the modem (unused)
    :param priority: (int) always 0 for MT messages
    :param kwargs: follows the structure of the Message superclass

    """

    def __init__(self, name, payload, data_format, msg_num=None, priority=PRIORITY_MT, **kwargs):
        """

        :param name: (string) name assigned by the modem
        :param payload: follows the structure of the Message superclass
        :param data_format: follows the structure of the Message superclass
        :param msg_num: (string) message number assigned by the modem (unused)
        :param priority: (int) always 0 for MT messages
        :param kwargs: follows the structure of the Message superclass

        """
        if data_format not in (FORMAT_TEXT, FORMAT_HEX, FORMAT_B64):
            raise ValueError(
                "Unrecognized data format: {}".format(data_format))
        super(MobileTerminatedMessage, self).__init__(name=name, payload=payload, data_format=data_format,
                                                      priority=priority, **kwargs)
        self.state = RX_RETRIEVED
        self.number = msg_num
