import unittest

from durations import parse


class ParseTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual([parse("90s"), parse("5m"), parse("2h")], [90, 300, 7200])

    def test_invalid_text_is_rejected(self) -> None:
        for text in ("", "5", "m", "5x", "five minutes"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text)


if __name__ == "__main__":
    unittest.main()
