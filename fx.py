import logging
from datetime import date, datetime, timedelta

import requests
from sqlmodel import Session, select

from database import ExchangeRate

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, datetime]] = {}
_CACHE_TTL = timedelta(hours=12)

_API = "https://api.frankfurter.app"


def get_rate(from_currency: str, to_currency: str = "EUR", session: Session = None) -> float:
    """Current exchange rate (cached 12h)."""
    if from_currency == to_currency:
        return 1.0

    key = f"{from_currency}/{to_currency}"
    cached = _CACHE.get(key)
    if cached and datetime.utcnow() - cached[1] < _CACHE_TTL:
        return cached[0]

    try:
        r = requests.get(f"{_API}/latest", params={"from": from_currency, "to": to_currency}, timeout=5)
        r.raise_for_status()
        rate = r.json()["rates"][to_currency]
        _CACHE[key] = (rate, datetime.utcnow())
        if session:
            session.add(ExchangeRate(from_currency=from_currency, to_currency=to_currency, rate=rate, rate_date=None))
            session.commit()
        return rate
    except Exception as e:
        log.warning("FX fetch failed %s: %s", key, e)

    if session:
        row = session.exec(
            select(ExchangeRate)
            .where(ExchangeRate.from_currency == from_currency, ExchangeRate.to_currency == to_currency)
            .order_by(ExchangeRate.fetched_at.desc())
        ).first()
        if row:
            return row.rate

    return 1.0


def get_rate_on(from_currency: str, on_date: date, to_currency: str = "EUR", session: Session = None) -> float:
    """Historical exchange rate for a specific date (cached in DB)."""
    if from_currency == to_currency:
        return 1.0

    # Check DB cache first
    if session:
        row = session.exec(
            select(ExchangeRate).where(
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
                ExchangeRate.rate_date == on_date,
            )
        ).first()
        if row:
            return row.rate

    try:
        r = requests.get(
            f"{_API}/{on_date.isoformat()}",
            params={"from": from_currency, "to": to_currency},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        # frankfurter returns the actual date used (last business day)
        rate = data["rates"][to_currency]
        actual_date = date.fromisoformat(data["date"])
        if session:
            session.add(ExchangeRate(
                from_currency=from_currency,
                to_currency=to_currency,
                rate=rate,
                rate_date=actual_date,
            ))
            session.commit()
        return rate
    except Exception as e:
        log.warning("FX historical fetch failed %s on %s: %s", from_currency, on_date, e)

    # Fallback to current rate
    return get_rate(from_currency, to_currency, session)


def convert(amount: float, from_currency: str, to_currency: str = "EUR", session: Session = None) -> float:
    return amount * get_rate(from_currency, to_currency, session)


def convert_on(amount: float, from_currency: str, on_date: date, to_currency: str = "EUR", session: Session = None) -> float:
    """Convert using the historical rate on a specific date."""
    return amount * get_rate_on(from_currency, on_date, to_currency, session)
