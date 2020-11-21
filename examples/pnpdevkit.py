"""TODO: Add docstring"""

from idpmodem.pnpdongle import PnpDongle


def main():
    dongle = PnpDongle()
    modem = dongle.modem
    if modem.connected:
        print('IDP modem connected')
    else:
        print('Something went wrong...')


if __name__ == '__main__':
    main()
