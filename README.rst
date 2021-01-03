========
idpmodem
========

Python library and samples for integrating with satellite Internet-of-Things 
modems using the `Inmarsat <https://www.inmarsat.com>`_
IsatData Pro ("IDP") network.

`Documentation <https://inmarsat.github.io/idpmodem/>`_

Overview
--------

IDP is a store-and-forward satellite messaging technology with
messages up to 6400 bytes mobile-originated or 10000 bytes mobile-terminated.
*Messages* are sent from or to a *Mobile* using its globally unique ID,
transacted through a *Mailbox* that provides authentication, encryption and
data segregation for cloud-based or enterprise client applications via a
network **Messaging API**.

The first byte of the message is referred to as the
*Service Identification Number* (**SIN**) where values below 16 are reserved
for system use.  SIN is intended to capture the concept of embedded
microservices used by an application.

The second byte of the message can optionally be defined as the
*Message Identifier Number* (**MIN**) intended to support remote operations 
within each embedded microservice with defined binary formatting.
The MIN concept also supports the optional *Message Definition File* feature
allowing an XML file to be applied which presents a JSON-tagged message
structure on the network API.

Terminology:

* MO = **Mobile Originated** aka *Return* aka *From-Mobile*
  message sent from modem to cloud/enterprise application
* MT = **Mobile Terminated** aka *Forward message* aka *To-Mobile*
  message sent from cloud/enterprise application to modem

Modem operation
---------------

Upon power-up or reset, the modem first acquires its location using 
Global Navigation Satellite Systems (GNSS).
After getting its location, the modem tunes to the correct frequency, then
registers on the Inmarsat network.  Once registered it can communicate on the
network.
Prolonged obstruction of satellite signal will put the modem into a "blockage"
state from which it will automatically try to recover based on an algorithm
influenced by its *power mode* setting.