"""Parse short durations such as 90s, 5m, 2h, or 1d into seconds."""

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse(text: str) -> int:
    number, unit = text[:-1], text[-1:]
    if unit not in UNITS or not number.isdigit():
        raise ValueError(f"not a duration: {text!r}")
    return int(number) * UNITS[unit]
