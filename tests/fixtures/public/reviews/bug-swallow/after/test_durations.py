import unittest

from durations import parse


class ParseTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual([parse("90s"), parse("5m"), parse("2h")], [90, 300, 7200])

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(parse(" 5m "), 300)


if __name__ == "__main__":
    unittest.main()
