import unittest

from scraper.index_scheduler import _parse_hhmm


class IndexSchedulerTests(unittest.TestCase):
    def test_parses_valid_time(self):
        self.assertEqual(_parse_hhmm("04:15"), (4, 15))

    def test_invalid_time_uses_default(self):
        self.assertEqual(_parse_hhmm("not-a-time"), (0, 30))

    def test_values_are_clamped(self):
        self.assertEqual(_parse_hhmm("99:99"), (23, 59))


if __name__ == "__main__":
    unittest.main()
