import hashlib
import json
import logging
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
        claimed_ids: set[int] = set()
        for acc in session_data.get("accounts", []):
            uid = acc["uid"]
            ident_hash = acc.get("identification_hash")
            account_id_obj = acc.get("account_id") or {}
            iban = account_id_obj.get("iban") if isinstance(account_id_obj, dict) else None
            currency = acc.get("currency") or "EUR"

            # Enable Banking issues a new uid per session, so match on stable
            # keys too: identification_hash first, then bank+IBAN+currency
            # (currency disambiguates Revolut multi-currency pockets that
            # share one IBAN).
            existing = db.exec(
                select(Account).where(Account.external_id == uid)
            ).first()
            if not existing and ident_hash:
                existing = db.exec(
                    select(Account).where(
                        Account.identification_hash == ident_hash,
                        Account.deleted == False,
                    )
                ).first()
            if not existing and iban:
                existing = db.exec(
                    select(Account).where(
                        Account.bank_name == state,
                        Account.iban == iban,
                        Account.currency == currency,
                        Account.deleted == False,
                    )
                ).first()
            name_match = False
            if not existing and not iban:
                # Banks without IBAN (e.g. PayPal): match by bank name +
                # currency, but only when unambiguous — exactly one active,
                # non-manual IBAN-less account not already claimed in this
                # session and whose stored hash (if any) agrees.
                candidates = [
                    a
                    for a in db.exec(
                        select(Account).where(
                            Account.bank_name == state,
                            Account.iban == None,
                            Account.currency == currency,
                            Account.session_id != "manual",
                            Account.deleted == False,
                        )
                    ).all()
                    if a.id not in claimed_ids
                    and a.identification_hash in (None, ident_hash)
                ]
                if len(candidates) == 1:
                    existing = candidates[0]
                    name_match = True
                elif len(candidates) > 1:
                    log.warning(
                        "Ambiguous IBAN-less match for %s %s (%d candidates: %s); "
                        "creating a new account",
                        state, currency, len(candidates),
                        [a.id for a in candidates],
                    )

            if existing:
                db_acc = existing
                db_acc.external_id = uid
            else:
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
            # A name-only match is heuristic: never let it overwrite a stored
            # stable hash, or a wrong match becomes permanent.
            if ident_hash and (db_acc.identification_hash is None or not name_match):
                db_acc.identification_hash = ident_hash
            db_acc.session_id = session_id
            db_acc.connected = True
            db_acc.sync_error = None
            db.flush()
            claimed_ids.add(db_acc.id)
            saved.append(db_acc.id)
        db.commit()
        return saved


def _classify_account_type(name: str) -> str:
    name_lower = name.lower()
    for kw in config.INVESTMENT_ACCOUNT_KEYWORDS:
        if kw in name_lower:
            return "investment"
    return "checking"


# Enable Banking balance_type codes, most authoritative first.
_BALANCE_TYPE_PREFERENCE = ("CLBD", "CLAV", "XPCD", "ITAV")

# Days re-fetched before the newest synced row: covers late-booked items and
# lets PDNG rows inside the window be reconciled/upgraded.
SYNC_OVERLAP_DAYS = 7
# Never delete a vanished PDNG row younger than this: banks can briefly omit
# a fresh pending item from the feed.
PDNG_STALE_DAYS = 3


def _is_manual_external_id(external_id: str | None) -> bool:
    """Manually-entered rows (main.add_manual_transaction) use 'manual_<hex>'."""
    return bool(external_id) and external_id.startswith("manual_")


def _remittance_str(raw_tx: dict) -> str:
    ri = raw_tx.get("remittance_information")
    if isinstance(ri, list):
        return ri[0] if ri else ""
    return ri or ""


def _fallback_id(tx: dict, amount: float, currency: str) -> str:
    """Deterministic id for transactions the bank sends without any reference,
    so a re-sync of the same window does not duplicate them."""
    key = f"{tx.get('booking_date') or ''}|{amount}|{currency}|{_remittance_str(tx)}"
    return "h_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def _pick_balance(balances: list[dict]) -> dict:
    by_type = {b.get("balance_type"): b for b in balances if isinstance(b, dict)}
    for code in _BALANCE_TYPE_PREFERENCE:
        if code in by_type:
            return by_type[code]
    return balances[0]


