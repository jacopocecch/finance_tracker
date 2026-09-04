from datetime import datetime
from types import SimpleNamespace

from portfolio import compute_position, compute_pac_position, compute_portfolio


def _tx(qty, price, fees=0.0, type_="BUY", instrument_id=1, trade_date=None):
    return SimpleNamespace(
        quantity=qty, unit_price=price, fees=fees, transaction_type=type_,
        instrument_id=instrument_id, pac_id=1, trade_date=trade_date,
    )


def _pos(txs, price, currency="EUR", fx_rate=None, ts=None):
    return compute_position(1, "X", "ISIN", "X.MI", currency, txs, price, False, ts, fx_rate_to_eur=fx_rate)


class TestComputePosition:
    def test_single_buy(self):
        txs = [_tx(10, 80.0, fees=2.95)]
        pos = compute_position(1, "VWCE", "IE00B3RBWM25", "VWCE.MI", "EUR", txs, 90.0, False, None)
        assert pos.total_quantity == 10
        assert pos.total_invested == round(10 * 80 + 2.95, 2)
        assert pos.average_cost_basis == round((10 * 80 + 2.95) / 10, 4)
        assert pos.market_value == 900.0
        assert pos.unrealized_pl == round(900 - (800 + 2.95), 2)
        assert pos.realized_pl == 0.0
        assert pos.is_open

    def test_multiple_buys(self):
        txs = [_tx(5, 80.0), _tx(5, 90.0)]
        pos = _pos(txs, 85.0)
        assert pos.total_quantity == 10
        assert pos.total_invested == 850.0
        assert pos.average_cost_basis == 85.0
        assert pos.market_value == 850.0
        assert pos.unrealized_pl == 0.0

    def test_no_quote(self):
        pos = _pos([_tx(10, 80.0)], None)
        assert pos.market_value is None
        assert pos.unrealized_pl is None
        assert pos.market_value_eur is None

    def test_pl_negative(self):
        pos = _pos([_tx(10, 100.0)], 80.0)
        assert pos.unrealized_pl == -200.0
        assert pos.unrealized_pl_pct == -20.0

    def test_partial_sell_reduces_cost_basis_at_avg_cost(self):
        # avg cost 80 (no fees); sell 3 @ 90 with 1.0 fees
        txs = [_tx(10, 80.0), _tx(3, 90.0, fees=1.0, type_="SELL")]
        pos = _pos(txs, 90.0)
        assert pos.total_quantity == 7
        assert pos.total_invested == 560.0          # 7 * 80
        assert pos.average_cost_basis == 80.0       # unchanged by the sale
        assert pos.realized_pl == 3 * (90 - 80) - 1.0
        assert pos.market_value == 630.0
        assert pos.unrealized_pl == 70.0            # 630 - 560

    def test_sell_then_buy_uses_running_average(self):
        txs = [_tx(10, 100.0), _tx(5, 120.0, type_="SELL"), _tx(5, 80.0)]
        pos = _pos(txs, 100.0)
        # after sell: 5 @ 100 = 500; buy 5 @ 80 = 400 -> 10 units, cost 900
        assert pos.total_quantity == 10
        assert pos.total_invested == 900.0
        assert pos.average_cost_basis == 90.0
        assert pos.realized_pl == 100.0

    def test_fully_sold_position(self):
        txs = [_tx(10, 80.0, fees=2.0), _tx(10, 100.0, fees=3.0, type_="SELL")]
        pos = _pos(txs, 110.0)
        assert pos.total_quantity == 0
        assert pos.total_invested == 0.0
        assert pos.average_cost_basis == 0.0
        assert pos.market_value is None
        assert pos.unrealized_pl is None
        assert pos.realized_pl == round(10 * (100 - 80.2) - 3.0, 2)
        assert not pos.is_open

    def test_oversell_is_clamped(self):
        txs = [_tx(10, 80.0), _tx(15, 90.0, type_="SELL")]
        pos = _pos(txs, 90.0)
        assert pos.total_quantity == 0
        assert pos.total_invested == 0.0
        # only the 10 units actually held are realized
        assert pos.realized_pl == 100.0

    def test_sell_with_nothing_held_is_ignored(self):
        pos = _pos([_tx(5, 90.0, type_="SELL")], 90.0)
        assert pos.total_quantity == 0
        assert pos.realized_pl == 0.0

    def test_eur_fields_for_eur_position(self):
        pos = _pos([_tx(10, 80.0)], 90.0, currency="EUR")
        assert pos.total_invested_eur == 800.0
        assert pos.market_value_eur == 900.0
        assert pos.unrealized_pl_eur == 100.0

    def test_eur_fields_with_fx_rate(self):
        pos = _pos([_tx(10, 80.0)], 90.0, currency="USD", fx_rate=0.5)
        assert pos.total_invested == 800.0           # native
        assert pos.total_invested_eur == 400.0
        assert pos.market_value_eur == 450.0
        assert pos.unrealized_pl_eur == 50.0

    def test_eur_fields_none_without_fx_rate(self):
        pos = _pos([_tx(10, 80.0)], 90.0, currency="USD", fx_rate=None)
        assert pos.total_invested_eur is None
        assert pos.market_value_eur is None


