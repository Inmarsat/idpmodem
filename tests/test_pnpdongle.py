from asyncio import run
from logging import DEBUG
from time import time, sleep

from idpmodem.pnpdongle import PnpDongle

def main():
    RUN_TIME = 180   # seconds
    try:
        start_time = time()
        pnpdongle = PnpDongle(log_level=DEBUG)
        modem = pnpdongle.modem
        run(modem.initialize())
        run(modem.lowpower_notifications_enable())
        while time() - start_time < RUN_TIME:
            sleep(5)
        print('Run time {} seconds complete'.format(RUN_TIME))
    except KeyboardInterrupt:
        print('Interrupted by user input')


if __name__ == '__main__':
    main()