def _compute_date_from(account: Account, session: Session, today: date | None = None) -> date:
    """Start of the fetch window for `account`.

    Based only on synced rows (manual ones can carry future dates and would
    otherwise block the sync forever), clamped to today, with a fixed overlap;
    then widened back to the oldest still-pending row so it can be reconciled.
    """
    today = today or date.today()
    rows = session.exec(
        select(Transaction.date, Transaction.external_id, Transaction.status)
        .where(Transaction.account_id == account.id)
    ).all()
    synced_dates = [d for d, ext, _ in rows if not _is_manual_external_id(ext)]
    if not synced_dates:
        return today - timedelta(days=90)
    date_from = min(max(synced_dates), today) - timedelta(days=SYNC_OVERLAP_DAYS)
    pending_dates = [
        d for d, ext, status in rows
        if status == "PDNG" and not _is_manual_external_id(ext)
    ]
    if pending_dates:
        date_from = min(date_from, min(pending_dates))
    return date_from


def sync_account(account: Account, session: Session):
    # Manual accounts have no bank connection — never sync them, and clear any
    # stale sync_error left over from before they were excluded.
    if account.session_id == "manual":
        if account.sync_error is not None:
            account.sync_error = None
            session.commit()
        return
    headers = _headers()
    uid = account.external_id
    today = date.today()
    date_from = _compute_date_from(account, session, today)

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

        pdng_count = sum(1 for t in all_txs if t.get("status") == "PDNG")
        book_count = sum(1 for t in all_txs if t.get("status") == "BOOK")
        print(f"[sync] {account.name}: {len(all_txs)} total from API — {book_count} BOOK, {pdng_count} PDNG", flush=True)

        # Pre-fetch external_ids for this account only (unique constraint is per-account)
        existing_ids: set[str] = set(session.exec(
            select(Transaction.external_id).where(Transaction.account_id == account.id)
        ).all())
        seen_ids: set[str] = set()
        # Every id present in the bank response (also ones already stored) and
        # the DB ids of PDNG rows matched/upgraded in this run: used afterwards
        # to spot pending rows the bank silently dropped.
        response_ids: set[str] = set()
        touched_pdng_ids: set[int] = set()
        # Still-pending rows by external_id: lets a PDNG→BOOK transition that
        # keeps the same entry_reference be upgraded in place.
        pdng_by_ext: dict[str, Transaction] = {
            t.external_id: t
            for t in session.exec(
                select(Transaction).where(
                    Transaction.account_id == account.id,
                    Transaction.status == "PDNG",
                )
            ).all()
        }

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
        def _forget_pdng(row: Transaction) -> None:
            # Drop a pending row from the merge maps once it has been consumed.
            for k, v in list(pdng_map.items()):
                if v is row:
                    pdng_map.pop(k, None)
            lst = pdng_map_date_amount.get((row.date, row.amount), [])
            if row in lst:
                lst.remove(row)

        with session.no_autoflush:
            for tx in all_txs:
                amount = float(tx["transaction_amount"]["amount"])
                if tx.get("credit_debit_indicator") == "DBIT":
                    amount = -abs(amount)
                else:
                    amount = abs(amount)
                tx_currency = (tx.get("transaction_amount") or {}).get("currency") or account.currency or "EUR"
                tx_id = (
                    tx.get("entry_reference")
                    or tx.get("transaction_id")
                    or _fallback_id(tx, amount, tx_currency)
                )
                response_ids.add(tx_id)
                tx_status = tx.get("status")
                if tx_id in existing_ids or tx_id in seen_ids:
                    stored = pdng_by_ext.get(tx_id)
                    if stored is not None and tx_status == "BOOK":
                        # Same entry_reference, PDNG→BOOK: upgrade in place,
                        # keep category/share set by user.
                        parsed = parse_transaction(tx, account.bank_name)
                        upgraded_date = parsed["date"]
                        if upgraded_date is None:
                            d_str = tx.get("transaction_date") or tx.get("booking_date") or tx.get("value_date")
                            upgraded_date = date.fromisoformat(d_str) if d_str else stored.date
                        stored.raw_data = json.dumps(tx)
                        stored.merchant = parsed["merchant"]
                        stored.description = parsed["description"]
                        stored.date = upgraded_date
                        stored.status = "BOOK"
                        touched_pdng_ids.add(stored.id)
                        _forget_pdng(stored)
                        pdng_by_ext.pop(tx_id, None)
                    elif stored is not None:
                        touched_pdng_ids.add(stored.id)
                    continue
                seen_ids.add(tx_id)
                parsed = parse_transaction(tx, account.bank_name)
                merchant = parsed["merchant"]
                desc = parsed["description"]
                tx_date = parsed["date"]
                if tx_date is None:
                    tx_date_str = tx.get("transaction_date") or tx.get("booking_date") or tx.get("value_date")
                    tx_date = date.fromisoformat(tx_date_str) if tx_date_str else today

                # PDNG resolution: match on (date, amount, HH:MM from remittance).
                # Fallback to (date, amount) alone when times differ due to UTC/local drift.
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
                        pdng_by_ext.pop(matched_pdng.external_id, None)
                        matched_pdng.external_id = tx_id
                        matched_pdng.raw_data = json.dumps(tx)
                        matched_pdng.merchant = merchant
                        matched_pdng.description = desc
                        matched_pdng.date = tx_date
                        matched_pdng.status = "BOOK"
                        existing_ids.add(tx_id)
                        touched_pdng_ids.add(matched_pdng.id)
                    elif tx_status == "PDNG":
                        # Still pending — refresh data, keep category/share set by user
                        pdng_by_ext.pop(matched_pdng.external_id, None)
                        matched_pdng.external_id = tx_id
                        matched_pdng.raw_data = json.dumps(tx)
                        matched_pdng.merchant = merchant
                        matched_pdng.description = desc
                        matched_pdng.date = tx_date
                        existing_ids.add(tx_id)
                        pdng_by_ext[tx_id] = matched_pdng
                        touched_pdng_ids.add(matched_pdng.id)
                    else:
                        # Cancelled/rejected: remove the pending transaction
                        touched_pdng_ids.add(matched_pdng.id)
                        session.delete(matched_pdng)
                    continue

                cat_id = categorize(desc, merchant, session)
                eur_amount = None
                if tx_currency != "EUR":
                    try:
                        eur_amount = _fx.convert_on(amount, tx_currency, tx_date, session=session)
                    except Exception as fx_err:
                        log.warning(
                            "FX conversion unavailable for %s %s on %s (%s / %s): %s",
                            amount, tx_currency, tx_date, account.bank_name, account.name, fx_err,
                        )
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

            # Pending rows inside the fetched window that the bank no longer
            # reports (reversed/cancelled) and that no incoming row touched:
            # drop them, unless they are fresh enough that the bank may simply
            # not have exposed them yet.
            stale_before = today - timedelta(days=PDNG_STALE_DAYS)
            for ext_id, row in list(pdng_by_ext.items()):
                if (
                    row.id in touched_pdng_ids
                    or ext_id in response_ids
                    or _is_manual_external_id(ext_id)
                    or row.date < date_from
                    or row.date > stale_before
                ):
                    continue
                log.warning(
                    "Dropping vanished PDNG %s on %s / %s: %s %s %s (%s)",
                    ext_id, account.bank_name, account.name,
                    row.date, row.amount, row.currency, row.merchant or row.description,
                )
                session.delete(row)

            # Fetch balance inside no_autoflush so pending tx don't trigger a flush
            r = requests.get(f"{API_ORIGIN}/accounts/{uid}/balances", headers=headers)
            r.raise_for_status()
            balances = r.json().get("balances", [])
            if balances:
                bal_amount = float(_pick_balance(balances)["balance_amount"]["amount"])
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

        # Local wall-clock time: the template renders it verbatim.
        account.last_sync = datetime.now()
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
        account_ids = [
            a.id for a in session.exec(
                select(Account).where(Account.connected == True, Account.session_id != "manual")
            ).all()
        ]

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
