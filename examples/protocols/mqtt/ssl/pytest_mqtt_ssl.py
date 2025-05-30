# SPDX-FileCopyrightText: 2022-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Unlicense OR CC0-1.0
import logging
import os
import re
import ssl
import sys
from threading import Event
from threading import Thread

import paho.mqtt.client as mqtt
import pexpect
import pytest
from pytest_embedded import Dut
from pytest_embedded_idf.utils import idf_parametrize

event_client_connected = Event()
event_stop_client = Event()
event_client_received_correct = Event()
event_client_received_binary = Event()
message_log = ''


# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):  # type: (mqtt.Client, str, bool, str) -> None
    _ = (userdata, flags)
    print('Connected with result code ' + str(rc))
    event_client_connected.set()
    client.subscribe('/topic/qos0')


def mqtt_client_task(client):  # type: (mqtt.Client) -> None
    while not event_stop_client.is_set():
        client.loop()


# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):  # type: (mqtt.Client, tuple, mqtt.client.MQTTMessage) -> None
    global message_log
    global event_client_received_correct
    global event_client_received_binary
    if msg.topic == '/topic/binary':
        binary, bin_size = userdata
        print('Receiving binary from esp and comparing with {}, size {}...'.format(binary, bin_size))
        with open(binary, 'rb') as f:
            bin = f.read()
            if bin[:bin_size] == msg.payload[:bin_size]:
                print('...matches!')
                event_client_received_binary.set()
                return
            recv_binary = binary + '.received'
            with open(recv_binary, 'w', encoding='utf-8') as fw:
                fw.write(msg.payload)
            raise ValueError(
                'Received binary (saved as: {}) does not match the original file: {}'.format(recv_binary, binary)
            )

    payload = msg.payload.decode()
    if not event_client_received_correct.is_set() and payload == 'data':
        client.subscribe('/topic/binary')
        client.publish('/topic/qos0', 'send binary please')
        if msg.topic == '/topic/qos0' and payload == 'data':
            event_client_received_correct.set()
    message_log += 'Received data:' + msg.topic + ' ' + payload + '\n'


@pytest.mark.ethernet
@idf_parametrize('target', ['esp32'], indirect=['target'])
def test_examples_protocol_mqtt_ssl(dut):  # type: (Dut) -> None
    broker_url = ''
    broker_port = 0
    """
    steps:
      1. join AP and connects to ssl broker
      2. Test connects a client to the same broker
      3. Test evaluates python client received correct qos0 message
      4. Test ESP32 client received correct qos0 message
      5. Test python client receives binary data from running partition and compares it with the binary
    """
    binary_file = os.path.join(dut.app.binary_path, 'mqtt_ssl.bin')
    bin_size = os.path.getsize(binary_file)
    logging.info('[Performance][mqtt_ssl_bin_size]: %s KB', bin_size // 1024)

    # Look for host:port in sdkconfig
    try:
        value = re.search(r'\:\/\/([^:]+)\:([0-9]+)', dut.app.sdkconfig.get('BROKER_URI'))
        assert value is not None
        broker_url = value.group(1)
        broker_port = int(value.group(2))
        bin_size = min(int(dut.app.sdkconfig.get('BROKER_BIN_SIZE_TO_SEND')), bin_size)
    except Exception:
        print('ENV_TEST_FAILURE: Cannot find broker url in sdkconfig')
        raise
    client = None
    # 1. Test connects to a broker
    try:
        client = mqtt.Client()
        client.on_connect = on_connect
        client.on_message = on_message
        client.user_data_set((binary_file, bin_size))
        client.tls_set(None, None, None, cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2, ciphers=None)
        client.tls_insecure_set(True)
        print('Connecting...')
        client.connect(broker_url, broker_port, 60)
    except Exception:
        print(
            'ENV_TEST_FAILURE: Unexpected error while connecting to broker {}: {}:'.format(
                broker_url, sys.exc_info()[0]
            )
        )
        raise
    # Starting a py-client in a separate thread
    thread1 = Thread(target=mqtt_client_task, args=(client,))
    thread1.start()
    try:
        print('Connecting py-client to broker {}:{}...'.format(broker_url, broker_port))
        if not event_client_connected.wait(timeout=30):
            raise ValueError('ENV_TEST_FAILURE: Test script cannot connect to broker: {}'.format(broker_url))
        try:
            ip_address = dut.expect(r'IPv4 address: (\d+\.\d+\.\d+\.\d+)[^\d]', timeout=30)[0]
            print('Connected to AP with IP: {}'.format(ip_address))
        except pexpect.TIMEOUT:
            print('ENV_TEST_FAILURE: Cannot connect to AP')
            raise
        print('Checking py-client received msg published from esp...')
        if not event_client_received_correct.wait(timeout=30):
            raise ValueError('Wrong data received, msg log: {}'.format(message_log))
        print('Checking esp-client received msg published from py-client...')
        dut.expect(r'DATA=send binary please', timeout=30)
        print('Receiving binary data from running partition...')
        if not event_client_received_binary.wait(timeout=30):
            raise ValueError('Binary not received within timeout')
    finally:
        event_stop_client.set()
        thread1.join()
