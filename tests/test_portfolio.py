from types import SimpleNamespace
from portfolio import compute_position, compute_pac_position, compute_portfolio


def _tx(qty, price, fees=0.0, type_="BUY"):
    return SimpleNamespace(quantity=qty, unit_price=price, fees=fees, transaction_type=type_)


class TestComputePosition:
    def test_single_buy(self):
        txs = [_tx(10, 80.0, fees=2.95)]
        pos = compute_position(1, "VWCE", "IE00B3RBWM25", "VWCE.MI", "EUR", txs, 90.0, False, None)
        assert pos.total_quantity == 10
        assert pos.total_invested == round(10 * 80 + 2.95, 2)
        assert pos.average_cost_basis == round((10 * 80 + 2.95) / 10, 4)
        assert pos.market_value == 900.0
        assert pos.unrealized_pl == round(900 - (800 + 2.95), 2)

    def test_multiple_buys(self):
        txs = [_tx(5, 80.0), _tx(5, 90.0)]
        pos = compute_position(1, "VWCE", "IE00B3RBWM25", "VWCE.MI", "EUR", txs, 85.0, False, None)
        assert pos.total_quantity == 10
        assert pos.total_invested == 850.0
        assert pos.average_cost_basis == 85.0
        assert pos.market_value == 850.0
        assert pos.unrealized_pl == 0.0

    def test_no_quote(self):
        txs = [_tx(10, 80.0)]
        pos = compute_position(1, "VWCE", "IE00B3RBWM25", "VWCE.MI", "EUR", txs, None, True, None)
        assert pos.market_value is None
        assert pos.unrealized_pl is None
        assert pos.is_stale is True

    def test_pl_negative(self):
        txs = [_tx(10, 100.0)]
        pos = compute_position(1, "X", "ISIN", "X.MI", "EUR", txs, 80.0, False, None)
        assert pos.unrealized_pl == -200.0
        assert pos.unrealized_pl_pct == -20.0

    def test_sell_reduces_quantity(self):
        txs = [_tx(10, 80.0), _tx(3, 90.0, type_="SELL")]
        pos = compute_position(1, "X", "ISIN", "X.MI", "EUR", txs, 90.0, False, None)
        assert pos.total_quantity == 7


class TestComputePACPosition:
    def test_basic_pac(self):
        txs = [_tx(10, 80.0, fees=2.95)]
        txs[0].instrument_id = 1
        txs[0].pac_id = 1
        pp = compute_pac_position(1, "PAC Test", txs, {1: 90.0})
        assert pp.total_invested == round(10 * 80 + 2.95, 2)
        assert pp.market_value == 900.0

    def test_missing_price(self):
        txs = [_tx(10, 80.0)]
        txs[0].instrument_id = 1
        txs[0].pac_id = 1
        pp = compute_pac_position(1, "PAC", txs, {})
        assert pp.market_value is None


class TestComputePortfolio:
    def test_empty(self):
        summary = compute_portfolio([], [])
        assert summary.total_invested == 0.0
        assert summary.total_market_value is None
        assert summary.n_instruments == 0

    def test_totals(self):
        from datetime import datetime
        txs1 = [_tx(10, 80.0)]
        txs2 = [_tx(5, 100.0)]
        pos1 = compute_position(1, "A", "ISIN1", "A.MI", "EUR", txs1, 90.0, False, datetime.utcnow())
        pos2 = compute_position(2, "B", "ISIN2", "B.MI", "EUR", txs2, 110.0, False, datetime.utcnow())
        summary = compute_portfolio([pos1, pos2], [])
        assert summary.total_invested == 800 + 500
        assert summary.total_market_value == 900 + 550
        assert summary.total_unrealized_pl == (900 + 550) - (800 + 500)
