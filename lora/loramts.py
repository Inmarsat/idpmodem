#!/usr/bin/env python

import paho.mqtt.client as mqtt
import json
import base64
import struct
import binascii
import time
import datetime


class LoraMClient(object):
    """A class to encapsulate MQTT operations between the broker (Conduit) and LoRa motes"""

    struct_byte_orders = {
        'native': '@',
        'little_endian': '<',
        'network': '!'
    }

    struct_formats = {
        'char': 'c',
        'int_8': 'b',       # signed char
        'uint_8': 'B',      # unsigned char
        'bool': '?',
        'int_16': 'h',      # short
        'uint_16': 'H',     # unsigned short
        'int_32': 'i',
        'uint_32': 'I',
        'int_64': 'q',
        'uint_64': 'Q',
        'float_32': 'f',
        'double_64': 'd',
        'string': 's'
    }

    def __init__(self, uplink_callback=None, logger=None, debug=False):
        """Initialize the network server instance
        :param: uplink_callback function that will be passed uplink messages
        :param: log (optional) passed in by the creator
        :param: debug (optional) flag to log debug trace
        """
        self.motes = []
        self.uplink_callback = uplink_callback
        self.lora_client = mqtt.Client()
        self.lora_client.on_connect = self._on_lora_connect
        self.lora_client.on_message = self._on_lora_uplink
        self.lora_client.on_log = self._on_log
        if logger is not None:
            self.log = logger
        else:
            import logging
            self.log = logging.getLogger("loranetworkserver")
            log_formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d,(%(threadName)-10s),'
                                                  '[%(levelname)s],%(funcName)s(%(lineno)d),%(message)s',
                                              datefmt='%Y-%m-%d %H:%M:%S')
            log_formatter.converter = time.gmtime
            console = logging.StreamHandler()
            console.setFormatter(log_formatter)
            if debug:
                console.setLevel(logging.DEBUG)
            else:
                console.setLevel(logging.INFO)
            self.log.setLevel(console.level)
            self.log.addHandler(console)

    def connect(self, host="127.0.0.1"):
        """Connects to the local LoRa network server in the MTS Conduit"""
        self.lora_client.connect(host=host, port=1883, keepalive=60)
        self.lora_client.loop_start()

    def disconnect(self):
        """Disconnects"""
        self.lora_client.loop_stop()

    def _on_log(self, client, userdata, level, buf):
        """A callback that puts MQTT (Paho) logs into the logger. Maps to paho.mqtt on_log caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation
        :param: level of log event
        :param: buf string buffer message
        """
        self.log.log(level, buf)

    def _on_lora_connect(self, client, userdata, flags, rc):
        """Callback subscribes to LoRa uplink MQTT messages from broker (Conduit).
        Maps to paho.mqtt on_connect caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation (unused)
        :param: flags dict containing response flags from the broker
        :param: rc result code (enum) returned by connection attempt
        """
        self.log.info("Connected with result code %s" % rc)
        client.subscribe("lora/+/up")

    def _on_lora_uplink(self, client, userdata, msg):
        """Callback parses a LoRa uplink MQTT message into a base64 envelope with the
        MAC, UTC timestamp and LoRa payload. Maps to paho.mqtt on_message caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation (unused)
        :param: msg is the MQTT message passed by the broker
        """
        # Extract the mac address from the topic
        mac_str = msg.topic.split('/')[1]
        lora_mac = mac_str.replace('-', '')
        if lora_mac not in self.motes:
            self.log.info("New LoRa mote registered: %s" % mac_str)
            self.motes.append(lora_mac)
        self.log.debug("MQTT publish from: %s | Message: %s" % (mac_str, msg.payload))
        json_data = json.loads(msg.payload.decode("utf-8"))
        self.log.debug("LoRa payload (base64): " + str(json_data['data']))
        if str(json_data['data']) != "":
            lora_mac_bytes = [ord(c) for c in binascii.unhexlify(lora_mac)]
            # self.log.debug("LoRa MAC bytes: %s" % binascii.hexlify(bytearray(lora_mac_bytes)))
            timestamp = int(time.mktime(datetime.datetime.utcnow().timetuple()))
            timestamp_bytes = list(struct.unpack('!4B', struct.pack('!I', timestamp)))
            # self.log.debug("Timestamp bytes: %s" % binascii.hexlify(bytearray(timestamp_bytes)))
            lora_payload_b64 = base64.b64decode(json_data['data'])
            b64_fmt = '!' + str(len(lora_payload_b64)) + 'B'
            lora_payload_bytes = list(struct.unpack(b64_fmt, lora_payload_b64))
            # self.log.debug("LoRa payload bytes: %s" % binascii.hexlify(bytearray(lora_payload_bytes)))
            envelope_bytes = lora_mac_bytes + timestamp_bytes + lora_payload_bytes
            b64_payload = base64.b64encode(bytearray(envelope_bytes))
            # self.log.debug("Hex payload: %s" % binascii.hexlify(bytearray(envelope_bytes)))
            self.uplink_callback(b64_payload)
        else:
            self.log.warning("Empty LoRa payload received from %s" % mac_str)

    def send_lora_downlink(self, mac_node, lora_payload, data_type=None):
        """Publishes a LoRa downlink MQTT message via the Conduit broker, converting data to base64.
        :param: mac_node (string) the destination LoRa MAC address
        :param: lora_payload the payload to be sent
        :param: data_type of lora_payload to be used for encoding to base64
        """
        if data_type == 'uint16' and isinstance(lora_payload, int):
            # TODO: add scalability beyond short int - test GlobalSat reconfig
            type_uint16_bigend = '!H'   # from struct definition
            b64_str = base64.b64encode(bytearray([ord(c) for c in struct.pack(type_uint16_bigend, lora_payload)]))
        elif isinstance(lora_payload, list):    # assume list of bytes
            b64_str = base64.b64encode(bytearray(lora_payload))
        else:   # isinstance(lora_payload, str)
            b64_str = base64.b64encode(str(lora_payload))
        topic_down = "lora/" + mac_node + "/down"
        data_to_mote = "{ \"data\": \"" + b64_str + "\" }"
        self.log.debug("Sending MQTT Topic: %s Data: %s" % (topic_down, data_to_mote))
        self.lora_client.publish(topic_down, data_to_mote)
