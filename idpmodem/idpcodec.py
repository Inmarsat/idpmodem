"""
Encode and Decode functions useful for IDP messages
"""
import struct


class CommonMessageFormat(object):

    data_types = (
        'bool',
        'int_8',
        'uint_8',
        'int_16',
        'uint_16',
        'int_32',
        'uint_31',   # unique to SkyWave IDP-series Lua 5.3
        'uint_32',   # not supported by all ORBCOMM/SkyWave terminals
        'int_64',    # not supported by all ORBCOMM/SkyWave terminals
        'uint_64',   # not supported by all ORBCOMM/SkyWave terminals
        'float',     # not supported by ORBCOMM/SkyWave terminals
        'double',    # not supported by ORBCOMM/SkyWave terminals
        'string',
        'data',
        'array',
    )

    @staticmethod
    def validate_type(value, data_type):
        if data_type == 'bool':
            return isinstance(value, bool)
        # TODO: more validation

    def __init__(self, msg_sin, msg_min, name=None, description=None):
        self.name = name
        self.description = description
        if isinstance(msg_sin, int) and msg_sin in range(16, 256):
            self.sin = msg_sin
        else:
            raise ValueError("Invalid SIN ({}) must be in range 16..255".format(msg_sin))
        if isinstance(msg_min, int) and msg_min in range (0, 256):
            self.min = msg_min
        else:
            raise ValueError("Invalid MIN ({}) must be integer type in range 0..255".format(msg_min))
        self.fields = []
        self.size = None

    class Field(object):
        def __init__(self, name, data_type, value, bit_size):
            self.name = name
            if data_type in CommonMessageFormat.data_types:
                self.data_type = data_type
            else:
                raise ValueError("Invalid data type, must be in: ({})".format(CommonMessageFormat.data_types))
            self.value = value
            self.bit_size = bit_size

    def add_field(self, name, data_type, value, value_range=None, bit_size=None, description=None, index=None):
        """
        Add a field to the message.

        :param name: (string)
        :param data_type: (string) from supported types
        :param value: the value (compliant with data_type)
        :param bit_size: string formatter '0nb' where n is number of bits
        :return:

           * error code
           * error string

        """
        # TODO: make it so fields cannot be added/deleted/modified without explicit class methods
        field = {}
        if isinstance(name, str):
            field['name'] = name
            if data_type in self.data_types:
                field['data_type'] = data_type
                if data_type == 'bool' and isinstance(value, bool) \
                        or 'int' in data_type and isinstance(value, int) \
                        or data_type == 'string' and isinstance(value, str) \
                        or (data_type == 'float' or data_type == 'double') and isinstance(value, float):

                    field['value'] = value
                    if bit_size[0] == '0' and bit_size[len(bit_size) - 1] == 'b':
                        # TODO: some risk that value range may not fit in bit_size
                        if bit_size[1:len(bit_size) - 1] > 0:
                            err_code = 0
                            err_str = 'OK'
                            field['bit_size'] = bit_size
                            self.fields.append(field)
                        else:
                            err_code = 5
                            err_str = "Value exceeds specified number of bits"
                    else:
                        err_code = 4
                        err_str = "Invalid bit_size definition"
                else:
                    err_code = 3
                    err_str = "Value type does not match data type"
            else:
                err_code = 2
                err_str = "Invalid data type"
        else:
            err_code = 1
            err_str = "Invalid name of field (not string)"
        return err_code, err_str

    def delete_field(self, name):
        """
        Remove a field from the message.

        :param name: of field (string)
        :returns:
           - error code (0 = no error)
           - error string description (0 = "OK")

        """
        err_code = 1
        err_str = "Field not found in message"
        for i, field in enumerate(self.fields):
            if field['name'] == name:
                err_code = 0
                err_str = "OK"
                del self.fields[i]
        return err_code, err_str

    def encode_idp(self, data_format=2):
        """
        Encodes the message using the specified data format (Text, Hex, base64).

        :param data_format: 1=Text, 2=ASCII-Hex, 3=base64
        :returns: encoded_payload (string) to pass into AT%MGRT

        """
        encoded_payload = ''
        bin_str = ''
        for field in self.fields:
            name = field['name']
            data_type = field['data_type']
            value = field['value']
            bit_size = field['bit_size']
            bin_field = ''
            if 'int' in data_type and isinstance(value, int):
                if value < 0:
                    inv_bin_field = format(-value, bit_size)
                    comp_bin_field = ''
                    i = 0
                    while len(comp_bin_field) < len(inv_bin_field):
                        comp_bin_field += '1' if inv_bin_field[i] == '0' else '0'
                        i += 1
                    bin_field = format(int(comp_bin_field, 2) + 1, bit_size)
                else:
                    bin_field = format(value, bit_size)
            elif data_type == 'bool' and isinstance(value, bool):
                bin_field = '1' if value else '0'
            elif data_type == 'float' and isinstance(value, float):
                f = '{0:0%db}' % bit_size
                bin_field = f.format(int(hex(struct.unpack('!I', struct.pack('!f', value))[0]), 16))
            elif data_type == 'double' and isinstance(value, float):
                f = '{0:0%db}' % bit_size
                bin_field = f.format(int(hex(struct.unpack('!Q', struct.pack('!d', value))[0]), 16))
            elif data_type == 'string' and isinstance(value, str):
                bin_field = bin(int(''.join(format(ord(c), '02x') for c in value), 16))[2:]
                if len(bin_field) < bit_size:
                    # TODO: be careful on padding strings...this should pad with NULL
                    bin_field += ''.join('0' for pad in range(len(bin_field), bit_size))
            else:
                pass
                # TODO: handle other cases
                # raise
            bin_str += bin_field
        payload_pad_bits = len(bin_str) % 8
        while payload_pad_bits > 0:
            bin_str += '0'
            payload_pad_bits -= 1
        hex_str = ''
        index_byte = 0
        while len(hex_str) / 2 < len(bin_str) / 8:
            hex_str += format(int(bin_str[index_byte:index_byte + 8], 2), '02X').upper()
            index_byte += 8
        self.size = len(hex_str) / 2 + 2
        self.payload_b64 = hex_str.decode('hex').encode('base64').strip()
        if data_format == 2:
            encoded_payload = hex_str
        elif data_format == 3:
            encoded_payload = self.payload_b64
        return encoded_payload

    '''
    # TODO: this is not a modem function, belongs with external controller / edge compute
    def decode_idp_json(self):
        """
        Decodes the message received to JSON from the modem based on data format retrieved from IDP modem.
        For future use with Message Definition Files

        :return: JSON-formatted string

        """
        if self.size > 0:
            json_str = '{"name":%s,"SIN":%d,"MIN":%d,"size":%d,"Fields":[' \
                       % (str(self.name), self.sin, self.min, self.size)
            for i, field in enumerate(self.fields):
                json_str += '{"name":"%s","data_type":"%s","value":' \
                            % (field['name'], field['data_type'])
                if isinstance(field['value'], int):
                    json_str += '%d}' % field['value']
                elif isinstance(field['value'], float):
                    json_str += '%f}' % field['value']
                elif isinstance(field['value'], bool):
                    json_str += '%s}' % str(field['value']).lower()
                elif isinstance(field['value'], str):
                    json_str += '"%s"}' % field['value']
                json_str += ',' if i < len(self.fields) else ']'
            json_str += '}'
        else:
            json_str = ''
        return json_str
    '''


class MqttSn(object):
    def __init__(self):
        pass
