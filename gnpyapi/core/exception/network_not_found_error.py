# coding: utf-8


class NetworkNotFoundError(Exception):
    """ Exception raise when a network id does not match a loaded network
    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
