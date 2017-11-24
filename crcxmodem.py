"""
Calculates CRC-16-CCITT checksum for xmodem, intended for use with SkyWave/ORBCOMM IDP modem 
Borrowed from https://stackoverflow.com/questions/25239423/crc-ccitt-16-bit-python-manual-calculation
"""

POLYNOMIAL = 0x1021
PRESET = 0


def _initial(c):
    _crc = 0
    c = c << 8
    for j in range(8):
        if (_crc ^ c) & 0x8000:
            _crc = (_crc << 1) ^ POLYNOMIAL
        else:
            _crc = _crc << 1
        c = c << 1
    return _crc


_tab = [_initial(i) for i in range(256)]


def _update_crc(_crc, c):
    cc = 0xff & c
    tmp = (_crc >> 8) ^ cc
    _crc = (_crc << 8) ^ _tab[tmp & 0xff]
    _crc = _crc & 0xffff
    return _crc


def crc(string, initial=0xffff):
    """Returns the CRC value
    :param string to be calculated
    :param initial value of crc
    :returns crc value
    """
    _crc = initial
    for c in string:
        _crc = _update_crc(_crc, ord(c))
    return _crc


def crc_bytes(*i):
    """Return the CRC value of a byte stream
    :param *i byte stream
    :returns crc value
    """
    _crc = PRESET
    for b in i:
        _crc = _update_crc(_crc, b)
    return _crc


def main():
    s = raw_input('Enter string: ')
    print('0x{:04X}'.format(crc(s, 0xffff)))


if __name__ == "__main__":
    main()
