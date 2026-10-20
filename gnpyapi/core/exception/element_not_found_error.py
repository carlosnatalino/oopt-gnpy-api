# coding: utf-8


class ElementNotFoundError(Exception):
    """ Exception raise when an element uid is not part of a loaded network
    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
