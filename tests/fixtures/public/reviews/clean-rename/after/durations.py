"""Parse short durations such as 90s, 5m, or 2h into seconds."""

SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600}


def parse(text: str) -> int:
    number, unit = text[:-1], text[-1:]
    if unit not in SECONDS_PER_UNIT or not number.isdigit():
        raise ValueError(f"not a duration: {text!r}")
    return int(number) * SECONDS_PER_UNIT[unit]
