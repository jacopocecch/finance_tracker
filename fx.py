"""
Exchange rates via frankfurter.app.

Contract:
- `get_rate` / `get_rate_on` never invent a rate: when neither the API nor a
  cached value (in-memory or DB) is available they raise `FxUnavailable`.
- The caller's session is never committed. Cache rows are written through a
  separate short-lived session so a cache-write failure never breaks a
  conversion and never flushes half-processed work of the caller.
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests
from sqlmodel import Session, select

from database import ExchangeRate, engine

log = logging.getLogger(__name__)


class FxUnavailable(Exception):
    """No exchange rate could be obtained from the API or any cache."""


_CACHE: dict[str, tuple[float, datetime]] = {}
_CACHE_TTL = timedelta(hours=12)

_API = "https://api.frankfurter.app"


# ── Internal helpers ─────────────────────────────────────────────────────────

@contextmanager
def _read_session(session: Optional[Session]) -> Iterator[Session]:
    """Yield the caller's session if given, else a short-lived one.

    Reads through the caller's session run with autoflush disabled so a
    cache lookup never flushes the caller's half-processed pending rows
    (which would also take SQLite's write lock and block the cache writer)."""
    if session is not None:
        with session.no_autoflush:
            yield session
        return
    with Session(engine) as s:
        yield s


def _store_rate(from_currency: str, to_currency: str, rate: float, rate_date: Optional[date]) -> None:
    """Persist a cache row in its own session. Never raises."""
    try:
        with Session(engine) as s:
            s.add(ExchangeRate(
                from_currency=from_currency,
                to_currency=to_currency,
                rate=rate,
                rate_date=rate_date,
            ))
            s.commit()
    except Exception as e:  # pragma: no cover - defensive
        log.warning("FX cache write failed %s/%s (%s): %s", from_currency, to_currency, rate_date, e)


def _db_current_rate(from_currency: str, to_currency: str, session: Optional[Session]) -> Optional[float]:
    try:
        with _read_session(session) as s:
            row = s.exec(
                select(ExchangeRate)
                .where(
                    ExchangeRate.from_currency == from_currency,
                    ExchangeRate.to_currency == to_currency,
                    ExchangeRate.rate_date.is_(None),
                )
                .order_by(ExchangeRate.fetched_at.desc())
            ).first()
            return row.rate if row else None
    except Exception as e:  # pragma: no cover - defensive
        log.warning("FX cache read failed %s/%s: %s", from_currency, to_currency, e)
        return None


def _db_rate_on(from_currency: str, to_currency: str, on_date: date, exact: bool,
                session: Optional[Session]) -> Optional[float]:
    """Exact-date row, or (exact=False) the newest row with rate_date <= on_date."""
    try:
        with _read_session(session) as s:
            stmt = select(ExchangeRate).where(
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
            )
            if exact:
                stmt = stmt.where(ExchangeRate.rate_date == on_date)
            else:
                stmt = stmt.where(ExchangeRate.rate_date.is_not(None), ExchangeRate.rate_date <= on_date)
                stmt = stmt.order_by(ExchangeRate.rate_date.desc())
            row = s.exec(stmt).first()
            return row.rate if row else None
    except Exception as e:  # pragma: no cover - defensive
        log.warning("FX cache read failed %s/%s on %s: %s", from_currency, to_currency, on_date, e)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def get_rate(from_currency: str, to_currency: str = "EUR", session: Session = None) -> float:
    """Current exchange rate.

    Order: fresh in-memory cache (12h) → API → stale in-memory cache →
    DB row with rate_date NULL (newest fetched_at) → FxUnavailable.
    """
    if from_currency == to_currency:
        return 1.0

    key = f"{from_currency}/{to_currency}"
    cached = _CACHE.get(key)
    if cached and datetime.utcnow() - cached[1] < _CACHE_TTL:
        return cached[0]

    try:
        r = requests.get(f"{_API}/latest", params={"from": from_currency, "to": to_currency}, timeout=5)
        r.raise_for_status()
        rate = float(r.json()["rates"][to_currency])
        _CACHE[key] = (rate, datetime.utcnow())
        _store_rate(from_currency, to_currency, rate, None)
        return rate
    except Exception as e:
        log.warning("FX fetch failed %s: %s", key, e)

    if cached:
        log.warning("Using stale in-memory FX rate for %s", key)
        return cached[0]

    rate = _db_current_rate(from_currency, to_currency, session)
    if rate is not None:
        log.warning("Using DB-cached FX rate for %s", key)
        return rate

    raise FxUnavailable(f"No exchange rate available for {key}")


def get_rate_on(from_currency: str, on_date: date, to_currency: str = "EUR", session: Session = None) -> float:
    """Historical exchange rate for a specific date.

    Order: DB row with rate_date == on_date → API (stored under the requested
    date so weekends/holidays hit the cache next time) → nearest DB row with
    rate_date <= on_date → current rate via get_rate → FxUnavailable.
    """
    if from_currency == to_currency:
        return 1.0

    rate = _db_rate_on(from_currency, to_currency, on_date, exact=True, session=session)
    if rate is not None:
        return rate

    try:
        r = requests.get(
            f"{_API}/{on_date.isoformat()}",
            params={"from": from_currency, "to": to_currency},
            timeout=5,
        )
        r.raise_for_status()
        rate = float(r.json()["rates"][to_currency])
        _store_rate(from_currency, to_currency, rate, on_date)
        return rate
    except Exception as e:
        log.warning("FX historical fetch failed %s on %s: %s", from_currency, on_date, e)

    rate = _db_rate_on(from_currency, to_currency, on_date, exact=False, session=session)
    if rate is not None:
        log.warning("Using nearest cached FX rate for %s/%s on %s", from_currency, to_currency, on_date)
        return rate

    log.warning("Falling back to current FX rate for %s/%s on %s", from_currency, to_currency, on_date)
    return get_rate(from_currency, to_currency, session)


def convert(amount: float, from_currency: str, to_currency: str = "EUR", session: Session = None) -> float:
    """Convert at the current rate. Raises FxUnavailable."""
    return amount * get_rate(from_currency, to_currency, session)


def convert_on(amount: float, from_currency: str, on_date: date, to_currency: str = "EUR", session: Session = None) -> float:
    """Convert using the historical rate on a specific date. Raises FxUnavailable."""
    return amount * get_rate_on(from_currency, on_date, to_currency, session)
