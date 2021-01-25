"""Base classes for AT command errors.
"""

class AtException(Exception):
    """Base class for AT command exceptions."""
    pass


class AtTimeout(AtException):
    """Indicates a timeout waiting for response."""
    pass


class AtGnssTimeout(AtException):
    """Indicates a timeout waiting for a GNSS fix."""
    pass


class AtCrcError(AtException):
    """Indicates a detected CRC mismatch on a response."""
    pass


class AtCrcConfigError(AtException):
    """Indicates a CRC response was received when none expected or vice versa.
    """
    pass


class AtUnsolicited(AtException):
    """Indicates unsolicited data was received from the modem."""
    pass


class AtBusy(AtException):
    """The modem is busy processing a prior AT command."""
    pass