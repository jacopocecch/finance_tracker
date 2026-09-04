"""Route/helper tests for main.py on an in-memory SQLite DB.

The TestClient is deliberately NOT used as a context manager: that would run
the startup hook (init_db on the production DB + scheduler)."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import main
from database import (
    Account, BalanceSnapshot, Budget, Category, CategoryRule, MerchantCategory,
    Transaction, Trip, get_session,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    def _override():
        with Session(engine) as s:
            yield s

    main.app.dependency_overrides[get_session] = _override
    c = TestClient(main.app, follow_redirects=False)
    yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def cats(session):
    """Seed the categories the routes look up by name."""
    out = {}
    for name, type_ in [
        ("Altro", "expense"), ("Spesa", "expense"), ("Ristoranti", "expense"),
        ("Trasferimento", "transfer"), ("Prelievo ATM", "transfer"),
        ("Investimento", "investment"),
    ]:
        c = Category(name=name, type=type_)
        session.add(c)
        session.flush()
        out[name] = c.id
    session.commit()
    return out


@pytest.fixture
def account(session):
    acc = Account(bank_name="Manual", external_id="manual_1", name="Cash", type="checking",
                  currency="EUR", session_id="manual", connected=True)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


def _tx(session, account_id, amount, day=None, **kw):
    kw.setdefault("external_id", f"ext_{amount}_{day}_{kw.get('merchant')}_{id(kw)}")
    tx = Transaction(account_id=account_id, date=day or date.today(), amount=amount, **kw)
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _fake_request(path="/"):
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "headers": [], "query_string": b"", "scheme": "http",
             "server": ("test", 80), "client": ("test", 1), "root_path": ""}
    return Request(scope)


def _flash_qs(resp):
    """(msg, msg_type) from a redirect Location."""
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return q.get("msg", [""])[0], q.get("msg_type", ["info"])[0]


# ── 1. _effective_amount ─────────────────────────────────────────────────────

class TestEffectiveAmount:
    def test_non_eur_expense_with_share_keeps_sign(self):
        tx = SimpleNamespace(amount=-100.0, currency="USD", eur_amount=-90.0, personal_share=50.0)
        assert main._effective_amount(tx) == pytest.approx(-45.0)

    def test_non_eur_expense_without_share(self):
        tx = SimpleNamespace(amount=-100.0, currency="USD", eur_amount=-90.0, personal_share=None)
        assert main._effective_amount(tx) == -90.0

    def test_eur_expense_with_share(self):
        tx = SimpleNamespace(amount=-100.0, currency="EUR", eur_amount=None, personal_share=30.0)
        assert main._effective_amount(tx) == -30.0

    def test_fx_unavailable_falls_back_to_native(self, session, monkeypatch):
        def _boom(*a, **k):
            raise main.FxUnavailable("down")
        monkeypatch.setattr(main._fx, "convert_on", _boom)
        monkeypatch.setattr(main._fx, "convert", _boom)
        tx = SimpleNamespace(amount=-20.0, currency="GBP", eur_amount=None, personal_share=None, date=date.today())
        assert main._effective_amount(tx, session) == -20.0


# ── 5. Optional form coercion ────────────────────────────────────────────────

class TestOptCoercion:
    @pytest.mark.parametrize("v,exp", [(None, None), ("", None), ("  ", None), ("3", 3), ("3.0", 3), ("x", None)])
    def test_opt_int(self, v, exp):
        assert main._opt_int(v) == exp

    @pytest.mark.parametrize("v,exp", [(None, None), ("", None), ("1.5", 1.5), ("1,5", 1.5), ("abc", None)])
    def test_opt_float(self, v, exp):
        assert main._opt_float(v) == exp


class TestEmptyFormFields:
    def test_manual_tx_empty_category(self, client, session, cats, account):
        r = client.post("/transactions/new", data={
            "account_id": account.id, "tx_date": "2026-09-01", "amount": "-12.5",
            "description": "esselunga", "merchant": "", "category_id": "",
        })
        assert r.status_code == 303
        tx = session.exec(select(Transaction)).first()
        assert tx is not None and tx.category_id == cats["Altro"]  # no rules → Altro
        assert tx.currency == "EUR" and tx.eur_amount is None

    def test_manual_tx_non_eur_account_uses_account_currency(self, client, session, cats, monkeypatch):
        acc = Account(bank_name="M", external_id="m_usd", name="USD", currency="USD",
                      session_id="manual", connected=True)
        session.add(acc); session.commit(); session.refresh(acc)
        monkeypatch.setattr(main._fx, "convert_on", lambda amount, cur, d, session=None, **k: amount * 0.9)
        r = client.post("/transactions/new", data={
            "account_id": acc.id, "tx_date": "2026-09-01", "amount": "-10", "category_id": "",
        })
        assert r.status_code == 303
        tx = session.exec(select(Transaction)).first()
        assert tx.currency == "USD" and tx.eur_amount == pytest.approx(-9.0)

    def test_manual_tx_fx_unavailable_leaves_eur_amount_none(self, client, session, cats, monkeypatch):
        acc = Account(bank_name="M", external_id="m_chf", name="CHF", currency="CHF",
                      session_id="manual", connected=True)
        session.add(acc); session.commit(); session.refresh(acc)

        def _boom(*a, **k):
            raise main.FxUnavailable("down")
        monkeypatch.setattr(main._fx, "convert_on", _boom)
        r = client.post("/transactions/new", data={"account_id": acc.id, "tx_date": "2026-09-01", "amount": "-10"})
        assert r.status_code == 303
        tx = session.exec(select(Transaction)).first()
        assert tx.currency == "CHF" and tx.eur_amount is None

    def test_add_category_empty_macro(self, client, session, cats):
        r = client.post("/categories/add", data={"name": "Nuova", "macrocategory_id": ""})
        assert r.status_code == 303 and r.headers["location"] == "/categories"
        c = session.exec(select(Category).where(Category.name == "Nuova")).first()
        assert c is not None and c.macrocategory_id is None

    def test_assign_macro_empty(self, client, session, cats):
        r = client.post(f"/categories/{cats['Spesa']}/assign", data={"macrocategory_id": ""})
        assert r.status_code == 303

    def test_clear_share(self, client, session, cats, account):
        tx = _tx(session, account.id, -50.0, personal_share=20.0)
        r = client.post(f"/transactions/{tx.id}/share", data={"personal_share": ""})
        assert r.status_code == 303
        session.refresh(tx)
        assert tx.personal_share is None

    def test_set_share(self, client, session, cats, account):
        tx = _tx(session, account.id, -50.0)
        client.post(f"/transactions/{tx.id}/share", data={"personal_share": "12.5"})
        session.refresh(tx)
        assert tx.personal_share == 12.5

    def test_clear_threshold(self, client, session, account):
        account.balance_threshold = 100.0
        session.add(account); session.commit()
        r = client.post(f"/setup/account/{account.id}/threshold", data={"threshold": ""})
        assert r.status_code == 303
        session.refresh(account)
        assert account.balance_threshold is None

    def test_balance_empty_redirects_with_error(self, client, session, account):
        r = client.post(f"/setup/account/{account.id}/balance", data={"balance": ""})
        assert r.status_code == 303
        msg, typ = _flash_qs(r)
        assert typ == "error" and msg

    def test_balance_valid(self, client, session, account):
        r = client.post(f"/setup/account/{account.id}/balance", data={"balance": "42.5"})
        assert r.status_code == 303
        snap = session.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)).first()
        assert snap.balance == 42.5

    def test_manual_account_empty_initial_balance(self, client, session):
        r = client.post("/setup/account/manual", data={"name": "X", "bank_name": "", "initial_balance": ""})
        assert r.status_code == 303
        snap = session.exec(select(BalanceSnapshot)).first()
        assert snap.balance == 0.0

    def test_budget_empty_amount_redirects_with_error(self, client, session, cats):
        r = client.post("/budgets/save", data={"category_id": cats["Spesa"], "amount": ""})
        assert r.status_code == 303
        assert _flash_qs(r)[1] == "error"
        assert session.exec(select(Budget)).first() is None

    def test_budget_valid(self, client, session, cats):
        r = client.post("/budgets/save", data={"category_id": cats["Spesa"], "amount": "300"})
        assert r.status_code == 303 and r.headers["location"] == "/budgets"
        assert session.exec(select(Budget)).first().amount == 300.0


# ── 6. Duplicate names ───────────────────────────────────────────────────────

class TestDuplicates:
    def test_duplicate_category(self, client, cats):
        r = client.post("/categories/add", data={"name": "Spesa"})
        assert r.status_code == 303
        assert _flash_qs(r)[1] == "error"

    def test_duplicate_macro(self, client, session):
        assert client.post("/macros/add", data={"name": "Cibo"}).status_code == 303
        r = client.post("/macros/add", data={"name": "Cibo"})
        assert r.status_code == 303
        assert _flash_qs(r)[1] == "error"


# ── 4. delete_category cleans dependents ─────────────────────────────────────

def test_delete_category_removes_dependents(client, session, cats):
    cid = cats["Ristoranti"]
    session.add(MerchantCategory(merchant="pizzeria da mario", category_id=cid))
    session.add(Budget(category_id=cid, amount=100.0))
    session.add(CategoryRule(pattern="pizza", category_id=cid))
    session.commit()
    r = client.post(f"/categories/{cid}/delete")
    assert r.status_code == 303
    assert session.get(Category, cid) is None
    assert session.exec(select(MerchantCategory).where(MerchantCategory.category_id == cid)).first() is None
    assert session.exec(select(Budget).where(Budget.category_id == cid)).first() is None
    assert session.exec(select(CategoryRule).where(CategoryRule.category_id == cid)).first() is None


# ── 7. Trips ─────────────────────────────────────────────────────────────────

class TestTrips:
    def test_end_before_start_rejected(self, client, session):
        r = client.post("/trips/add", data={"name": "X", "start_date": "2026-09-10", "end_date": "2026-09-01"})
        assert r.status_code == 303 and r.headers["location"].startswith("/trips?")
        assert _flash_qs(r)[1] == "error"
        assert session.exec(select(Trip)).first() is None

    def test_update_end_before_start_rejected(self, client, session):
        t = Trip(name="T", start_date=date(2026, 9, 1), end_date=date(2026, 9, 3))
        session.add(t); session.commit(); session.refresh(t)
        r = client.post(f"/trips/{t.id}/update", data={"name": "T", "start_date": "2026-09-05", "end_date": "2026-09-01"})
        assert _flash_qs(r)[1] == "error"
        session.refresh(t)
        assert t.end_date == date(2026, 9, 3)

    def test_single_day_trip_detail_renders(self, client, session, cats, account):
        t = Trip(name="Day", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))
        session.add(t); session.commit(); session.refresh(t)
        _tx(session, account.id, -30.0, day=date(2026, 9, 1), trip_id=t.id)
        r = client.get(f"/trips/{t.id}")
        assert r.status_code == 200
        assert "1 giorni" in r.text

    def test_days_clamped(self, client, session):
        # Legacy row with end < start must not divide by zero.
        t = Trip(name="Bad", start_date=date(2026, 9, 5), end_date=date(2026, 9, 1))
        session.add(t); session.commit(); session.refresh(t)
        assert client.get(f"/trips/{t.id}").status_code == 200

    def test_trip_name_with_apostrophe_renders(self, client, session, cats, account):
        t = Trip(name="Sant'Antonio", start_date=date(2026, 9, 1), end_date=date(2026, 9, 2))
        session.add(t); session.commit(); session.refresh(t)
        _tx(session, account.id, -5.0, day=date(2026, 9, 1))  # candidate → assign-range confirm rendered
        r = client.get(f"/trips/{t.id}")
        assert r.status_code == 200
        assert "confirm(\"Assegnare 1 transazioni a \\u00abSant\\u0027Antonio\\u00bb?\")" in r.text


# ── 8. Flash on /transactions ────────────────────────────────────────────────

def test_transactions_flash_rendered_and_not_in_pagination(client, session, cats, account):
    for i in range(60):
        _tx(session, account.id, -1.0 - i, external_id=f"e{i}")
    import re
    r = client.get("/transactions", params={"msg": "Fatto bene", "msg_type": "error", "search": "x"})
    assert r.status_code == 200 and "Fatto bene" in r.text
    r2 = client.get("/transactions", params={"msg": "Fatto bene"})
    assert "Fatto bene" in r2.text
    links = re.findall(r'href="\?([^"]*)"', r2.text)
    assert links and all("page=" in l and "msg" not in l for l in links)


# ── 11/14. Merchant categories ───────────────────────────────────────────────

class TestMerchantCategory:
    def test_unicode_merchant_matches_all(self, client, session, cats, account):
        a = _tx(session, account.id, -5.0, merchant="CAFFÈ ROMA", external_id="a")
        b = _tx(session, account.id, -6.0, merchant="Caffè Roma", external_id="b")
        r = client.post(f"/transactions/{a.id}/merchant-category",
                        data={"category_id": cats["Ristoranti"]}, headers={"X-Fetch": "1"})
        assert r.status_code == 200 and r.json()["mapped"] is True
        session.refresh(a); session.refresh(b)
        assert a.category_id == cats["Ristoranti"] and b.category_id == cats["Ristoranti"]
        mc = session.exec(select(MerchantCategory)).first()
        assert mc.merchant == "caffè roma"

    def test_remove_mapping(self, client, session, cats, account):
        a = _tx(session, account.id, -5.0, merchant="Esselunga", external_id="a")
        session.add(MerchantCategory(merchant="esselunga", category_id=cats["Spesa"])); session.commit()
        r = client.post(f"/transactions/{a.id}/merchant-category",
                        data={"category_id": cats["Spesa"], "remove": "1"}, headers={"X-Fetch": "1"})
        assert r.status_code == 200 and r.json() == {"mapped": False}
        assert session.exec(select(MerchantCategory)).first() is None

    def test_missing_category_is_400(self, client, session, cats, account):
        a = _tx(session, account.id, -5.0, merchant="X", external_id="a")
        r = client.post(f"/transactions/{a.id}/merchant-category", data={"category_id": ""}, headers={"X-Fetch": "1"})
        assert r.status_code == 400

    def test_sync_unicode(self, client, session, cats, account):
        a = _tx(session, account.id, -5.0, merchant="CAFFÈ ROMA", external_id="a", category_id=cats["Altro"])
        session.add(MerchantCategory(merchant="caffè roma", category_id=cats["Ristoranti"])); session.commit()
        r = client.post("/merchant-categories/sync")
        assert r.status_code == 303 and "1+transazioni" in r.headers["location"]
        session.refresh(a)
        assert a.category_id == cats["Ristoranti"]


# ── 18. detect_transfers ─────────────────────────────────────────────────────

def test_detect_transfers_respects_currency_and_status(client, session, cats):
    a1 = Account(bank_name="A", external_id="a1", name="a", session_id="s", connected=True)
    a2 = Account(bank_name="B", external_id="a2", name="b", session_id="s", connected=True, currency="USD")
    a3 = Account(bank_name="C", external_id="a3", name="c", session_id="s", connected=True)
    session.add_all([a1, a2, a3]); session.commit()
    d = date(2026, 9, 1)
    out_eur = _tx(session, a1.id, -100.0, day=d, external_id="o1")
    in_usd = _tx(session, a2.id, 100.0, day=d, currency="USD", external_id="i1")     # different currency → no match
    in_pdng = _tx(session, a3.id, 100.0, day=d, status="PDNG", external_id="i2")     # pending → skipped
    r = client.post("/transactions/detect-transfers")
    assert "0+trasferimenti" in r.headers["location"]
    in_eur = _tx(session, a3.id, 100.0, day=d, external_id="i3")
    r = client.post("/transactions/detect-transfers")
    assert "1+trasferimenti" in r.headers["location"]
    session.refresh(out_eur); session.refresh(in_eur); session.refresh(in_usd); session.refresh(in_pdng)
    assert out_eur.transfer_partner_id == in_eur.id and in_eur.transfer_partner_id == out_eur.id
    assert in_usd.transfer_partner_id is None and in_pdng.transfer_partner_id is None


# ── 19. detect_prelievi ──────────────────────────────────────────────────────

def test_detect_prelievi_only_recent_withdrawals_adjust_balance(client, session, cats):
    bank = Account(bank_name="A", external_id="b", name="bank", session_id="s", connected=True)
    cash = Account(bank_name="Cash", external_id="c", name="cash", type="cash", session_id="manual", connected=True)
    session.add_all([bank, cash]); session.commit()
    today = date.today()
    session.add(BalanceSnapshot(account_id=cash.id, date=today - timedelta(days=10), balance=50.0))
    session.commit()
    old = _tx(session, bank.id, -100.0, day=today - timedelta(days=30), category_id=cats["Prelievo ATM"], external_id="old")
    new = _tx(session, bank.id, -20.0, day=today - timedelta(days=2), category_id=cats["Prelievo ATM"], external_id="new")
    r = client.post("/transactions/detect-prelievi", data={"cash_account_id": cash.id})
    assert "2+prelievi" in r.headers["location"]
    session.refresh(old); session.refresh(new)
    assert old.transfer_partner_id and new.transfer_partner_id      # both linked
    assert old.category_id == cats["Trasferimento"]
    latest = session.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == cash.id)
                          .order_by(BalanceSnapshot.date.desc())).first()
    assert latest.date == today and latest.balance == pytest.approx(70.0)  # only the recent 20


# ── 16. /sync without HTMX ───────────────────────────────────────────────────

def test_sync_plain_form_post_redirects(client, engine, monkeypatch):
    monkeypatch.setattr(main, "sync_all", lambda: None)
    monkeypatch.setattr(main, "engine", engine)
    r = client.post("/sync")
    assert r.status_code == 303 and r.headers["location"].startswith("/setup?msg=")
    r = client.post("/sync", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Sync completato" in r.text


# ── 2. Monthly weekly buckets ────────────────────────────────────────────────

def test_monthly_weeks_cover_whole_month(client, session, cats, account):
    _tx(session, account.id, -10.0, day=date(2026, 8, 31), external_id="last")
    r = client.get("/monthly", params={"month": "2026-08"})
    assert r.status_code == 200
    assert '"Sett 5"' in r.text and "10.0" in r.text
    r = client.get("/monthly", params={"month": "2026-02"})
    assert '"Sett 4"' in r.text and '"Sett 5"' not in r.text


# ── 12. Net worth series: disconnected accounts stop after last snapshot ─────

def test_networth_series_disconnected_stops(session, monkeypatch):
    today = date.today()
    monkeypatch.setattr(main, "CHART_START_DATE", today - timedelta(days=3))
    live = Account(bank_name="A", external_id="l", name="l", session_id="s", connected=True)
    dead = Account(bank_name="B", external_id="d", name="d", session_id="s", connected=False)
    session.add_all([live, dead]); session.commit()
    session.add(BalanceSnapshot(account_id=live.id, date=today - timedelta(days=3), balance=100.0))
    session.add(BalanceSnapshot(account_id=dead.id, date=today - timedelta(days=3), balance=50.0))
    session.add(BalanceSnapshot(account_id=dead.id, date=today - timedelta(days=2), balance=50.0))
    session.commit()
    labels, values = main._networth_series(session)
    assert values == [150.0, 150.0, 100.0, 100.0]


# ── 13. Dashboard P&L from the non-liquidity basket ──────────────────────────

def test_dashboard_pl_excludes_liquidity_etf(session, cats, monkeypatch):
    from database import Instrument, InvestmentTransaction, MarketQuote
    # Capture the template context instead of rendering with a synthetic request.
    monkeypatch.setattr(main.templates, "TemplateResponse",
                        lambda name, ctx, **k: SimpleNamespace(context=ctx))
    inv = Instrument(name="World", isin="I1", ticker="W.MI", currency="EUR")
    liq = Instrument(name="Cash ETF", isin="I2", ticker="C.MI", currency="EUR", is_liquidity=True)
    session.add_all([inv, liq]); session.commit()
    session.add(InvestmentTransaction(instrument_id=inv.id, trade_date=date(2026, 1, 1), quantity=10, unit_price=100.0))
    session.add(InvestmentTransaction(instrument_id=liq.id, trade_date=date(2026, 1, 1), quantity=10, unit_price=100.0))
    session.add(MarketQuote(instrument_id=inv.id, price=110.0, currency="EUR", quote_timestamp=datetime(2026, 9, 1)))
    session.add(MarketQuote(instrument_id=liq.id, price=101.0, currency="EUR", quote_timestamp=datetime(2026, 9, 1)))
    session.commit()
    resp = main.dashboard(_fake_request("/"), session)
    ctx = resp.context
    assert ctx["portfolio_value"] == pytest.approx(1100.0)
    assert ctx["liquidity_etf"] == pytest.approx(1010.0)
    assert ctx["portfolio_pl"] == pytest.approx(100.0)
    assert ctx["portfolio_pl_pct"] == pytest.approx(10.0)


# ── 3. Fresh-install rules ───────────────────────────────────────────────────

def test_init_db_fresh_install_rules(tmp_path, monkeypatch):
    import database
    from categorizer import categorize
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr(database, "engine", eng)
    database.init_db()
    with Session(eng) as s:
        by_name = {c.name: c.id for c in s.exec(select(Category)).all()}
        assert "Pasticceria/Gelateria" in by_name
        assert s.exec(select(CategoryRule)).all()  # rules were inserted without KeyError
        assert categorize("esselunga s.p.a. data operazione 01/09/26", None, s) == by_name["Spesa"]
        assert categorize("pagamento pos gasparini srl", None, s) == by_name["Altro"]
        assert categorize("bolletta gas settembre", None, s) == by_name["Casa"]
        assert categorize("acquisto etf vwce", None, s) == by_name["Investimento"]
        assert categorize("sottoscrizione azioni", None, s) == by_name["Investimento"]
        assert categorize("atm milano abbonamento", None, s) == by_name["Trasporti"]
        assert categorize("pasticceria rossi", None, s) == by_name["Pasticceria/Gelateria"]


# ── Render check ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/transactions", "/categories", "/setup", "/trips", "/monthly", "/yearly", "/budgets"])
def test_pages_render(client, session, cats, account, path):
    session.add(BalanceSnapshot(account_id=account.id, date=date.today(), balance=10.0))
    session.add(MerchantCategory(merchant="bar dell'angolo", category_id=cats["Ristoranti"]))
    session.add(Category(name="Sant'Anna", type="expense"))
    session.commit()
    _tx(session, account.id, -3.0, merchant="Bar dell'angolo", external_id="r1")
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "confirm('" not in r.text.replace("confirm('Eliminare questa transazione?')", "") \
        .replace("confirm('Ricategorizzare tutte le transazioni con le regole attuali?')", "") \
        .replace("confirm('Eliminare il viaggio? Le transazioni non vengono cancellate, solo scollegate.')", "")
