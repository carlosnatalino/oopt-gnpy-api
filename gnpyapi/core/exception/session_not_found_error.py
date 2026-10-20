# coding: utf-8


class SessionNotFoundError(Exception):
    """ Exception raise when a session token is missing or does not match an open session
    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
