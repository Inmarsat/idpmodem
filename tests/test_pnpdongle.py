from asyncio import run
from logging import DEBUG
from time import time, sleep

from idpmodem.pnpdongle import PnpDongle

def main():
    RUN_TIME = 60   # seconds
    try:
        start_time = time()
        pnpdongle = PnpDongle(log_level=DEBUG)
        modem = pnpdongle.modem
        run(modem.initialize())
        while time() - start_time > RUN_TIME:
            if pnpdongle.modem_event_callback is None:
                pnpdongle._process_event_queue()
            sleep(5)
    except KeyboardInterrupt:
        print('Interrupted by user input')


if __name__ == '__main__':
    main()
