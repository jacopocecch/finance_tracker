"""fx.py contract tests. The frankfurter API is mocked via requests.get and
the cache DB is an in-memory SQLite engine (never finance.db)."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import fx
from database import ExchangeRate


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def mem_engine(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(fx, "engine", engine)
    monkeypatch.setattr(fx, "_CACHE", {})
    return engine


def _api_ok(rate, day="2024-03-01"):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _Resp({"rates": {params["to"]: rate}, "date": day})

    fake_get.calls = calls
    return fake_get


def _api_down(url, params=None, timeout=None):
    raise ConnectionError("down")


class TestGetRate:
    def test_same_currency(self, mem_engine):
        assert fx.get_rate("EUR", "EUR") == 1.0

    def test_api_success_caches_in_memory_and_db(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_ok(0.9))
        assert fx.get_rate("USD", "EUR") == 0.9
        assert fx._CACHE["USD/EUR"][0] == 0.9
        with Session(mem_engine) as s:
            rows = s.exec(select(ExchangeRate)).all()
        assert len(rows) == 1
        assert rows[0].rate_date is None
        assert rows[0].rate == 0.9

    def test_raises_when_nothing_available(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_down)
        with pytest.raises(fx.FxUnavailable):
            fx.get_rate("USD", "EUR")
        with pytest.raises(fx.FxUnavailable):
            fx.convert(10.0, "USD", "EUR")

    def test_never_returns_one_as_fallback(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_down)
        with pytest.raises(fx.FxUnavailable):
            fx.get_rate("GBP")

    def test_falls_back_to_stale_memory_cache(self, mem_engine, monkeypatch):
        fx._CACHE["USD/EUR"] = (0.8, datetime.utcnow() - timedelta(days=2))
        monkeypatch.setattr(fx.requests, "get", _api_down)
        assert fx.get_rate("USD", "EUR") == 0.8

    def test_falls_back_to_db_current_row_only(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_down)
        with Session(mem_engine) as s:
            # historical row must NOT be used for the current rate
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.5, rate_date=date(2020, 1, 1)))
            s.commit()
        with pytest.raises(fx.FxUnavailable):
            fx.get_rate("USD", "EUR")
        with Session(mem_engine) as s:
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.7, rate_date=None,
                               fetched_at=datetime(2024, 1, 1)))
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.75, rate_date=None,
                               fetched_at=datetime(2024, 6, 1)))
            s.commit()
        assert fx.get_rate("USD", "EUR") == 0.75  # newest fetched_at

    def test_does_not_commit_caller_session(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_ok(0.9))
        with Session(mem_engine) as caller:
            pending = ExchangeRate(from_currency="XXX", to_currency="EUR", rate=1.23, rate_date=None)
            caller.add(pending)  # half-processed work, not committed
            assert fx.get_rate("USD", "EUR", session=caller) == 0.9
            assert pending in caller.new  # still pending: not flushed, not committed
            # The cache row was written through a separate session...
            with Session(mem_engine) as other:
                usd = other.exec(select(ExchangeRate).where(ExchangeRate.from_currency == "USD")).all()
            assert len(usd) == 1
            # ...and the caller's work can still be rolled back (a commit would have persisted it)
            caller.rollback()
        with Session(mem_engine) as s:
            assert s.exec(select(ExchangeRate).where(ExchangeRate.from_currency == "XXX")).all() == []


class TestGetRateOn:
    def test_db_exact_hit_skips_api(self, mem_engine, monkeypatch):
        with Session(mem_engine) as s:
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.66, rate_date=date(2024, 3, 2)))
            s.commit()
        monkeypatch.setattr(fx.requests, "get", _api_down)
        assert fx.get_rate_on("USD", date(2024, 3, 2), "EUR") == 0.66

    def test_api_stores_under_requested_date(self, mem_engine, monkeypatch):
        # Saturday: frankfurter answers with Friday's date
        fake = _api_ok(0.91, day="2024-03-01")
        monkeypatch.setattr(fx.requests, "get", fake)
        assert fx.get_rate_on("USD", date(2024, 3, 2), "EUR") == 0.91
        with Session(mem_engine) as s:
            rows = s.exec(select(ExchangeRate)).all()
        assert len(rows) == 1
        assert rows[0].rate_date == date(2024, 3, 2)
        # second call hits the DB, no API call
        monkeypatch.setattr(fx.requests, "get", _api_down)
        assert fx.get_rate_on("USD", date(2024, 3, 2), "EUR") == 0.91
        assert len(fake.calls) == 1

    def test_nearest_previous_row(self, mem_engine, monkeypatch):
        with Session(mem_engine) as s:
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.60, rate_date=date(2024, 1, 1)))
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.62, rate_date=date(2024, 2, 1)))
            s.add(ExchangeRate(from_currency="USD", to_currency="EUR", rate=0.99, rate_date=date(2024, 4, 1)))
            s.commit()
        monkeypatch.setattr(fx.requests, "get", _api_down)
        assert fx.get_rate_on("USD", date(2024, 3, 15), "EUR") == 0.62

    def test_falls_back_to_current_rate(self, mem_engine, monkeypatch):
        fx._CACHE["USD/EUR"] = (0.85, datetime.utcnow())
        monkeypatch.setattr(fx.requests, "get", _api_down)
        assert fx.get_rate_on("USD", date(2024, 3, 15), "EUR") == 0.85

    def test_raises_when_nothing_available(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_down)
        with pytest.raises(fx.FxUnavailable):
            fx.get_rate_on("USD", date(2024, 3, 15), "EUR")
        with pytest.raises(fx.FxUnavailable):
            fx.convert_on(5.0, "USD", date(2024, 3, 15), "EUR")

    def test_does_not_commit_caller_session(self, mem_engine, monkeypatch):
        monkeypatch.setattr(fx.requests, "get", _api_ok(0.9))
        with Session(mem_engine) as caller:
            pending = ExchangeRate(from_currency="YYY", to_currency="EUR", rate=1.0, rate_date=None)
            caller.add(pending)
            # get_rate_on reads the DB through the caller's session before calling the API:
            # that read must not flush (nor commit) the caller's pending work.
            assert fx.get_rate_on("USD", date(2024, 3, 2), "EUR", session=caller) == 0.9
            assert pending in caller.new
            with Session(mem_engine) as other:
                assert other.exec(select(ExchangeRate).where(ExchangeRate.from_currency == "YYY")).all() == []
                assert len(other.exec(select(ExchangeRate).where(ExchangeRate.from_currency == "USD")).all()) == 1
            caller.rollback()
        with Session(mem_engine) as s:
            assert s.exec(select(ExchangeRate).where(ExchangeRate.from_currency == "YYY")).all() == []
