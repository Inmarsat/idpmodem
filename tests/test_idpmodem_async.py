from asyncio import run
from asynctest import CoroutineMock
import pytest
from pytest_mock import MockerFixture
from threading import Thread
from time import sleep

import idpmodem
from idpmodem.utils import validate_serial_port
from idpmodem.constants import AT_ERROR_CODES
from idpmodem.atcommand_async import (IdpModemAsyncioClient,
                                      AtException,
                                      AtGnssTimeout)
from idpmodem.atcommand_async import LOGGING_VERBOSE_LEVEL as VERBOSE
from idpmodem.nmea import Location


DEFAULT_PORT = '/dev/ttyUSB0'
LOG_LEVEL = VERBOSE

#TODO: mock serial port

@pytest.fixture
def modem(mocker: MockerFixture):
    if not validate_serial_port(DEFAULT_PORT):
        mocker.patch('idpmodem.atcommand_async.validate_serial_port',
            return_value=(True, 'mockSerial'))
    return IdpModemAsyncioClient(port=DEFAULT_PORT, log_level=LOG_LEVEL)

def test_invalid_port():
    with pytest.raises(ValueError):
        IdpModemAsyncioClient(port='invalid')

@pytest.fixture
def mock_command_ok(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['OK']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.fixture
def mock_command_ok_delay(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['OK']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    sleep(3)
    return mock

@pytest.fixture
def mock_command_crc_error(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['ERROR', '100']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.fixture
def mock_command_gnss_timeout(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['ERROR', '108']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_initialize_crc(mock_command_ok, modem):
    assert await modem.initialize(crc=True)
    assert modem.crc == True

@pytest.mark.asyncio
async def test_initialize_no_crc(mock_command_ok, modem):
    assert await modem.initialize(crc=False)
    assert modem.crc == False

@pytest.mark.asyncio
async def test_multithread(modem):
    
    def parallel_command():
        assert run(modem.initialize()) is not None
    
    if not validate_serial_port(DEFAULT_PORT):
        pytest.skip("Test irrelevant if not connected to real modem/serial")
    t = Thread(target=parallel_command)
    t.start()
    assert await modem.initialize()
    t.join()

@pytest.fixture
def mock_lowpower_mode_get(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['000','OK']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_lowpower_mode_get(mock_lowpower_mode_get, modem):
    res = await modem.lowpower_mode_get()
    assert res == 0

@pytest.fixture
def mock_command_invalid_parameter(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=['ERROR','102']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_message_mo_send_invalid(mock_command_invalid_parameter, modem):
    with pytest.raises(AtException) as e:
        await modem.message_mo_send('XYZ', 2, 128)
    assert e.value.args[0] == AT_ERROR_CODES[102]

@pytest.mark.asyncio
async def test_message_mo_send_mock(mock_command_ok, modem):
    res = await modem.message_mo_send('abc', 1, 128)
    assert isinstance(res, str)

@pytest.fixture
def mock_command_message_mt_get(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=[
            '%MGFG: "FM01.01",01.01,0,18,2,13,1,"\\01Hello World"',
            'OK'
        ]
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_message_mt_get_mock(mock_command_message_mt_get, modem):
    res = await modem.message_mt_get('FM01.01', 1)
    assert isinstance(res, dict)
    assert res['name'] == 'FM01.01'
    assert res['system_message_number'] == 1
    assert res['system_message_sequence'] == 1
    assert res['priority'] == 0
    assert res['sin'] == 18
    assert res['min'] == 1
    assert res['state'] == 2
    assert res['length'] == 13
    assert res['data_format'] == 1
    assert res['raw_payload'] == '\\12\\01Hello World'
    assert res['bytes'] == b'\x12\x01Hello World'

NMEA_DATA_MOCK = [
    '$GPRMC,184849.000,A,4517.1094,N,07550.9143,W,0.20,0.00,211120,,,A,V*0E',
    '$GPGGA,184849.000,4517.1094,N,07550.9143,W,1,07,1.6,119.2,M,-34.3,M,,0000*67',
    '$GPGSA,A,3,32,10,31,21,23,01,20,,,,,,2.3,1.6,1.6,1*2D',
    '$GPGSV,2,1,08,01,20,310,22,10,54,161,34,20,19,154,32,21,24,288,38,0*69',
    '$GPGSV,2,2,08,23,24,149,29,25,39,120,23,31,39,223,43,32,75,332,34,0*68',
]

@pytest.fixture
def mock_command_gnss_nmea_get(monkeypatch):
    nmea_mock = NMEA_DATA_MOCK.copy()
    nmea_mock[0] = '%GPS: ' + nmea_mock[0]
    nmea_mock = nmea_mock + ['OK']
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.command,
        return_value=nmea_mock
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_gnss_nmea_get_mock(mock_command_gnss_nmea_get, modem):
    res = await modem.gnss_nmea_get()
    assert isinstance(res, list)
    assert res[0] == NMEA_DATA_MOCK[0]
    assert res[1] == NMEA_DATA_MOCK[1]
    assert res[2] == NMEA_DATA_MOCK[2]
    assert res[3] == NMEA_DATA_MOCK[3]
    assert res[4] == NMEA_DATA_MOCK[4]

@pytest.mark.asyncio
async def test_location_mock(mock_command_gnss_nmea_get, modem):
    res = await modem.location()
    assert isinstance(res, Location)
    assert res.latitude == 45.285157
    assert res.longitude == -75.848572

@pytest.mark.asyncio
async def test_location_timeout(mock_command_gnss_timeout, modem):
    with pytest.raises(AtGnssTimeout):
        await modem.location()
