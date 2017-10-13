"""
Calculates CRC-16-CCITT checksum for xmodem, intended for use with SkyWave/ORBCOMM IDP modem 
Borrowed from https://stackoverflow.com/questions/25239423/crc-ccitt-16-bit-python-manual-calculation
"""

POLYNOMIAL = 0x1021
PRESET = 0

_debug = False


def _initial(c):
    crc = 0
    c = c << 8
    for j in range(8):
        if (crc ^ c) & 0x8000:
            crc = (crc << 1) ^ POLYNOMIAL
        else:
            crc = crc << 1
        c = c << 1
    return crc

_tab = [_initial(i) for i in range(256)]


def _update_crc(crc, c):
    cc = 0xff & c
    tmp = (crc >> 8) ^ cc
    crc = (crc << 8) ^ _tab[tmp & 0xff]
    crc = crc & 0xffff
    return crc


def crc(string, initial=0xffff):
    crc = initial
    for c in string:
        crc = _update_crc(crc, ord(c))
    return crc


def crc_bytes(*i):
    crc = PRESET
    for b in i:
        crc = _update_crc(crc, b)
    return crc


def main():
    s = raw_input('Enter string: ')
    print('0x{:04X}'.format(crc(s, 0xffff)))


if __name__ == "__main__":
    _debug = False
    main()
