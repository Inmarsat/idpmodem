#!/usr/bin/env python3
#coding: utf-8
"""
Sends a Hello World text message and waits for a message to print.

"""
import asyncio
from argparse import ArgumentParser
import sys
from time import sleep

from idpmodem.atcommand_async import IdpModemAsyncioClient
from idpmodem.constants import FORMAT_TEXT


def parse_args(argv: tuple) -> dict:
    """
    Parses the command line arguments.

    Args:
        argv: An array containing the command line arguments.
    
    Returns:
        A dictionary containing the command line arguments and their values.

    """
    parser = ArgumentParser(description="Hello World from an IDP modem.")
    parser.add_argument('-p', '--port', dest='port', type=str, default='/dev/ttyUSB0',
                        help="the serial port of the IDP modem")
    return vars(parser.parse_args(args=argv[1:]))


def main():
    user_options = parse_args(sys.argv)
    port = user_options['port']
    return_message_complete = False
    forward_message_received = False
    try:
        modem = IdpModemAsyncioClient(port=port)
        return_name = asyncio.run(modem.message_mo_send(
                                        data='Hello World',
                                        data_format=FORMAT_TEXT,
                                        sin=200,
                                        min=0))
        print('Assigned return message name: {}'.format(return_name))
        while not forward_message_received:
            sleep(5)
            return_mesage_statuses = asyncio.run(modem.message_mo_state())
            if len(return_mesage_statuses) == 0 and not return_message_complete:
                return_message_complete = True
                print('Message {} delivered to cloud'.format(return_name))
            forward_messages = asyncio.run(modem.message_mt_waiting())
            if len(forward_messages) > 0:
                forward_message_received = True
                name = forward_messages[0]['name']
                print(asyncio.run(modem.message_mt_get(name, FORMAT_TEXT)))
    
    except KeyboardInterrupt:
        print('Interrupted by user')
    

if __name__ == '__main__':
    main()
