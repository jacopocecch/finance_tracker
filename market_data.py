"""
Market data abstraction layer.

Default provider: yfinance (unofficial Yahoo Finance wrapper).
The ticker stored on Instrument must be a yfinance-compatible symbol,
e.g. VWCE.MI (Borsa Italiana), IWDA.AS (Euronext Amsterdam), CSPX.L (LSE).

To swap provider: implement MarketDataProvider and call set_provider().
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

QUOTE_TTL_MINUTES = 60


class MarketDataProvider(ABC):
    @abstractmethod
    def fetch_price(self, ticker: str) -> Optional[dict]:
        """Returns {'price': float, 'currency': str, 'timestamp': datetime} or None."""


class YFinanceProvider(MarketDataProvider):
    def fetch_price(self, ticker: str) -> Optional[dict]:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            price = None
            currency = None

            try:
                fi = t.fast_info
                price = fi.last_price
                currency = fi.currency
            except Exception:
                pass

            if price is None:
                hist = t.history(period="5d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                info = t.info
                currency = info.get("currency", "EUR")

            if price is None:
                log.warning(f"No price found for {ticker}")
                return None

            return {
                "price": round(float(price), 4),
                "currency": str(currency or "EUR"),
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
    """Refresh quotes for all active instruments."""
    from database import Instrument
    from sqlmodel import select

    instruments = session.exec(
        select(Instrument).where(Instrument.active == True)
    ).all()
    ok = fail = 0
    for inst in instruments:
        if refresh_quote(inst, session):
            ok += 1
        else:
            fail += 1
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
