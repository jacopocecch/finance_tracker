"""
Pure portfolio calculation functions.
No database access — all inputs passed explicitly for testability.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Position:
    instrument_id: int
    name: str
    isin: str
    ticker: str
    currency: str
    total_quantity: float
    average_cost_basis: float   # per unit
    total_invested: float       # cost basis including fees
    last_price: Optional[float]
    market_value: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]
    is_stale: bool = False
    quote_timestamp: Optional[datetime] = None


@dataclass
class PACPosition:
    pac_id: int
    pac_name: str
    total_invested: float
    market_value: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]


@dataclass
class PortfolioSummary:
    total_invested: float
    total_market_value: Optional[float]
    total_unrealized_pl: Optional[float]
    total_unrealized_pl_pct: Optional[float]
    n_instruments: int
    has_stale_quotes: bool
    last_updated: Optional[datetime]
    positions: list = field(default_factory=list)
    pac_positions: list = field(default_factory=list)


def compute_position(
    instrument_id: int,
    name: str,
    isin: str,
    ticker: str,
    currency: str,
    transactions: list,
    last_price: Optional[float],
    is_stale: bool,
    quote_timestamp: Optional[datetime],
) -> Position:
    total_qty = 0.0
    total_cost = 0.0
    for tx in transactions:
        sign = 1 if tx.transaction_type == "BUY" else -1
        total_qty += sign * tx.quantity
        if tx.transaction_type == "BUY":
            total_cost += tx.quantity * tx.unit_price + tx.fees

    total_qty = round(total_qty, 8)
    total_cost = round(total_cost, 2)
    avg_cost = round(total_cost / total_qty, 4) if total_qty > 0 else 0.0

    mv = upl = upl_pct = None
    if last_price is not None and total_qty > 0:
        mv = round(last_price * total_qty, 2)
        upl = round(mv - total_cost, 2)
        upl_pct = round(upl / total_cost * 100, 2) if total_cost > 0 else 0.0

    return Position(
        instrument_id=instrument_id,
        name=name,
        isin=isin,
        ticker=ticker,
        currency=currency,
        total_quantity=total_qty,
        average_cost_basis=avg_cost,
        total_invested=total_cost,
        last_price=last_price,
        market_value=mv,
        unrealized_pl=upl,
        unrealized_pl_pct=upl_pct,
        is_stale=is_stale,
        quote_timestamp=quote_timestamp,
    )


def compute_pac_position(
    pac_id: int,
    pac_name: str,
    transactions: list,             # all InvestmentTransaction with this pac_id
    last_prices: dict[int, float],  # {instrument_id: price}
) -> PACPosition:
    invested = 0.0
    qty_by_instrument: dict[int, float] = {}
    for tx in transactions:
        if tx.transaction_type == "BUY":
            invested += tx.quantity * tx.unit_price + tx.fees
            qty_by_instrument[tx.instrument_id] = (
                qty_by_instrument.get(tx.instrument_id, 0.0) + tx.quantity
            )
        else:
            qty_by_instrument[tx.instrument_id] = (
                qty_by_instrument.get(tx.instrument_id, 0.0) - tx.quantity
            )

    invested = round(invested, 2)

    if all(iid in last_prices for iid in qty_by_instrument):
        mv = round(sum(qty * last_prices[iid] for iid, qty in qty_by_instrument.items()), 2)
        upl = round(mv - invested, 2)
        upl_pct = round(upl / invested * 100, 2) if invested > 0 else 0.0
    else:
        mv = upl = upl_pct = None

    return PACPosition(
        pac_id=pac_id,
        pac_name=pac_name,
        total_invested=invested,
        market_value=mv,
        unrealized_pl=upl,
        unrealized_pl_pct=upl_pct,
    )


def compute_portfolio(positions: list[Position], pac_positions: list[PACPosition]) -> PortfolioSummary:
    ti = round(sum(p.total_invested for p in positions), 2)

    mv = upl = upl_pct = None
    if positions and all(p.market_value is not None for p in positions):
        mv = round(sum(p.market_value for p in positions), 2)  # type: ignore
        upl = round(mv - ti, 2)
        upl_pct = round(upl / ti * 100, 2) if ti > 0 else 0.0

    has_stale = any(p.is_stale or p.last_price is None for p in positions)
    timestamps = [p.quote_timestamp for p in positions if p.quote_timestamp]
    last_updated = max(timestamps) if timestamps else None

    return PortfolioSummary(
        total_invested=ti,
        total_market_value=mv,
        total_unrealized_pl=upl,
        total_unrealized_pl_pct=upl_pct,
        n_instruments=len(positions),
        has_stale_quotes=has_stale,
        last_updated=last_updated,
        positions=positions,
        pac_positions=pac_positions,
    )
