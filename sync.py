import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import requests
import jwt as pyjwt
from sqlmodel import Session, select

from database import Account, Transaction, BalanceSnapshot, engine
from categorizer import categorize
from parsers import parse_transaction
import config

log = logging.getLogger(__name__)

API_ORIGIN = "https://api.enablebanking.com"


def _make_jwt() -> str:
    iat = int(datetime.now().timestamp())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": iat,
        "exp": iat + 3600,
    }
    private_key = config.PRIVATE_KEY_PATH.read_bytes()
    return pyjwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": config.APPLICATION_ID},
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {_make_jwt()}"}


def build_auth_url(bank_name: str, country: str = "IT") -> str:
    body = {
        "access": {
            "valid_until": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        },
        "aspsp": {"name": bank_name, "country": country},
        "state": bank_name,
        "redirect_url": config.REDIRECT_URI,
        "psu_type": "personal",
    }
    r = requests.post(f"{API_ORIGIN}/auth", json=body, headers=_headers())
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()["url"]


def handle_callback(code: str, state: str) -> list[int]:
    r = requests.post(
        f"{API_ORIGIN}/sessions",
        json={"code": code},
        headers=_headers(),
    )
    r.raise_for_status()
    session_data = r.json()
    session_id = session_data["session_id"]

    with Session(engine) as db:
        saved = []
        for acc in session_data.get("accounts", []):
            uid = acc["uid"]
            existing = db.exec(
                select(Account).where(Account.external_id == uid)
            ).first()
            if existing:
                db_acc = existing
            else:
                account_id_obj = acc.get("account_id") or {}
                currency = acc.get("currency") or "EUR"
                auto_display = f"{state} {currency}" if currency != "EUR" else None
                db_acc = Account(
                    bank_name=state,
                    external_id=uid,
                    name=acc.get("name") or uid,
                    display_name=auto_display,
                    iban=account_id_obj.get("iban") if isinstance(account_id_obj, dict) else None,
                    type=_classify_account_type(acc.get("name") or ""),
                    currency=currency,
                )
                db.add(db_acc)
            db_acc.session_id = session_id
            db_acc.connected = True
            db.flush()
            saved.append(db_acc.id)
        db.commit()
        return saved


def _classify_account_type(name: str) -> str:
    name_lower = name.lower()
    for kw in config.INVESTMENT_ACCOUNT_KEYWORDS:
        if kw in name_lower:
            return "investment"
    return "checking"


def sync_account(account: Account, session: Session):
    headers = _headers()
    uid = account.external_id
    date_from = (account.last_sync or (datetime.now() - timedelta(days=90))).date()

    try:
        # Fetch transactions (paginated)
        query: dict = {"date_from": date_from.isoformat()}
        continuation_key = None
        all_txs = []
        while True:
            if continuation_key:
                query["continuation_key"] = continuation_key
            r = requests.get(
                f"{API_ORIGIN}/accounts/{uid}/transactions",
                params=query,
                headers=headers,
            )
            r.raise_for_status()
            resp = r.json()
            all_txs.extend(resp.get("transactions", []))
            continuation_key = resp.get("continuation_key")
            if not continuation_key:
                break

        # Pre-fetch all known external_ids globally (unique constraint is global)
        existing_ids: set[str] = set(session.exec(select(Transaction.external_id)).all())
        seen_ids: set[str] = set()

        # no_autoflush covers both the insert loop AND the balance query so pending
        # objects are never flushed implicitly mid-function
        with session.no_autoflush:
            for tx in all_txs:
                tx_id = tx.get("entry_reference") or tx.get("transaction_id") or str(uuid.uuid4())
                if tx_id in existing_ids or tx_id in seen_ids:
                    continue
                seen_ids.add(tx_id)
                amount = float(tx["transaction_amount"]["amount"])
                if tx.get("credit_debit_indicator") == "DBIT":
                    amount = -abs(amount)
                else:
                    amount = abs(amount)
                parsed = parse_transaction(tx, account.bank_name)
                merchant = parsed["merchant"]
                desc = parsed["description"]
                tx_date_str = tx.get("transaction_date") or tx.get("booking_date") or tx.get("value_date")
                tx_date = date.fromisoformat(tx_date_str) if tx_date_str else date.today()
                tx_currency = (tx.get("transaction_amount") or {}).get("currency") or account.currency or "EUR"
                cat_id = categorize(desc, merchant, session)
                new_tx = Transaction(
                    account_id=account.id,
                    external_id=tx_id,
                    date=tx_date,
                    amount=amount,
                    currency=tx_currency,
                    description=desc,
                    merchant=merchant,
                    category_id=cat_id,
                    raw_data=json.dumps(tx),
                )
                session.add(new_tx)

            # Fetch balance inside no_autoflush so pending tx don't trigger a flush
            r = requests.get(f"{API_ORIGIN}/accounts/{uid}/balances", headers=headers)
            r.raise_for_status()
            balances = r.json().get("balances", [])
            if balances:
                bal_amount = float(balances[0]["balance_amount"]["amount"])
                snap = session.exec(
                    select(BalanceSnapshot).where(
                        BalanceSnapshot.account_id == account.id,
                        BalanceSnapshot.date == date.today(),
                    )
                ).first()
                if snap:
                    snap.balance = bal_amount
                else:
                    session.add(BalanceSnapshot(
                        account_id=account.id, date=date.today(), balance=bal_amount
                    ))

        account.last_sync = datetime.now(timezone.utc)
        session.commit()
        log.info(f"Synced {account.bank_name} / {account.name}")
    except Exception as e:
        log.error(f"Sync failed for {account.bank_name} / {account.name}: {e}")
        session.rollback()


def sync_all():
    with Session(engine) as session:
        account_ids = [a.id for a in session.exec(select(Account).where(Account.connected == True)).all()]
    for acc_id in account_ids:
        with Session(engine) as session:
            acc = session.get(Account, acc_id)
            if acc:
                sync_account(acc, session)
