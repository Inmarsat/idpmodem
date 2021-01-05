import asyncio
from asynctest import CoroutineMock
import pytest
from pytest_mock import MockerFixture

import idpmodem
from idpmodem.utils import get_wrapping_logger
from idpmodem.constants import AT_ERROR_CODES
import idpmodem.atcommand_async
from idpmodem.atcommand_async import IdpModemAsyncioClient, AtException
from idpmodem.atcommand_async import LOGGING_VERBOSE_LEVEL as VERBOSE
from idpmodem.nmea import Location


DEFAULT_PORT = '/dev/ttyUSB0'


def repeat_to_length(string_to_expand: str, length: int) -> str:
    return (string_to_expand * (int(length/len(string_to_expand))+1))[:length]

@pytest.fixture
def modem():
    return IdpModemAsyncioClient(port=DEFAULT_PORT, log_level=VERBOSE)

def test_invalid_port():
    with pytest.raises(ValueError):
        m = IdpModemAsyncioClient(port='invalid')

@pytest.mark.asyncio
async def test_command_at(modem):
    assert await modem.command('AT') == ['OK']

@pytest.mark.asyncio
async def test_initialize_no_crc(modem):
    assert await modem.initialize(crc=False)
    assert modem.crc == False

@pytest.mark.asyncio
async def test_initialize_crc(modem):
    assert await modem.initialize(crc=True)
    assert modem.crc == True

@pytest.mark.asyncio
async def test_lowpower_mode_get(modem):
    res = await modem.lowpower_mode_get()
    assert res == 0

@pytest.mark.asyncio
async def test_message_mo_send_invalid(modem):
    with pytest.raises(AtException) as e:
        res = await modem.message_mo_send('XYZ', 2, 128)
    assert e.value.args[0] == AT_ERROR_CODES[102]

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
def mock_command_message_mt_get(monkeypatch):
    mock = CoroutineMock(
        idpmodem.atcommand_async.IdpModemAsyncioClient.message_mt_get,
        return_value=['%MGFG: ', 'OK']
    )
    monkeypatch.setattr(idpmodem.atcommand_async.IdpModemAsyncioClient,
        'command', mock)
    return mock

@pytest.mark.asyncio
async def test_message_mo_send_mock(mock_command_ok, modem):
    res = await modem.message_mo_send('abc', 1, 128)
    assert isinstance(res, str)
