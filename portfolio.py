"""
Pure portfolio calculation functions.
No database access — all inputs passed explicitly for testability.

Cost basis uses the running-average method: every SELL removes
`qty_sold * average_cost` from the cost basis and books the difference
between the sale price and the average cost (minus fees) as realized P&L.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Position:
    instrument_id: int
    name: str
    isin: str
    ticker: str
    currency: str
    total_quantity: float
    average_cost_basis: float   # per unit, instrument currency
    total_invested: float       # remaining cost basis including fees, instrument currency
    last_price: Optional[float]
    market_value: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]
    is_stale: bool = False
    quote_timestamp: Optional[datetime] = None
    realized_pl: float = 0.0    # instrument currency
    # EUR-denominated figures (None when the FX rate is unavailable)
    total_invested_eur: Optional[float] = None
    market_value_eur: Optional[float] = None
    unrealized_pl_eur: Optional[float] = None
    realized_pl_eur: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.total_quantity > 0


@dataclass
class PACPosition:
    pac_id: int
    pac_name: str
    total_invested: float
    market_value: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]
    realized_pl: float = 0.0


@dataclass
class PortfolioSummary:
    total_invested: float                   # EUR
    total_market_value: Optional[float]     # EUR
    total_unrealized_pl: Optional[float]    # EUR
    total_unrealized_pl_pct: Optional[float]
    n_instruments: int                      # open positions
    has_stale_quotes: bool
    last_updated: Optional[datetime]
    positions: list = field(default_factory=list)
    pac_positions: list = field(default_factory=list)
    total_realized_pl: float = 0.0          # EUR, includes closed positions


def _apply_transactions(transactions: list) -> tuple[float, float, float]:
    """Run the running-average cost algorithm over a list of transactions.

    Returns (quantity, remaining_cost_basis, realized_pl). Transactions are
    expected in chronological order and with unit_price/fees already in the
    position's currency.
    """
    qty = 0.0
    cost = 0.0
    realized = 0.0
    for tx in transactions:
        if tx.transaction_type == "BUY":
            qty += tx.quantity
            cost += tx.quantity * tx.unit_price + tx.fees
            continue

        # SELL
        if qty <= 0:
            log.warning("Ignoring SELL of %s with no open quantity", tx.quantity)
            continue
        qty_sold = tx.quantity
        if qty_sold > qty:
            log.warning("SELL of %s exceeds open quantity %s; clamping", tx.quantity, qty)
            qty_sold = qty
        avg = cost / qty
        cost -= qty_sold * avg
        qty -= qty_sold
        realized += qty_sold * (tx.unit_price - avg) - tx.fees

    qty = round(qty, 8)
    if qty <= 0:
        qty = 0.0
        cost = 0.0
    return qty, round(cost, 2), round(realized, 2)


def _to_eur(value: Optional[float], rate: Optional[float]) -> Optional[float]:
    if value is None or rate is None:
        return None
    return round(value * rate, 2)


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
    fx_rate_to_eur: Optional[float] = None,
) -> Position:
    """Compute a position. `last_price` and the transactions' unit_price/fees
    must be expressed in `currency`. `fx_rate_to_eur` is the rate from
    `currency` to EUR (ignored for EUR positions; None = unavailable)."""
    total_qty, total_cost, realized = _apply_transactions(transactions)
    avg_cost = round(total_cost / total_qty, 4) if total_qty > 0 else 0.0

    mv = upl = upl_pct = None
    if last_price is not None and total_qty > 0:
        mv = round(last_price * total_qty, 2)
        upl = round(mv - total_cost, 2)
        upl_pct = round(upl / total_cost * 100, 2) if total_cost > 0 else 0.0

    rate = 1.0 if currency == "EUR" else fx_rate_to_eur

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
        realized_pl=realized,
        total_invested_eur=_to_eur(total_cost, rate),
        market_value_eur=_to_eur(mv, rate),
        unrealized_pl_eur=_to_eur(upl, rate),
        realized_pl_eur=_to_eur(realized, rate),
    )


def compute_pac_position(
    pac_id: int,
    pac_name: str,
    transactions: list,             # all InvestmentTransaction with this pac_id
    last_prices: dict[int, float],  # {instrument_id: price}
) -> PACPosition:
    by_instrument: dict[int, list] = {}
    for tx in transactions:
        by_instrument.setdefault(tx.instrument_id, []).append(tx)

    invested = 0.0
    realized = 0.0
    qty_by_instrument: dict[int, float] = {}
    for iid, txs in by_instrument.items():
        txs = sorted(txs, key=lambda t: getattr(t, "trade_date", None) or 0) if all(
            getattr(t, "trade_date", None) is not None for t in txs
        ) else txs
        qty, cost, rpl = _apply_transactions(txs)
        invested += cost
        realized += rpl
        if qty > 0:
            qty_by_instrument[iid] = qty

    invested = round(invested, 2)
    realized = round(realized, 2)

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
        realized_pl=realized,
    )


def compute_portfolio(positions: list[Position], pac_positions: list[PACPosition]) -> PortfolioSummary:
    """Summarise positions. Totals are EUR-denominated (from the *_eur fields);
    closed positions (quantity 0) contribute nothing to invested/market value
    but their realized P&L is included in `total_realized_pl`."""
    open_positions = [p for p in positions if p.is_open]

    ti = round(sum(p.total_invested_eur for p in open_positions if p.total_invested_eur is not None), 2)
    fx_missing = any(p.total_invested_eur is None for p in open_positions)

    mv = upl = upl_pct = None
    if open_positions and all(p.market_value_eur is not None for p in open_positions):
        mv = round(sum(p.market_value_eur for p in open_positions), 2)  # type: ignore
        upl = round(mv - ti, 2)
        upl_pct = round(upl / ti * 100, 2) if ti > 0 else 0.0

    realized = round(sum(p.realized_pl_eur for p in positions if p.realized_pl_eur is not None), 2)

    has_stale = fx_missing or any(p.is_stale or p.last_price is None for p in open_positions)
    timestamps = [p.quote_timestamp for p in open_positions if p.quote_timestamp]
    last_updated = max(timestamps) if timestamps else None

    return PortfolioSummary(
        total_invested=ti,
        total_market_value=mv,
        total_unrealized_pl=upl,
        total_unrealized_pl_pct=upl_pct,
        n_instruments=len(open_positions),
        has_stale_quotes=has_stale,
        last_updated=last_updated,
        positions=positions,
        pac_positions=pac_positions,
        total_realized_pl=realized,
    )
