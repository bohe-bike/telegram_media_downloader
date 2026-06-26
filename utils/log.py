"""Util module to handle logs."""
import logging


class LogFilter(logging.Filter):
    """
    Custom Log Filter.

    Ignore logs from specific functions.
    """

    ignored_func_names = {
        "invoke",
        "send",
        "_fatal_error",
        "_force_close",
    }

    # pylint: disable = W0221
    def filter(self, record):
        if record.funcName in self.ignored_func_names:
            return False
        return True
