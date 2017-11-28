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

    def __init__(self, uplink_callback=None, log=None, debug=False):
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
        if log is not None:
            self.log = log
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

    def connect(self):
        """Connects to the local LoRa network server in the MTS Conduit"""
        self.lora_client.connect(host="127.0.0.1", port=1883, keepalive=60)

    def _on_log(self, client, userdata, level, buf):
        """A callback that puts MQTT (Paho) logs into the logger. Maps to paho.mqtt on_log caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation
        :param: level of log event
        :param: buf string buffer message
        """
        self.log.level(buf)

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
        if lora_mac not in motes:
            self.log.info("New LoRa mote found: %s" % mac_str)
            self.motes.append(lora_mac)
            # TODO: optimize using a byte array instead of string for MAC?
        self.log.debug("MQTT publish from: %s | Message: %s" % (mac_str, msg.payload))
        json_data = json.loads(msg.payload.decode("utf-8"))
        self.log.debug("LoRa payload (base64): " + str(json_data['data']))
        if str(json_data['data']) != "":
            lora_mac_bytes = [ord(c) for c in binascii.unhexlify(lora_mac)]
            lora_payload_b64 = base64.b64decode(json_data['data'])
            b64_fmt = '!' + str(len(lora_payload_b64)) + 'B'
            lora_payload_bytes = list(struct.unpack(b64_fmt, lora_payload_b64))
            dt = datetime.datetime.utcnow()
            timestamp = int(time.mktime(dt.timetuple()))
            timestamp_bytes = list(struct.pack('!I', timestamp))
            envelope_bytes = lora_mac_bytes + timestamp_bytes + lora_payload_bytes
            b64_payload = base64.b64encode(bytearray(envelope_bytes))
            self.uplink_callback(b64_payload)
        else:
            self.log.warning("Empty LoRa payload received from %s" % mac_str)

    def send_lora_downlink(self, mac_node, lora_payload, data_type='uint16'):
        """Publishes a LoRa downlink MQTT message via the Conduit broker, converting data to base64.
        :param: mac_node (string) the destination LoRa MAC address
        :param: lora_payload the payload to be sent
        :param: data_type of lora_payload to be used for encoding to base64
        """
        if data_type == 'uint16':
            # TODO: add scalability beyond short int - test GlobalSat reconfig
            type_uint16_bigend = '!H'   # from struct definition
            b64_payload = base64.b64encode(bytearray([ord(c) for c in struct.pack(type_uint16_bigend, lora_payload)]))
        else:
            b64_payload = base64.b64encode(str(lora_payload))
        topic_down = "lora/" + mac_node + "/down"
        data_to_mote = "{ \"data\": \"" + b64_payload + "\" }"
        self.log.debug("Sending MQTT Topic: %s Data: %s" % (topic_down, data_to_mote))
        self.lora_client.publish(topic_down, data_to_mote)
