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
import fx as _fx

log = logging.getLogger(__name__)

API_ORIGIN = "https://api.enablebanking.com"


def _format_sync_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        try:
            body = e.response.json()
            code = body.get("code", e.response.status_code)
            error = body.get("error", "")
            msg = body.get("message", "") or str(body.get("detail", ""))
            return f"{code} {error}: {msg}".strip(": ")
        except Exception:
            return f"{e.response.status_code}: {e.response.text[:200]}"
    return str(e)[:300]


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
    last_tx_date = session.exec(
        select(Transaction.date).where(Transaction.account_id == account.id).order_by(Transaction.date.desc())
    ).first()
    date_from = last_tx_date or (date.today() - timedelta(days=90))

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

        # Pre-fetch external_ids for this account only (unique constraint is per-account)
        existing_ids: set[str] = set(session.exec(
            select(Transaction.external_id).where(Transaction.account_id == account.id)
        ).all())
        seen_ids: set[str] = set()

        def _remittance_str(raw_tx: dict) -> str:
            ri = raw_tx.get("remittance_information")
            if isinstance(ri, list):
                return ri[0] if ri else ""
            return ri or ""

        def _rem_time(rem: str) -> str | None:
            # Extract HH:MM from "alle ore HH:MM[:SS]" (ING card remittance)
            import re as _re
            m = _re.search(r'alle ore (\d{2}:\d{2})', rem)
            return m.group(1) if m else None

        # Build PDNG merge maps keyed by (date, amount, HH:MM) and fallback (date, amount)
        # Bank APIs sometimes report PDNG time in UTC and BOOK time in local (±2h drift).
        pdng_map: dict[tuple, Transaction] = {}
        pdng_map_date_amount: dict[tuple, list] = {}
        for existing_tx in session.exec(
            select(Transaction).where(Transaction.account_id == account.id)
        ).all():
            if existing_tx.raw_data:
                try:
                    raw = json.loads(existing_tx.raw_data)
                    if raw.get("status") == "PDNG":
                        t = _rem_time(_remittance_str(raw))
                        if t:
                            pdng_map[(existing_tx.date, existing_tx.amount, t)] = existing_tx
                        da_key = (existing_tx.date, existing_tx.amount)
                        pdng_map_date_amount.setdefault(da_key, []).append(existing_tx)
                except Exception:
                    pass

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
                tx_date = parsed["date"]
                if tx_date is None:
                    tx_date_str = tx.get("transaction_date") or tx.get("booking_date") or tx.get("value_date")
                    tx_date = date.fromisoformat(tx_date_str) if tx_date_str else date.today()

                # PDNG resolution: match on (date, amount, HH:MM from remittance).
                # Fallback to (date, amount) alone when times differ due to UTC/local drift.
                tx_status = tx.get("status")
                rem_str = _remittance_str(tx)
                t = _rem_time(rem_str)
                matched_pdng: Transaction | None = None
                if t:
                    merge_key = (tx_date, amount, t)
                    if merge_key in pdng_map:
                        matched_pdng = pdng_map.pop(merge_key)
                        da_key = (matched_pdng.date, matched_pdng.amount)
                        pdng_map_date_amount.get(da_key, []).remove(matched_pdng) if matched_pdng in pdng_map_date_amount.get(da_key, []) else None
                if matched_pdng is None:
                    # Fallback: unique PDNG on same date+amount (covers UTC/local time drift)
                    da_key = (tx_date, amount)
                    candidates = pdng_map_date_amount.get(da_key, [])
                    if len(candidates) == 1:
                        matched_pdng = candidates.pop(0)
                        t_key = _rem_time(_remittance_str(json.loads(matched_pdng.raw_data or "{}")))
                        if t_key:
                            pdng_map.pop((matched_pdng.date, matched_pdng.amount, t_key), None)
                if matched_pdng is not None:
                    if tx_status == "BOOK":
                        # Upgrade PDNG→BOOK in place, keep category/share set by user
                        matched_pdng.external_id = tx_id
                        matched_pdng.raw_data = json.dumps(tx)
                        matched_pdng.merchant = merchant
                        matched_pdng.description = desc
                        matched_pdng.date = tx_date
                        matched_pdng.status = "BOOK"
                        existing_ids.add(tx_id)
                    elif tx_status == "PDNG":
                        # Still pending — refresh data, keep category/share set by user
                        matched_pdng.external_id = tx_id
                        matched_pdng.raw_data = json.dumps(tx)
                        matched_pdng.merchant = merchant
                        matched_pdng.description = desc
                        matched_pdng.date = tx_date
                        existing_ids.add(tx_id)
                    else:
                        # Cancelled/rejected: remove the pending transaction
                        session.delete(matched_pdng)
                    continue

                tx_currency = (tx.get("transaction_amount") or {}).get("currency") or account.currency or "EUR"
                cat_id = categorize(desc, merchant, session)
                eur_amount = None
                if tx_currency != "EUR":
                    try:
                        eur_amount = _fx.convert_on(amount, tx_currency, tx_date, session=session)
                    except Exception:
                        pass
                new_tx = Transaction(
                    account_id=account.id,
                    external_id=tx_id,
                    date=tx_date,
                    amount=amount,
                    currency=tx_currency,
                    description=desc,
                    merchant=merchant,
                    category_id=cat_id,
                    eur_amount=eur_amount,
                    raw_data=json.dumps(tx),
                    status=tx_status or "BOOK",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),
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
        account.sync_error = None
        session.commit()
        log.info(f"Synced {account.bank_name} / {account.name}")
    except Exception as e:
        log.error(f"Sync failed for {account.bank_name} / {account.name}: {e}")
        session.rollback()
        try:
            account.sync_error = _format_sync_error(e)
            session.commit()
        except Exception:
            pass


def sync_all():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with Session(engine) as session:
        account_ids = [a.id for a in session.exec(select(Account).where(Account.connected == True)).all()]

    def _sync_one(acc_id):
        with Session(engine) as session:
            acc = session.get(Account, acc_id)
            if acc:
                sync_account(acc, session)

    with ThreadPoolExecutor(max_workers=len(account_ids) or 1) as pool:
        futures = {pool.submit(_sync_one, acc_id): acc_id for acc_id in account_ids}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                log.error(f"sync_all: account {futures[fut]} raised {exc}")
