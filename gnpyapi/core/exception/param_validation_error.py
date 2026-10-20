# coding: utf-8


class ParamValidationError(Exception):
    """ Exception raise when an element parameter update is not allowed or out of range
    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
