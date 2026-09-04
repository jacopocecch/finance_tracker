"""Route-level smoke tests for the investments router on an in-memory DB.
Network (yfinance / frankfurter) is stubbed; finance.db is never touched."""

from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import fx
import investments
import market_data
from database import Instrument, InvestmentTransaction, MarketQuote, PAC, PACComponent, get_session


class _StubProvider(market_data.MarketDataProvider):
    def fetch_price(self, ticker):
        return {"price": 100.0, "currency": "EUR", "timestamp": datetime.now(timezone.utc)}


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(fx, "engine", engine)
    monkeypatch.setattr(fx, "_CACHE", {"USD/EUR": (0.5, datetime.utcnow())})
    monkeypatch.setattr(fx.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setattr(market_data, "_provider", _StubProvider())

    app = FastAPI()
    app.include_router(investments.router)

    def _override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _override
    client = TestClient(app, follow_redirects=False)
    return engine, client


def _seed(engine):
    with Session(engine) as s:
        eur = Instrument(name="EUR ETF", isin="IE0001", ticker="A.MI", currency="EUR")
        usd = Instrument(name="USD ETF", isin="US0001", ticker="B.N", currency="USD")
        s.add(eur); s.add(usd); s.flush()
        s.add(InvestmentTransaction(instrument_id=eur.id, transaction_type="BUY", trade_date=date(2024, 1, 1),
                                    quantity=10, unit_price=80.0, fees=0.0, currency="EUR"))
        s.add(InvestmentTransaction(instrument_id=usd.id, transaction_type="BUY", trade_date=date(2024, 1, 1),
                                    quantity=10, unit_price=100.0, fees=0.0, currency="USD"))
        s.add(InvestmentTransaction(instrument_id=usd.id, transaction_type="SELL", trade_date=date(2024, 2, 1),
                                    quantity=4, unit_price=120.0, fees=1.0, currency="USD"))
        now = datetime.now(timezone.utc)
        s.add(MarketQuote(instrument_id=eur.id, price=90.0, currency="EUR", quote_timestamp=now, is_stale=False))
        s.add(MarketQuote(instrument_id=usd.id, price=110.0, currency="USD", quote_timestamp=now, is_stale=False))
        s.commit()
        return eur.id, usd.id


class TestBuildPortfolioData:
    def test_summary_is_eur_denominated_and_positions_native(self, env):
        engine, _ = env
        eur_id, usd_id = _seed(engine)
        with Session(engine) as s:
            summary = investments._build_portfolio_data(s)
        by_id = {p.instrument_id: p for p in summary.positions}
        usd = by_id[usd_id]
        assert usd.total_quantity == 6
        assert usd.total_invested == 600.0          # native USD, avg cost 100
        assert usd.realized_pl == 4 * 20 - 1.0
        assert usd.total_invested_eur == 300.0
        assert usd.market_value_eur == 330.0
        assert summary.total_invested == 800.0 + 300.0
        assert summary.total_market_value == 900.0 + 330.0
        assert summary.total_realized_pl == 39.5

    def test_fx_unavailable_does_not_crash(self, env, monkeypatch):
        engine, _ = env
        _seed(engine)
        monkeypatch.setattr(fx, "_CACHE", {})
        with Session(engine) as s:
            summary = investments._build_portfolio_data(s)
        usd = [p for p in summary.positions if p.currency == "USD"][0]
        assert usd.total_invested == 600.0
        assert usd.total_invested_eur is None
        assert summary.total_market_value is None
        assert summary.total_invested == 800.0

    def test_price_in_inst_currency_converts_quote(self, env):
        engine, _ = env
        with Session(engine) as s:
            inst = Instrument(name="X", isin="X1", ticker="X.L", currency="EUR")
            s.add(inst); s.flush()
            q = MarketQuote(instrument_id=inst.id, price=200.0, currency="USD",
                            quote_timestamp=datetime.now(timezone.utc), is_stale=False)
            assert investments._price_in_inst_currency(q, inst, s) == 100.0
            assert investments._price_in_inst_currency(None, inst, s) is None


class TestRoutes:
    def test_pages_render_with_flash(self, env):
        engine, client = env
        eur_id, usd_id = _seed(engine)
        for url in ["/investments?msg=Ciao", "/investments/instruments?err=Boom",
                    f"/investments/instruments/{usd_id}", "/investments/transactions",
                    "/investments/transactions/add?err=Strumento+mancante", "/investments/pac"]:
            r = client.get(url)
            assert r.status_code == 200, url
        assert "Ciao" in client.get("/investments?msg=Ciao").text
        assert "Boom" in client.get("/investments/instruments?err=Boom").text
        assert "Strumento mancante" in client.get("/investments/transactions/add?err=Strumento+mancante").text
        page = client.get("/investments").text
        assert "$" in page and "realizzato" in page

    def test_transaction_add_missing_instrument_redirects(self, env):
        _, client = env
        r = client.post("/investments/transactions/add", data={
            "instrument_id": "", "trade_date": "2024-01-01", "quantity": "1", "unit_price": "10",
        })
        assert r.status_code == 303
        assert "err=Strumento+mancante" in r.headers["location"]

    def test_transaction_edit_updates_type(self, env):
        engine, client = env
        eur_id, _ = _seed(engine)
        with Session(engine) as s:
            tx = s.exec(select(InvestmentTransaction).where(InvestmentTransaction.instrument_id == eur_id)).first()
            tx_id = tx.id
        r = client.post(f"/investments/transactions/{tx_id}/edit", data={
            "transaction_type": "SELL", "trade_date": "2024-01-02", "quantity": "5", "unit_price": "10",
        })
        assert r.status_code == 303
        with Session(engine) as s:
            assert s.get(InvestmentTransaction, tx_id).transaction_type == "SELL"
        r = client.post(f"/investments/transactions/{tx_id}/edit", data={
            "transaction_type": "BOGUS", "trade_date": "2024-01-02", "quantity": "5", "unit_price": "10",
        })
        assert "err=" in r.headers["location"]

    def test_instrument_edit_updates_type(self, env):
        engine, client = env
        eur_id, _ = _seed(engine)
        r = client.post(f"/investments/instruments/{eur_id}/edit", data={
            "name": "N", "ticker": "T", "type": "Fondo", "currency": "EUR",
        })
        assert r.status_code == 303
        with Session(engine) as s:
            assert s.get(Instrument, eur_id).type == "Fondo"

    def test_pac_add_skips_empty_instrument_rows(self, env):
        engine, client = env
        eur_id, _ = _seed(engine)
        r = client.post("/investments/pac/add", data={
            "name": "P", "instrument_ids": ["", str(eur_id)], "target_weights": ["", "3"],
        })
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        with Session(engine) as s:
            comps = s.exec(select(PACComponent)).all()
        assert [(c.instrument_id, c.target_weight) for c in comps] == [(eur_id, 3.0)]

    def test_pac_list_confirm_escapes_name(self, env):
        engine, client = env
        with Session(engine) as s:
            s.add(PAC(name="L'anno"))
            s.commit()
        html = client.get("/investments/pac").text
        assert "confirm(\"Archiviare L\\u0027anno?\")" in html or "confirm(\"Archiviare L'anno?\")" in html


class TestMarketData:
    def test_refresh_all_quotes_with_no_instruments(self, env):
        engine, _ = env
        with Session(engine) as s:
            assert market_data.refresh_all_quotes(s) == {"success": 0, "failed": 0}

    def test_valid_price_and_minor_units(self):
        assert market_data._valid_price(float("nan")) is None
        assert market_data._valid_price(None) is None
        assert market_data._valid_price(12.5) == 12.5
        assert market_data.normalize_quote(1234.0, "GBp") == (12.34, "GBP")
        assert market_data.normalize_quote(1234.0, "GBX") == (12.34, "GBP")
        assert market_data.normalize_quote(500.0, "ZAc") == (5.0, "ZAR")
        assert market_data.normalize_quote(500.0, "ILA") == (5.0, "ILS")
        assert market_data.normalize_quote(12.34, "GBP") == (12.34, "GBP")
        assert market_data.normalize_quote(1.0, None) == (1.0, "EUR")
