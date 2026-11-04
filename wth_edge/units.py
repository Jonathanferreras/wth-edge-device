"""Display helpers. Data stays metric; only what people read is converted."""

from __future__ import annotations


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def format_temp(celsius: float | None, unit: str = "F", decimals: int = 0) -> str | None:
    if celsius is None:
        return None
    if unit.upper() == "F":
        return f"{c_to_f(celsius):.{decimals}f}°F"
    return f"{celsius:.{decimals}f}°C"
