import json
from datetime import date, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import sync
from database import Account, BalanceSnapshot, Category, Transaction

TODAY = date.today()


def _d(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


# --------------------------------------------------------------------------
# Fixtures: in-memory DB + canned Enable Banking responses
# --------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Category(name="Altro", type="expense"))
        s.add(Category(name="Spesa", type="expense"))
        s.commit()
        yield s


@pytest.fixture
def account(session):
    acc = Account(
        bank_name="Revolut",
        external_id="acc-uid-1",
        name="Revolut EUR",
        session_id="sess-1",
        connected=True,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sync.requests.HTTPError(f"{self.status_code}", response=None)

    def json(self):
        return self._payload


class FakeBank:
    """Stands in for `requests.get`; records the query params it received."""

    def __init__(self, transactions=None, balances=None, fail=False):
        self.transactions = list(transactions or [])
        self.balances = balances if balances is not None else [
            {"balance_type": "CLBD", "balance_amount": {"amount": "100.00", "currency": "EUR"}}
        ]
        self.fail = fail
        self.tx_params: list[dict] = []

    def __call__(self, url, params=None, headers=None):
        if self.fail:
            return _Resp({"code": 500, "message": "boom"}, status=500)
        if url.endswith("/transactions"):
            self.tx_params.append(dict(params or {}))
            return _Resp({"transactions": self.transactions, "continuation_key": None})
        if url.endswith("/balances"):
            return _Resp({"balances": self.balances})
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def bank(monkeypatch):
    fake = FakeBank()
    monkeypatch.setattr(sync.requests, "get", fake)
    monkeypatch.setattr(sync, "_headers", lambda: {})
    return fake


def _api_tx(ref, amount, when: date, status="BOOK", remittance="Coffee", currency="EUR", debit=True):
    tx = {
        "transaction_amount": {"amount": f"{abs(amount):.2f}", "currency": currency},
        "credit_debit_indicator": "DBIT" if debit else "CRDT",
        "status": status,
        "booking_date": when.isoformat(),
        "value_date": when.isoformat(),
        "remittance_information": [remittance],
        "bank_transaction_code": {"code": "CARD_PAYMENT"},
    }
    if ref is not None:
        tx["entry_reference"] = ref
    return tx


def _db_tx(session, account, ext, amount, when: date, status="BOOK", raw=None, **kw):
    tx = Transaction(
        account_id=account.id,
        external_id=ext,
        date=when,
        amount=amount,
        description=kw.pop("description", "old"),
        merchant=kw.pop("merchant", "OLD"),
        status=status,
        raw_data=json.dumps(raw) if raw is not None else json.dumps({"status": status}),
        **kw,
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _rows(session, account):
    return session.exec(
        select(Transaction).where(Transaction.account_id == account.id).order_by(Transaction.id)
    ).all()


# --------------------------------------------------------------------------
# Item 1: fetch window
# --------------------------------------------------------------------------

def test_date_from_no_rows_is_90_days(session, account):
    assert sync._compute_date_from(account, session, TODAY) == _d(90)


def test_date_from_uses_last_synced_row_minus_overlap(session, account):
    _db_tx(session, account, "a", -1.0, _d(20))
    _db_tx(session, account, "b", -1.0, _d(3))
    assert sync._compute_date_from(account, session, TODAY) == _d(3 + sync.SYNC_OVERLAP_DAYS)


def test_date_from_ignores_manual_rows(session, account):
    _db_tx(session, account, "a", -1.0, _d(10))
    # A manually-entered future row must not push the window into the future
    _db_tx(session, account, "manual_deadbeef", -5.0, TODAY + timedelta(days=30), raw="")
    assert sync._compute_date_from(account, session, TODAY) == _d(10 + sync.SYNC_OVERLAP_DAYS)


def test_date_from_only_manual_rows_counts_as_no_rows(session, account):
    _db_tx(session, account, "manual_1", -5.0, _d(2), raw="")
    assert sync._compute_date_from(account, session, TODAY) == _d(90)


def test_date_from_clamps_future_synced_rows_to_today(session, account):
    _db_tx(session, account, "a", -1.0, TODAY + timedelta(days=5))
    assert sync._compute_date_from(account, session, TODAY) == _d(sync.SYNC_OVERLAP_DAYS)


def test_date_from_widens_to_oldest_pending_row(session, account):
    _db_tx(session, account, "a", -1.0, _d(1))
    _db_tx(session, account, "p-old", -9.0, _d(40), status="PDNG")
    _db_tx(session, account, "p-new", -9.0, _d(2), status="PDNG")
    assert sync._compute_date_from(account, session, TODAY) == _d(40)


def test_sync_sends_computed_date_from(session, account, bank):
    _db_tx(session, account, "a", -1.0, _d(2))
    sync.sync_account(account, session)
    assert bank.tx_params[0]["date_from"] == _d(2 + sync.SYNC_OVERLAP_DAYS).isoformat()
    assert account.sync_error is None
    assert account.last_sync is not None


# --------------------------------------------------------------------------
# Item 2: vanished PDNG rows are dropped (only when old enough)
# --------------------------------------------------------------------------

def test_vanished_old_pdng_is_deleted(session, account, bank):
    gone = _db_tx(session, account, "p-gone", -9.0, _d(10), status="PDNG")
    kept = _db_tx(session, account, "b-book", -3.0, _d(5))
    bank.transactions = [_api_tx("b-book", -3.0, _d(5))]

    sync.sync_account(account, session)

    ids = {t.external_id for t in _rows(session, account)}
    assert "p-gone" not in ids
    assert "b-book" in ids
    assert session.get(Transaction, gone.id) is None
    assert session.get(Transaction, kept.id) is not None


def test_fresh_vanished_pdng_is_kept(session, account, bank):
    _db_tx(session, account, "p-fresh", -9.0, _d(sync.PDNG_STALE_DAYS - 1), status="PDNG")
    bank.transactions = []

    sync.sync_account(account, session)

    assert {t.external_id for t in _rows(session, account)} == {"p-fresh"}


def test_pdng_still_reported_is_kept(session, account, bank):
    _db_tx(session, account, "p-1", -9.0, _d(10), status="PDNG")
    bank.transactions = [_api_tx("p-1", -9.0, _d(10), status="PDNG")]

    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert [(t.external_id, t.status) for t in rows] == [("p-1", "PDNG")]


def test_pdng_matched_under_new_reference_is_not_deleted(session, account, bank):
    # PDNG stored with ref "p-1"; bank books it under "b-1" with same date/amount
    pd = _db_tx(
        session, account, "p-1", -9.0, _d(10), status="PDNG",
        raw={"status": "PDNG", "remittance_information": ["Shop"]},
        category_id=2,
    )
    bank.transactions = [_api_tx("b-1", -9.0, _d(10), status="BOOK", remittance="Shop")]

    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert len(rows) == 1
    assert rows[0].id == pd.id
    assert rows[0].external_id == "b-1"
    assert rows[0].status == "BOOK"
    assert rows[0].category_id == 2


def test_manual_pdng_rows_are_never_deleted(session, account, bank):
    _db_tx(session, account, "manual_abc", -9.0, _d(10), status="PDNG", raw="")
    _db_tx(session, account, "a", -1.0, _d(4))
    bank.transactions = [_api_tx("a", -1.0, _d(4))]

    sync.sync_account(account, session)

    assert "manual_abc" in {t.external_id for t in _rows(session, account)}


def test_nothing_deleted_when_fetch_fails(session, account, bank):
    _db_tx(session, account, "p-gone", -9.0, _d(10), status="PDNG")
    bank.fail = True

    sync.sync_account(account, session)

    assert {t.external_id for t in _rows(session, account)} == {"p-gone"}
    assert account.sync_error


# --------------------------------------------------------------------------
# Item 3: same entry_reference, PDNG -> BOOK upgraded in place
# --------------------------------------------------------------------------

def test_same_reference_pdng_to_book_upgrades_in_place(session, account, bank):
    pd = _db_tx(
        session, account, "ref-1", -9.0, _d(6), status="PDNG",
        raw={"status": "PDNG", "remittance_information": ["Pending shop"]},
        category_id=2, personal_share=4.5,
    )
    bank.transactions = [_api_tx("ref-1", -9.0, _d(5), status="BOOK", remittance="Booked shop")]

    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == pd.id
    assert row.status == "BOOK"
    assert row.date == _d(5)
    assert row.merchant == "Booked shop"
    assert row.description == "Booked shop"
    assert json.loads(row.raw_data)["status"] == "BOOK"
    # user-set fields survive
    assert row.category_id == 2
    assert row.personal_share == 4.5


def test_same_reference_still_pending_is_left_alone(session, account, bank):
    pd = _db_tx(
        session, account, "ref-1", -9.0, _d(6), status="PDNG",
        raw={"status": "PDNG", "remittance_information": ["Pending shop"]},
    )
    bank.transactions = [_api_tx("ref-1", -9.0, _d(6), status="PDNG", remittance="Pending shop")]

    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert len(rows) == 1 and rows[0].id == pd.id and rows[0].status == "PDNG"


def test_existing_book_row_is_skipped(session, account, bank):
    _db_tx(session, account, "ref-1", -9.0, _d(6), merchant="KEEP")
    bank.transactions = [_api_tx("ref-1", -9.0, _d(6), remittance="Changed")]

    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert len(rows) == 1 and rows[0].merchant == "KEEP"


# --------------------------------------------------------------------------
# Item 6: deterministic fallback id
# --------------------------------------------------------------------------

def test_fallback_id_is_deterministic():
    tx = _api_tx(None, -12.5, _d(1), remittance="No ref")
    a = sync._fallback_id(tx, -12.5, "EUR")
    b = sync._fallback_id(dict(tx), -12.5, "EUR")
    assert a == b
    assert a.startswith("h_") and len(a) == 2 + 24
    assert sync._fallback_id(tx, -12.5, "EUR") != sync._fallback_id(tx, -12.0, "EUR")
    other = _api_tx(None, -12.5, _d(1), remittance="Other")
    assert sync._fallback_id(other, -12.5, "EUR") != a


def test_transactions_without_reference_do_not_duplicate_on_resync(session, account, bank):
    bank.transactions = [
        _api_tx(None, -12.5, _d(1), remittance="No ref A"),
        _api_tx(None, -12.5, _d(1), remittance="No ref B"),
    ]

    sync.sync_account(account, session)
    sync.sync_account(account, session)

    rows = _rows(session, account)
    assert len(rows) == 2
    assert all(t.external_id.startswith("h_") for t in rows)
    assert len({t.external_id for t in rows}) == 2


# --------------------------------------------------------------------------
# Item 4: balance_type preference (cheap to cover alongside)
# --------------------------------------------------------------------------

def test_balance_prefers_clbd(session, account, bank):
    bank.balances = [
        {"balance_type": "XPCD", "balance_amount": {"amount": "10.00", "currency": "EUR"}},
        {"balance_type": "CLBD", "balance_amount": {"amount": "20.00", "currency": "EUR"}},
    ]
    sync.sync_account(account, session)
    snap = session.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)).one()
    assert snap.balance == 20.0


def test_balance_single_entry_unchanged(session, account, bank):
    bank.balances = [{"balance_type": "OTHR", "balance_amount": {"amount": "7.50", "currency": "EUR"}}]
    sync.sync_account(account, session)
    snap = session.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)).one()
    assert snap.balance == 7.5
