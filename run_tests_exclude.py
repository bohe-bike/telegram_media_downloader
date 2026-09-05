"""Run the media downloader test suite excluding network-bound tests."""
import sys
import unittest

from tests.test_media_downloader import MediaDownloaderTestCase

EXCLUDE = {"test_main_with_bot"}


def build_suite():
    suite = unittest.TestSuite()
    for name in sorted(dir(MediaDownloaderTestCase)):
        if name.startswith("test_") and name not in EXCLUDE:
            suite.addTest(MediaDownloaderTestCase(name))
    return suite


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(build_suite())
    sys.exit(0 if result.wasSuccessful() else 1)