class TestComputePACPosition:
    def test_basic_pac(self):
        pp = compute_pac_position(1, "PAC Test", [_tx(10, 80.0, fees=2.95)], {1: 90.0})
        assert pp.total_invested == round(10 * 80 + 2.95, 2)
        assert pp.market_value == 900.0

    def test_missing_price(self):
        pp = compute_pac_position(1, "PAC", [_tx(10, 80.0)], {})
        assert pp.market_value is None

    def test_sell_reduces_cost_basis(self):
        txs = [_tx(10, 80.0), _tx(4, 100.0, type_="SELL")]
        pp = compute_pac_position(1, "PAC", txs, {1: 100.0})
        assert pp.total_invested == 480.0
        assert pp.market_value == 600.0
        assert pp.realized_pl == 80.0

    def test_fully_sold_instrument_does_not_need_price(self):
        txs = [_tx(10, 80.0, instrument_id=1), _tx(10, 90.0, type_="SELL", instrument_id=1),
               _tx(5, 50.0, instrument_id=2)]
        pp = compute_pac_position(1, "PAC", txs, {2: 60.0})
        assert pp.total_invested == 250.0
        assert pp.market_value == 300.0
        assert pp.realized_pl == 100.0


class TestComputePortfolio:
    def test_empty(self):
        summary = compute_portfolio([], [])
        assert summary.total_invested == 0.0
        assert summary.total_market_value is None
        assert summary.n_instruments == 0

    def test_totals(self):
        pos1 = compute_position(1, "A", "ISIN1", "A.MI", "EUR", [_tx(10, 80.0)], 90.0, False, datetime.utcnow())
        pos2 = compute_position(2, "B", "ISIN2", "B.MI", "EUR", [_tx(5, 100.0)], 110.0, False, datetime.utcnow())
        summary = compute_portfolio([pos1, pos2], [])
        assert summary.total_invested == 800 + 500
        assert summary.total_market_value == 900 + 550
        assert summary.total_unrealized_pl == (900 + 550) - (800 + 500)
        assert summary.n_instruments == 2

    def test_totals_are_eur_denominated(self):
        eur = compute_position(1, "A", "I1", "A.MI", "EUR", [_tx(10, 80.0)], 90.0, False, None)
        usd = compute_position(2, "B", "I2", "B.N", "USD", [_tx(10, 100.0)], 120.0, False, None, fx_rate_to_eur=0.5)
        summary = compute_portfolio([eur, usd], [])
        assert summary.total_invested == 800 + 500
        assert summary.total_market_value == 900 + 600
        assert summary.total_unrealized_pl == 200.0

    def test_fx_unavailable_makes_market_value_unavailable(self):
        eur = compute_position(1, "A", "I1", "A.MI", "EUR", [_tx(10, 80.0)], 90.0, False, None)
        usd = compute_position(2, "B", "I2", "B.N", "USD", [_tx(10, 100.0)], 120.0, False, None, fx_rate_to_eur=None)
        summary = compute_portfolio([eur, usd], [])
        assert summary.total_invested == 800.0
        assert summary.total_market_value is None
        assert summary.has_stale_quotes

    def test_fully_sold_position_excluded_from_totals(self):
        open_pos = compute_position(1, "A", "I1", "A.MI", "EUR", [_tx(10, 80.0)], 90.0, False, datetime.utcnow())
        closed = compute_position(
            2, "B", "I2", "B.MI", "EUR",
            [_tx(10, 50.0), _tx(10, 70.0, type_="SELL")], None, True, None,
        )
        summary = compute_portfolio([open_pos, closed], [])
        assert summary.total_invested == 800.0
        assert summary.total_market_value == 900.0       # not forced to None by the closed one
        assert summary.total_unrealized_pl == 100.0
        assert summary.n_instruments == 1
        assert summary.has_stale_quotes is False
        assert summary.total_realized_pl == 200.0
        assert len(summary.positions) == 2               # still returned for listing
