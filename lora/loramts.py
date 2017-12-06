#!/usr/bin/env python

import paho.mqtt.client as mqtt
import json
import base64
import struct
import binascii
import time
import datetime
import subprocess


class LoraMClient(object):
    """A class to encapsulate MQTT operations between the broker (Conduit) and LoRa motes"""

    _struct_byte_orders = {
        'native': '@',
        'little_endian': '<',
        'network': '!'
    }

    _struct_formats = {
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

    def __init__(self, uplink_callback=None, downlink_callback=None, joinfail_callback=None, logger=None, debug=False):
        """Initialize the network server instance
        :param: uplink_callback function that will be passed uplink messages
        :param: downlink_callback function that will handle ack/retransmission
        :param: joinfail_callback function that will notify back office of failed join attempts
        :param: log (optional) passed in by the creator
        :param: debug (optional) flag to log debug trace
        """
        self.product_id = subprocess.check_output('mts-io-sysfs show product-id', shell=True).strip()
        self.device_id = subprocess.check_output('mts-io-sysfs show device-id', shell=True).strip()
        self.hardware_version = subprocess.check_output('mts-io-sysfs show hw-version', shell=True).strip()
        self.software_version = 'unknown'
        ap1 = subprocess.check_output('mts-io-sysfs show ap1/product-id', shell=True).strip()
        ap2 = subprocess.check_output('mts-io-sysfs show ap2/product-id', shell=True).strip()
        if 'MTAC-LORA' in ap1:
            m_card = "1"
        elif 'MTAC-LORA' in ap2:
            m_card = "2"
        else:
            raise ValueError("No LoRa mCard detected in AP1 or AP2")
        self.lora_mcard = {
            'port': 'AP%s' % m_card,
            'product_id': ap1 if 'MTAC-LORA' in ap1 else ap2,
            'device_id': subprocess.check_output('mts-io-sysfs show ap%s/device-id' % m_card, shell=True).strip(),
            'hw_version': subprocess.check_output('mts-io-sysfs show ap%s/hw-version' % m_card, shell=True).strip()
        }
        self.lns_version = 'unknown'
        self.motes = []
        self.uplink_callback = uplink_callback
        self.downlink_callback = downlink_callback
        self.joinfail_callback = joinfail_callback
        self.lora_client = mqtt.Client()
        self.lora_client.on_connect = self._on_mqtt_connect
        self.lora_client.on_message = self._on_mqtt_message
        self.lora_client.on_log = self._on_log
        # http://www.multitech.net/developer/software/lora/lora-network-server/mqtt-messages/
        self.mqtt_event_subscriptions = [
            'up',
            'down',
            'joined',
            'join_rejected',
            'down_queued',
            'down_dropped',
            'cleared',
            'packet_sent',
            'packet_ack',
            'class',
            'packet_recv',
            'mac_sent',
            'mac_recv'
        ]
        self.stats = {
            'joined': 0,
            'join_rejected': 0,
            'up': 0,
            'up_ack_request': 0,
            'down_publish': 0,
            'down': 0,
            'down_ack_received': 0,
            'class': 0,
        }
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
        self.log.info("MultiTech Conduit Model:%s SN:%s" % (self.product_id, self.device_id))

    def connect(self, host="127.0.0.1", port=1883, keepalive=60):
        """Connects to the local LoRa network server in the MTS Conduit
        :param:     host default localhost
        :param:     port default 1883
        :param:     keepalive default 60 seconds
        """
        self.lora_client.connect(host=host, port=port, keepalive=keepalive)
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

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """Callback subscribes to LoRa uplink MQTT messages from broker (Conduit).
        Maps to paho.mqtt on_connect caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation (unused)
        :param: flags dict containing response flags from the broker
        :param: rc result code (enum) returned by connection attempt
        """
        self.log.info("Connected LoRa Network Server with result code %s" % rc)
        for event in self.mqtt_event_subscriptions:
            client.subscribe("lora/+/%s" % event)

    def _on_mqtt_message(self, client, userdata, msg):
        """Callback parses a LoRa uplink MQTT message into a base64 envelope with the
        MAC, UTC timestamp and LoRa payload. Maps to paho.mqtt on_message caller.
        :param: client that called this callback
        :param: userdata optionally populated in the client on creation (unused)
        :param: msg is the MQTT message passed by the broker with structure:
                    topic
                    qos
                    payload
                    retain
        """
        # Extract the mac address from the topic
        mac_str, event_type = msg.topic.split('/')[1], msg.topic.split('/')[2]
        dev_eui = mac_str.replace('-', '')
        if dev_eui not in self.motes:
            self.log.info("New LoRa mote registered: %s" % mac_str)
            self.motes.append(dev_eui)
        if event_type == 'joined':
            self.stats['joined'] += 1
            self.log.debug("Mote %s joined" % dev_eui)
        elif event_type == 'up':
            self.stats['up'] += 1
            self.log.debug("MQTT publish from: %s | Message: %s" % (mac_str, msg.payload))
            # TODO: check if uplink ACK requested by mote, count
            json_data = json.loads(msg.payload.decode('utf-8'))
            if json_data['ack']:
                self.stats['down_ack_received'] += 1
            # self.log.debug("LoRa payload (base64): " + str(json_data['data']))
            if str(json_data['data']) != "":
                lora_mac_bytes = [ord(c) for c in binascii.unhexlify(dev_eui)]
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
        elif event_type == 'join_rejected':
            # TODO: callback notify back-office application of failed join attempt and MAC, possible provisioning issue
            self.stats['join_rejected'] += 1
            self.log.debug("Join rejected from: %s - %s" % (dev_eui, msg.payload.decode('utf-8')))
        elif event_type == 'down':
            self.stats['down_publish'] += 1
            self.log.debug("Downlink published to %s: %s" % (dev_eui, msg.payload.decode('utf-8')))
        elif event_type == 'packet_sent':
            self.stats['down'] += 1
            self.log.debug("Downlink message sent to %s: %s" % (dev_eui, msg.payload.decode('utf-8')))
            # TODO: create/store unique downlink identifier to match with ACK
        elif event_type == 'packet_ack':
            self.stats['down_ack_received'] += 1
            self.log.debug("Downlink acknowledged from %s: %s" % (dev_eui, msg.payload.decode('utf-8')))
            # TODO: reference unique downlink identifier to handle retries
        elif event_type == 'class':
            # TODO: callback to modify the mote attributes
            self.stats['class'] += 1
            self.log.debug("LoRa class update mote %s now Class %s" % (dev_eui, msg.payload.decode('utf-8')))
            pass

    def send_lora_downlink(self, dev_eui, lora_payload, data_type=None, ack=True):
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
        topic_down = "lora/" + dev_eui + "/down"
        ack_str = ", \"ack\": true" if ack else ""
        data_to_mote = "{ \"data\": \"%s\"%s }" % (b64_str, ack_str)
        self.log.debug("Sending MQTT Topic: %s Data: %s" % (topic_down, data_to_mote))
        self.lora_client.publish(topic_down, data_to_mote)

    def get_statistics(self):
        """Returns a dictionary of operating statistics for the modem/network
        :return list of strings containing key statistics
        """
        stat_list = [
            ('LoRa motes joined', self.stats['joined']),
            ('LoRa motes rejected', self.stats['join_rejected']),
            ('LoRa uplink messages', self.stats['up']),
            ('LoRa uplink ACK requests', self.stats['up_ack_request']),
            ('MQTT downlink publishes', self.stats['down_publish']),
            ('LoRa downlink messages', self.stats['down']),
            ('LoRa downlink ACK received', self.stats['down_ack_received']),
            ('LoRa mote class updates', self.stats['class'])
        ]
        return stat_list

    def log_statistics(self):
        """Logs the modem/network statistics"""
        self.log.info("*" * 28 + " LoRaWAN STATISTICS " + "*" * 28)
        self.log.info("* Product ID: %s" % self.product_id)
        self.log.info("* Device ID: %s" % self.device_id)
        self.log.info("* Hardware version: %s" % self.hardware_version)
        self.log.info("* Firmware version: %s" % self.software_version)
        self.log.info("* LoRa mCard:")
        for k in self.lora_mcard:
            self.log.info("*    %s: %s" % (k, self.lora_mcard[k]))
        self.log.info("* LoRa Network Server version: %s" % self.lns_version)
        if len(motes) > 0:
            self.log.info("* Motes joined:")
        for m in self.motes:
            self.log.info("*   %s", m)
        for stat in self.get_statistics():
            self.log.info("* %s: %s" % (stat[0], str(stat[1])))
        self.log.info("*" * 75)
