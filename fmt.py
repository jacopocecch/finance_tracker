"""Jinja filters for numbers and money, shared by main.py and investments.py.

Both modules own a separate Jinja2Templates environment; each registers
FILTERS so templates render the same "1.234,56 €" style everywhere.
"""
from typing import Optional

_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CHF": "CHF",
                     "SEK": "kr", "NOK": "kr", "DKK": "kr", "PLN": "zł", "CZK": "Kč",
                     "HUF": "Ft", "RON": "lei", "TRY": "₺", "CNY": "¥", "HKD": "HK$",
                     "SGD": "S$", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "MXN": "MX$"}


def currency_symbol(code: Optional[str]) -> str:
    return _CURRENCY_SYMBOLS.get((code or "EUR").upper(), code or "€")


def num(value, decimals: int = 0, sign: bool = False) -> str:
    """Italian-style number: thousands '.', decimals ','. sign=True adds '+'."""
    if value is None:
        return "—"
    v = round(float(value), decimals)
    if v == 0:
        v = 0.0  # avoid "-0"
    body = f"{abs(v):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if v < 0:
        return "-" + body
    if sign and v > 0:
        return "+" + body
    return body


def eur(value, decimals: int = 0, sign: bool = False) -> str:
    if value is None:
        return "—"
    return num(value, decimals, sign) + " €"


def money(value, currency: Optional[str] = "EUR", decimals: int = 2, sign: bool = False) -> str:
    if value is None:
        return "—"
    return num(value, decimals, sign) + " " + currency_symbol(currency)


FILTERS = {
    "currency_symbol": currency_symbol,
    "num": num,
    "eur": eur,
    "money": money,
}
