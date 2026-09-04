"""
Market data abstraction layer.

Default provider: yfinance (unofficial Yahoo Finance wrapper).
The ticker stored on Instrument must be a yfinance-compatible symbol,
e.g. VWCE.MI (Borsa Italiana), IWDA.AS (Euronext Amsterdam), CSPX.L (LSE).

To swap provider: implement MarketDataProvider and call set_provider().
"""

import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

QUOTE_TTL_MINUTES = 60


class MarketDataProvider(ABC):
    @abstractmethod
    def fetch_price(self, ticker: str) -> Optional[dict]:
        """Returns {'price': float, 'currency': str, 'timestamp': datetime} or None."""


# Currencies quoted in minor units by Yahoo: code → (major currency, divisor).
# Case matters: "GBp" (pence) is not "GBP".
_MINOR_UNIT_CURRENCIES = {
    "GBp": ("GBP", 100.0),
    "GBX": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0),
    "ILA": ("ILS", 100.0),
}


def _valid_price(value) -> Optional[float]:
    """Return value as float if it is a usable price, else None (None/NaN/inf/<=0)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f <= 0:
        return None
    return f


def normalize_quote(price: float, currency: Optional[str]) -> tuple[float, str]:
    """Convert minor-unit quotes (pence, cents) to the major currency."""
    cur = currency or "EUR"
    if cur in _MINOR_UNIT_CURRENCIES:
        major, divisor = _MINOR_UNIT_CURRENCIES[cur]
        return price / divisor, major
    return price, cur


class YFinanceProvider(MarketDataProvider):
    def fetch_price(self, ticker: str) -> Optional[dict]:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            price = None
            currency = None

            try:
                fi = t.fast_info
                price = _valid_price(fi.last_price)
                currency = fi.currency
            except Exception:
                pass

            if price is None:
                hist = t.history(period="5d")
                if not hist.empty:
                    price = _valid_price(hist["Close"].dropna().iloc[-1]) if not hist["Close"].dropna().empty else None
                if price is not None and not currency:
                    try:
                        currency = t.info.get("currency")
                    except Exception:
                        currency = None

            if price is None:
                log.warning(f"No price found for {ticker}")
                return None

            price, currency = normalize_quote(price, str(currency) if currency else None)

            return {
                "price": round(float(price), 4),
                "currency": currency,
                "timestamp": datetime.now(timezone.utc),
            }
        except Exception as e:
            log.error(f"yfinance error for {ticker}: {e}")
            return None


_provider: MarketDataProvider = YFinanceProvider()


def set_provider(p: MarketDataProvider) -> None:
    global _provider
    _provider = p


def get_provider() -> MarketDataProvider:
    return _provider


def refresh_quote(instrument, session) -> bool:
    """Fetch and persist latest quote for one instrument. Returns True on success."""
    from database import MarketQuote
    from sqlmodel import select

    result = _provider.fetch_price(instrument.ticker)

    # Mark existing quotes stale regardless
    for q in session.exec(
        select(MarketQuote).where(MarketQuote.instrument_id == instrument.id)
    ).all():
        q.is_stale = True
        session.add(q)

    if result is None:
        session.commit()
        return False

    session.add(MarketQuote(
        instrument_id=instrument.id,
        price=result["price"],
        currency=result["currency"],
        quote_timestamp=result["timestamp"],
        source="yfinance",
        is_stale=False,
    ))
    session.commit()
    log.info(f"Quote refreshed: {instrument.ticker} = {result['price']} {result['currency']}")
    return True


def refresh_all_quotes(session) -> dict:
    """Refresh quotes for all active instruments. Fetches in parallel, writes serially."""
    from database import Instrument, MarketQuote
    from sqlmodel import select
    from concurrent.futures import ThreadPoolExecutor, as_completed

    instruments = session.exec(
        select(Instrument).where(Instrument.active == True)
    ).all()
    if not instruments:
        return {"success": 0, "failed": 0}

    # Fetch all prices in parallel (network-bound, yfinance is stateless per Ticker)
    def _fetch(inst):
        return inst, _provider.fetch_price(inst.ticker)

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(instruments), 8)) as pool:
        futures = {pool.submit(_fetch, inst): inst for inst in instruments}
        for f in as_completed(futures):
            inst, result = f.result()
            results[inst.id] = (inst, result)

    # Write serially to avoid SQLite contention
    ok = fail = 0
    for inst_id, (inst, result) in results.items():
        for q in session.exec(
            select(MarketQuote).where(MarketQuote.instrument_id == inst.id)
        ).all():
            q.is_stale = True
            session.add(q)
        if result is not None:
            session.add(MarketQuote(
                instrument_id=inst.id,
                price=result["price"],
                currency=result["currency"],
                quote_timestamp=result["timestamp"],
                source="yfinance",
                is_stale=False,
            ))
            log.info(f"Quote refreshed: {inst.ticker} = {result['price']} {result['currency']}")
            ok += 1
        else:
            fail += 1
    session.commit()
    return {"success": ok, "failed": fail}


def latest_quote(instrument_id: int, session):
    """Return the most recent MarketQuote for an instrument, or None."""
    from database import MarketQuote
    from sqlmodel import select

    return session.exec(
        select(MarketQuote)
        .where(MarketQuote.instrument_id == instrument_id)
        .order_by(MarketQuote.quote_timestamp.desc())
    ).first()
