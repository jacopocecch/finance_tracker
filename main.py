import json
import logging
import os
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, select

import config
import scheduler
from database import (
    Account, Transaction, BalanceSnapshot, Category, CategoryRule, MerchantCategory, Budget,
    Instrument, MacroCategory, Trip, engine, init_db, get_session,
)
from colors import derive_leaf_colors
from sync import build_auth_url, handle_callback, sync_all, sync_account
from investments import router as investments_router, _build_portfolio_data
from portfolio import compute_portfolio
import fx as _fx
from fx import FxUnavailable

log = logging.getLogger(__name__)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.include_router(investments_router)


def _tag_text_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#ffffff"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#111827" if luminance > 160 else "#ffffff"


templates.env.filters["tag_text_color"] = _tag_text_color

_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CHF": "CHF",
                     "SEK": "kr", "NOK": "kr", "DKK": "kr", "PLN": "zł", "CZK": "Kč",
                     "HUF": "Ft", "RON": "lei", "TRY": "₺", "CNY": "¥", "HKD": "HK$",
                     "SGD": "S$", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "MXN": "MX$"}

def _currency_symbol(code: str) -> str:
    return _CURRENCY_SYMBOLS.get((code or "EUR").upper(), code or "€")

templates.env.filters["currency_symbol"] = _currency_symbol


if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")

SUPPORTED_BANKS = [
    {"name": "FinecoBank",          "display_name": "FinecoBank"},
    {"name": "ING",                 "display_name": "ING"},
    {"name": "Revolut",             "display_name": "Revolut"},
    {"name": "PayPal",              "display_name": "PayPal"}
]


@app.on_event("startup")
def startup():
    init_db()
    scheduler.start()


@app.on_event("shutdown")
def shutdown():
    scheduler.stop()


def flash(message: str, type_: str = "info"):
    return {"message": message, "type": type_}


def _flash_from_query(msg: str = "", msg_type: str = "info") -> Optional[dict]:
    return {"message": msg, "type": msg_type} if msg else None


def _redirect_flash(path: str, message: str, type_: str = "info") -> RedirectResponse:
    """303 redirect to `path` carrying a flash message in the query string."""
    sep = "&" if "?" in path else "?"
    qs = urllib.parse.urlencode({"msg": message, "msg_type": type_})
    return RedirectResponse(f"{path}{sep}{qs}", status_code=303)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _opt_int(v: Optional[str]) -> Optional[int]:
    """Coerce an optional form field to int; empty/blank/invalid → None."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def _opt_float(v: Optional[str]) -> Optional[float]:
    """Coerce an optional form field to float; empty/blank/invalid → None."""
    if v is None:
        return None
    v = v.strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


_fx_warned: set[str] = set()


def _fx_convert_safe(amount: float, currency: str, session: Session, on_date: Optional[date] = None) -> float:
    """Convert to EUR; when no rate is obtainable, warn once per currency and
    return the unconverted amount."""
    try:
        if on_date:
            return _fx.convert_on(amount, currency, on_date, session=session)
        return _fx.convert(amount, currency, session=session)
    except FxUnavailable:
        if currency not in _fx_warned:
            _fx_warned.add(currency)
            log.warning("FX rate unavailable for %s: using unconverted amounts", currency)
        return amount

def _enrich_tx(tx: Transaction, session: Session) -> SimpleNamespace:
    ns = SimpleNamespace(**{k: v for k, v in vars(tx).items() if not k.startswith('_')})
    ns.category = session.get(Category, tx.category_id)
    ns.account  = session.get(Account, tx.account_id)
    ns.deletable = _is_deletable(tx, ns.account)
    return ns


def _is_deletable(tx: Transaction, account: Optional[Account]) -> bool:
    """Pending, manually-entered, and cash transactions can be deleted; synced bank ones cannot."""
    if tx.status == "PDNG":
        return True
    return bool(account and (account.session_id == "manual" or account.type == "cash"))


def _effective_amount(tx, session: Session = None) -> float:
    """Expense amount in EUR, respecting personal_share and FX conversion."""
    amount = tx.amount
    cur = getattr(tx, 'currency', 'EUR') or 'EUR'
    if cur != "EUR":
        eur_amount = getattr(tx, 'eur_amount', None)
        if eur_amount is not None:
            ps = getattr(tx, 'personal_share', None)
            if amount < 0 and ps is not None and amount != 0:
                # Scale the EUR amount by the personal fraction; sign preserved.
                return eur_amount * abs(ps / amount)
            return eur_amount
        if session:
            amount = _fx_convert_safe(amount, cur, session, on_date=getattr(tx, 'date', None))
    if amount < 0 and getattr(tx, 'personal_share', None) is not None:
        return -tx.personal_share
    return amount


def _get_balance_warnings(session: Session) -> list[dict]:
    accounts = session.exec(
        select(Account).where(Account.connected == True, Account.deleted == False, Account.balance_threshold != None)
    ).all()
    warnings = []
    for acc in accounts:
        snap = session.exec(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == acc.id)
            .order_by(BalanceSnapshot.date.desc())
        ).first()
        if snap and snap.balance < acc.balance_threshold:
            name = acc.display_name or f"{acc.bank_name} — {acc.name}"
            warnings.append({
                "name": name,
                "balance": snap.balance,
                "threshold": acc.balance_threshold,
                "currency": acc.currency or "EUR",
            })
    return warnings


def _balances_by_account(session: Session) -> dict[int, dict]:
    """Latest balance snapshot per account. Returns dict with EUR and native values."""
    accounts = session.exec(select(Account).where(Account.connected == True)).all()
    result = {}
    for acc in accounts:
        snap = session.exec(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == acc.id)
            .order_by(BalanceSnapshot.date.desc())
        ).first()
        if snap:
            native = snap.balance
            currency = acc.currency or "EUR"
            if currency != "EUR":
                eur = _fx_convert_safe(native, currency, session)
            else:
                eur = native
            result[acc.id] = {"eur": eur, "native": native, "currency": currency}
        else:
            result[acc.id] = {"eur": 0.0, "native": 0.0, "currency": acc.currency or "EUR"}
    return result


CHART_START_DATE = date(2026, 5, 24)

def _networth_series(session: Session) -> tuple[list[str], list[float]]:
    start = CHART_START_DATE
    today = date.today()
    # Include archived accounts too: their snapshots must keep contributing to
    # past days, otherwise archiving rewrites history.
    liquidity_accounts = {
        a.id: a for a in session.exec(
            select(Account).where(Account.type.in_(("checking", "savings", "cash")))
        ).all()
    }
    if not liquidity_accounts:
        return [], []

    # Fetch ALL snapshots for these accounts (including before start, for forward-fill)
    all_snaps = session.exec(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id.in_(list(liquidity_accounts.keys())))
        .order_by(BalanceSnapshot.date)
    ).all()

    # Group by account, sorted by date
    by_account: dict[int, list[BalanceSnapshot]] = defaultdict(list)
    for s in all_snaps:
        by_account[s.account_id].append(s)

    # For each day in range, forward-fill last known snapshot per account
    all_dates = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    result: dict[date, float] = {}
    for d in all_dates:
        total = 0.0
        has_any = False
        for acc_id, acc in liquidity_accounts.items():
            snaps = by_account.get(acc_id, [])
            # Archived/disconnected accounts stop counting after their last
            # snapshot instead of forward-filling a stale balance to today
            # (consistent with _balances_by_account, which excludes them).
            if (acc.deleted or not acc.connected) and (not snaps or d > snaps[-1].date):
                continue
            last_snap = None
            for s in snaps:
                if s.date <= d:
                    last_snap = s
                else:
                    break
            if last_snap is not None:
                has_any = True
                balance = last_snap.balance
                if acc.currency and acc.currency != "EUR":
                    balance = _fx_convert_safe(balance, acc.currency, session)
                total += balance
        if has_any:
            result[d] = total

    dates = sorted(result)
    return (
        [d.strftime("%d/%m") for d in dates],
        [round(result[d], 2) for d in dates],
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    accounts = session.exec(select(Account).where(Account.connected == True)).all()
    balances = _balances_by_account(session)
    labels, networth_data = _networth_series(session)

    liquidity = sum(
        balances.get(a.id, {"eur": 0})["eur"] for a in accounts if a.type in ("checking", "savings", "cash")
    )
    bank_investments = sum(
        balances.get(a.id, {"eur": 0})["eur"] for a in accounts if a.type == "investment"
    )

    try:
        portfolio = _build_portfolio_data(session)
        liquidity_ids = {
            inst.id for inst in session.exec(
                select(Instrument).where(Instrument.is_liquidity == True, Instrument.active == True)
            ).all()
        }
        # Split the basket exactly like investments.overview(): liquidity ETFs
        # count as liquidity, everything else is the investment portfolio, and
        # P&L is computed on that same filtered basket (summary totals are EUR).
        inv_positions = [p for p in portfolio.positions if p.instrument_id not in liquidity_ids]
        liq_positions = [p for p in portfolio.positions if p.instrument_id in liquidity_ids]
        inv_summary = compute_portfolio(inv_positions, portfolio.pac_positions)
        liq_summary = compute_portfolio(liq_positions, [])
        liquidity_etf = (
            liq_summary.total_market_value if liq_summary.total_market_value is not None
            else liq_summary.total_invested
        )
        portfolio_value = (
            inv_summary.total_market_value if inv_summary.total_market_value is not None
            else inv_summary.total_invested
        )
        portfolio_pl = inv_summary.total_unrealized_pl
        portfolio_pl_pct = inv_summary.total_unrealized_pl_pct
    except Exception:
        liquidity_etf = 0.0
        portfolio_value = 0.0
        portfolio_pl = None
        portfolio_pl_pct = None

    liquidity = liquidity + liquidity_etf
    investments_total = bank_investments + portfolio_value
    net_worth = liquidity + investments_total

    acc_with_balance = sorted(
        [(a, balances.get(a.id, {"eur": 0.0, "native": 0.0, "currency": "EUR"})) for a in accounts],
        key=lambda x: (x[0].session_id == "manual", -x[1]["eur"])
    )

    balance_warnings = _get_balance_warnings(session)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "balance_warnings": balance_warnings,
        "liquidity": liquidity,
        "liquidity_etf": liquidity_etf,
        "investments": investments_total,
        "portfolio_value": portfolio_value,
        "portfolio_pl": portfolio_pl,
        "portfolio_pl_pct": portfolio_pl_pct,
        "net_worth": net_worth,
        "accounts": [
            type("Acc", (), {
                **a.__dict__,
                "balance": b["eur"],
                "balance_native": b["native"],
                "balance_currency": b["currency"],
            })()
            for a, b in acc_with_balance
        ],
        "labels": labels,
        "networth_data": networth_data,
    })


@app.get("/monthly", response_class=HTMLResponse)
def monthly(
    request: Request,
    month: Optional[str] = None,
    cat: Optional[str] = None,
    tx_type: Optional[str] = None,
    session: Session = Depends(get_session),
):
    today = date.today()
    if month:
        year, m = int(month[:4]), int(month[5:])
    else:
        year, m = today.year, today.month
    current_month = today.strftime("%Y-%m")
    month_str = f"{year:04d}-{m:02d}"

    first_day = date(year, m, 1)
    last_m = m + 1 if m < 12 else 1
    last_y = year if m < 12 else year + 1
    last_day = date(last_y, last_m, 1) - timedelta(days=1)

    prev = (date(year, m, 1) - timedelta(days=1))
    prev_month = prev.strftime("%Y-%m")
    next_day = last_day + timedelta(days=1)
    next_month = next_day.strftime("%Y-%m")

    month_names = ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                   "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    month_label = f"{month_names[m-1]} {year}"

    q = select(Transaction).where(
        Transaction.date >= first_day,
        Transaction.date <= last_day,
    )
    if cat:
        q = q.where(Transaction.category_id == int(cat))
    all_transactions = [_enrich_tx(tx, session) for tx in session.exec(q.order_by(Transaction.date.desc())).all()]

    def _is_transfer(tx) -> bool:
        return tx.category is not None and tx.category.type == "transfer"

    def _is_investment(tx) -> bool:
        return tx.category is not None and tx.category.type == "investment"

    total_invested = abs(sum(
        _effective_amount(tx, session)
        for tx in all_transactions
        if _is_investment(tx) and tx.amount < 0
    ))

    transactions = [tx for tx in all_transactions if not _is_transfer(tx) and not _is_investment(tx) and tx.amount != 0]
    if tx_type == "in":
        transactions = [tx for tx in transactions if tx.amount > 0]
    elif tx_type == "out":
        transactions = [tx for tx in transactions if tx.amount < 0]

    total_in  = sum(_effective_amount(tx, session) for tx in transactions if tx.amount > 0 and not _is_transfer(tx) and not tx.is_reimbursement)
    total_out = abs(sum(_effective_amount(tx, session) for tx in transactions if tx.amount < 0 and not _is_transfer(tx)))
    balance   = total_in - total_out - total_invested

    # Weekly aggregation: 7-day buckets from the 1st, covering the whole month
    # (5 buckets when needed so days 29–31 are not dropped)
    weekly_in, weekly_out, weekly_labels = [], [], []
    w_start = first_day
    week = 0
    while w_start <= last_day:
        w_end   = min(w_start + timedelta(days=6), last_day)
        w_txs   = [tx for tx in transactions if w_start <= tx.date <= w_end and not _is_transfer(tx)]
        weekly_in.append(round(sum(_effective_amount(t, session) for t in w_txs if t.amount > 0 and not t.is_reimbursement), 2))
        weekly_out.append(round(abs(sum(_effective_amount(t, session) for t in w_txs if t.amount < 0)), 2))
        week += 1
        weekly_labels.append(f"Sett {week}")
        w_start = w_end + timedelta(days=1)

    # Category breakdown (expenses only, with budget); trip expenses grouped per trip
    cat_totals: dict[str, float] = defaultdict(float)
    cat_id_map: dict[str, int] = {}
    cat_colors_map: dict[str, str] = {}
    cat_macro_map: dict[str, Optional[int]] = {}
    trip_totals: dict[int, float] = defaultdict(float)
    for tx in transactions:
        if tx.amount < 0 and not _is_transfer(tx):
            if getattr(tx, "trip_id", None):
                trip_totals[tx.trip_id] += abs(_effective_amount(tx, session))
            elif tx.category:
                cat_totals[tx.category.name] += abs(_effective_amount(tx, session))
                cat_colors_map[tx.category.name] = tx.category.color
                cat_id_map[tx.category.name] = tx.category.id
                cat_macro_map[tx.category.name] = tx.category.macrocategory_id

    budgets_map = {
        b.category_id: b.amount
        for b in session.exec(select(Budget).where(Budget.active == True, Budget.period == "monthly")).all()
    }

    cat_rows = []
    for name, total in cat_totals.items():
        cid = cat_id_map[name]
        budget_amount = budgets_map.get(cid)
        cat_rows.append({
            "name": name,
            "total": round(total, 2),
            "color": cat_colors_map[name],
            "macro_id": cat_macro_map[name],
            "budget": budget_amount,
            "pct": min(round(total / budget_amount * 100), 100) if budget_amount else None,
        })
    if trip_totals:
        trip_names = {t.id: t.name for t in session.exec(select(Trip).where(Trip.id.in_(trip_totals))).all()}
        for tid, total in trip_totals.items():
            cat_rows.append({
                "name": f"✈️ {trip_names.get(tid, 'Viaggio')}",
                "total": round(total, 2),
                "color": "#818CF8",
                "macro_id": None,
                "budget": None,
                "pct": None,
            })
    # Keep same-macro shades together: order groups by their combined total,
    # then rows within a group by total. Ungrouped categories sort by own total.
    macro_totals: dict[Optional[int], float] = defaultdict(float)
    for r in cat_rows:
        macro_totals[r["macro_id"]] += r["total"]
    cat_rows.sort(key=lambda r: (
        -(macro_totals[r["macro_id"]] if r["macro_id"] else r["total"]),
        r["macro_id"] or 0,
        -r["total"],
    ))

    cat_labels = [r["name"] for r in cat_rows]
    cat_data = [r["total"] for r in cat_rows]
    cat_colors = [r["color"] for r in cat_rows]

    categories = session.exec(select(Category)).all()

    return templates.TemplateResponse("monthly.html", {
        "request": request,
        "month": month_str,
        "month_label": month_label,
        "prev_month": prev_month,
        "next_month": next_month,
        "current_month": current_month,
        "total_in": total_in,
        "total_out": total_out,
        "total_invested": total_invested,
        "balance": balance,
        "transactions": transactions,
        "weekly_labels": weekly_labels,
        "weekly_in": weekly_in,
        "weekly_out": weekly_out,
        "cat_labels": cat_labels,
        "cat_data": cat_data,
        "cat_colors": cat_colors,
        "cat_rows": cat_rows,
        "categories": categories,
        "selected_cat": cat or "",
        "tx_type": tx_type or "",
    })


@app.get("/yearly", response_class=HTMLResponse)
def yearly(
    request: Request,
    year: Optional[int] = None,
    cat: Optional[str] = None,
    session: Session = Depends(get_session),
):
    today = date.today()
    if year is None:
        year = today.year
    current_year = today.year

    first_day = date(year, 1, 1)
    last_day = date(year, 12, 31)

    q = select(Transaction).where(
        Transaction.date >= first_day,
        Transaction.date <= last_day,
    )
    if cat:
        q = q.where(Transaction.category_id == int(cat))
    all_transactions = [_enrich_tx(tx, session) for tx in session.exec(q.order_by(Transaction.date.desc())).all()]

    def _is_transfer(tx) -> bool:
        return tx.category is not None and tx.category.type == "transfer"

    def _is_investment(tx) -> bool:
        return tx.category is not None and tx.category.type == "investment"

    total_invested = abs(sum(
        _effective_amount(tx, session)
        for tx in all_transactions
        if _is_investment(tx) and tx.amount < 0
    ))

    transactions = [tx for tx in all_transactions if not _is_transfer(tx) and not _is_investment(tx) and tx.amount != 0]

    total_in  = sum(_effective_amount(tx, session) for tx in transactions if tx.amount > 0 and not tx.is_reimbursement)
    total_out = abs(sum(_effective_amount(tx, session) for tx in transactions if tx.amount < 0))
    balance   = total_in - total_out - total_invested

    # Monthly aggregation (12 months)
    month_names_short = ["Gen","Feb","Mar","Apr","Mag","Giu",
                         "Lug","Ago","Set","Ott","Nov","Dic"]
    monthly_in  = [0.0] * 12
    monthly_out = [0.0] * 12
    for tx in transactions:
        idx = tx.date.month - 1
        amt = _effective_amount(tx, session)
        if tx.amount > 0 and not tx.is_reimbursement:
            monthly_in[idx] += amt
        elif tx.amount < 0:
            monthly_out[idx] += abs(amt)
    monthly_in  = [round(v, 2) for v in monthly_in]
    monthly_out = [round(v, 2) for v in monthly_out]

    # Months elapsed in the selected year (for monthly average)
    if year < current_year:
        months_elapsed = 12
    elif year == current_year:
        months_elapsed = today.month
    else:
        months_elapsed = 1

    # Category breakdown (expenses only); trip expenses grouped per trip
    cat_totals: dict[str, float] = defaultdict(float)
    cat_colors_map: dict[str, str] = {}
    cat_macro_map: dict[str, Optional[int]] = {}
    trip_totals: dict[int, float] = defaultdict(float)
    for tx in transactions:
        if tx.amount < 0:
            if getattr(tx, "trip_id", None):
                trip_totals[tx.trip_id] += abs(_effective_amount(tx, session))
            elif tx.category:
                cat_totals[tx.category.name] += abs(_effective_amount(tx, session))
                cat_colors_map[tx.category.name] = tx.category.color
                cat_macro_map[tx.category.name] = tx.category.macrocategory_id

    cat_rows = []
    for name, total in cat_totals.items():
        cat_rows.append({
            "name": name,
            "total": round(total, 2),
            "color": cat_colors_map[name],
            "macro_id": cat_macro_map[name],
            "monthly_avg": round(total / months_elapsed, 2),
            "pct_of_out": round(total / total_out * 100, 1) if total_out else 0,
        })
    if trip_totals:
        trip_names = {t.id: t.name for t in session.exec(select(Trip).where(Trip.id.in_(trip_totals))).all()}
        for tid, total in trip_totals.items():
            cat_rows.append({
                "name": f"✈️ {trip_names.get(tid, 'Viaggio')}",
                "total": round(total, 2),
                "color": "#818CF8",
                "macro_id": None,
                "monthly_avg": round(total / months_elapsed, 2),
                "pct_of_out": round(total / total_out * 100, 1) if total_out else 0,
            })
    cat_rows.sort(key=lambda r: -r["total"])

    cat_labels = [r["name"] for r in cat_rows]
    cat_data   = [r["total"] for r in cat_rows]
    cat_colors = [r["color"] for r in cat_rows]

    categories = session.exec(select(Category)).all()

    return templates.TemplateResponse("yearly.html", {
        "request": request,
        "year": year,
        "prev_year": year - 1,
        "next_year": year + 1,
        "current_year": current_year,
        "total_in": total_in,
        "total_out": total_out,
        "total_invested": total_invested,
        "balance": balance,
        "avg_in": total_in / months_elapsed,
        "avg_out": total_out / months_elapsed,
        "monthly_labels": month_names_short,
        "monthly_in": monthly_in,
        "monthly_out": monthly_out,
        "cat_labels": cat_labels,
        "cat_data": cat_data,
        "cat_colors": cat_colors,
        "cat_rows": cat_rows,
        "categories": categories,
        "selected_cat": cat or "",
    })


def _categories_by_frequency(session: Session) -> list:
    from sqlalchemy import func
    counts = dict(
        session.exec(
            select(Transaction.category_id, func.count(Transaction.id).label("n"))
            .group_by(Transaction.category_id)
        ).all()
    )
    cats = session.exec(select(Category)).all()
    return sorted(cats, key=lambda c: counts.get(c.id, 0), reverse=True)


@app.get("/transactions", response_class=HTMLResponse)
def transactions_view(
    request: Request,
    cat: Optional[str] = None,
    account: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to_: Optional[str] = Query(default=None, alias="to"),
    tx_type: Optional[str] = Query(default=None, alias="type"),
    search: Optional[str] = None,
    new_since: Optional[str] = None,
    page: int = 1,
    msg: str = "",
    msg_type: str = "info",
    session: Session = Depends(get_session),
):
    page_size = 50
    q = select(Transaction).order_by(Transaction.date.desc())
    if cat:
        q = q.where(Transaction.category_id == int(cat))
    if account:
        q = q.where(Transaction.account_id == int(account))
    if from_:
        q = q.where(Transaction.date >= date.fromisoformat(from_))
    if to_:
        q = q.where(Transaction.date <= date.fromisoformat(to_))
    if tx_type == "income":
        q = q.where(Transaction.amount > 0)
    elif tx_type == "expense":
        q = q.where(Transaction.amount < 0)

    all_txs = session.exec(q).all()
    all_txs = [tx for tx in all_txs if tx.amount != 0]

    if search:
        s = search.lower()
        all_txs = [
            tx for tx in all_txs
            if s in (tx.description or "").lower() or s in (tx.merchant or "").lower()
        ]

    # When new_since is provided, float newly-synced transactions to the top
    new_since_dt = None
    if new_since:
        try:
            new_since_dt = datetime.fromisoformat(new_since).replace(tzinfo=None)
            new_txs = sorted(
                [tx for tx in all_txs if tx.created_at and tx.created_at >= new_since_dt],
                key=lambda tx: tx.created_at, reverse=True
            )
            old_txs = [tx for tx in all_txs if not (tx.created_at and tx.created_at >= new_since_dt)]
            all_txs = new_txs + old_txs
        except ValueError:
            new_since = None

    total = len(all_txs)
    txs = [_enrich_tx(tx, session) for tx in all_txs[(page-1)*page_size : page*page_size]]

    qs_parts = {k: v for k, v in {"cat": cat, "account": account, "from": from_, "to": to_, "type": tx_type, "search": search, "new_since": new_since}.items() if v}
    query_string = urllib.parse.urlencode(qs_parts)

    mapped_merchants = {
        mc.merchant
        for mc in session.exec(select(MerchantCategory)).all()
    }

    trips_list = session.exec(select(Trip).order_by(Trip.start_date.desc())).all()

    return templates.TemplateResponse("transactions.html", {
        "request": request,
        "transactions": txs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "categories": _categories_by_frequency(session),
        "accounts": session.exec(select(Account).order_by(Account.deleted)).all(),
        "manual_accounts": session.exec(select(Account).where(Account.session_id == "manual", Account.deleted == False)).all(),
        "today": date.today().isoformat(),
        "selected_cat": cat or "",
        "selected_account": account or "",
        "selected_type": tx_type or "",
        "date_from": from_,
        "date_to": to_,
        "search": search or "",
        "query_string": query_string,
        "mapped_merchants": mapped_merchants,
        "trips": trips_list,
        "trip_map": {t.id: t.name for t in trips_list},
        "new_since": new_since,
        "new_since_dt": new_since_dt,
        "cash_accounts": session.exec(
            select(Account).where(Account.type == "cash", Account.deleted == False)
        ).all(),
        "flash": _flash_from_query(msg, msg_type),
    })


@app.post("/transactions/{tx_id}/category")
def update_category(
    tx_id: int,
    request: Request,
    category_id: int = Form(...),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.category_id = category_id
        tx.is_confirmed = True
        session.commit()
    if request.headers.get("X-Fetch"):
        cat = session.get(Category, category_id)
        color = cat.color if cat else "#6b6b88"
        return JSONResponse({"color": color, "name": cat.name if cat else "Altro", "icon": cat.icon if cat else "❓", "text_color": _tag_text_color(color), "is_confirmed": True})
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/transactions/{tx_id}/confirm")
def toggle_confirm(tx_id: int, session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.is_confirmed = not tx.is_confirmed
        session.commit()
        return JSONResponse({"is_confirmed": tx.is_confirmed})
    return JSONResponse({"is_confirmed": False})


@app.post("/transactions/{tx_id}/reimbursement")
def toggle_reimbursement(
    tx_id: int,
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.is_reimbursement = not tx.is_reimbursement
        session.commit()
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/transactions/{tx_id}/delete")
def delete_transaction(
    tx_id: int,
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx and _is_deletable(tx, session.get(Account, tx.account_id)):
        # Unlink any transfer partner first to avoid dangling reference
        if tx.transfer_partner_id:
            partner = session.get(Transaction, tx.transfer_partner_id)
            if partner:
                partner.transfer_partner_id = None
        session.delete(tx)
        session.commit()
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/transactions/{tx_id}/share")
def update_share(
    tx_id: int,
    personal_share: Optional[str] = Form(default=None),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    share = _opt_float(personal_share)
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.personal_share = share if share and share > 0 else None
        session.commit()
    return RedirectResponse(redirect_to, status_code=303)


# ── Viaggi ────────────────────────────────────────────────────────────────────

def _trip_candidates(trip: Trip, session: Session) -> list[Transaction]:
    """In-range transactions not yet assigned to any trip, excluding transfers/investments."""
    txs = session.exec(
        select(Transaction).where(
            Transaction.trip_id == None,
            Transaction.date >= trip.start_date,
            Transaction.date <= trip.end_date,
        )
    ).all()
    out = []
    for tx in txs:
        if tx.amount == 0:
            continue
        cat = session.get(Category, tx.category_id) if tx.category_id else None
        if cat and cat.type in ("transfer", "investment"):
            continue
        out.append(tx)
    return out


def _trip_stats(trip: Trip, session: Session) -> dict:
    txs = session.exec(select(Transaction).where(Transaction.trip_id == trip.id)).all()
    spent = sum(abs(_effective_amount(tx, session)) for tx in txs if tx.amount < 0)
    refunds = sum(_effective_amount(tx, session) for tx in txs if tx.amount > 0)
    return {"count": len(txs), "spent": round(spent, 2), "refunds": round(refunds, 2),
            "net": round(spent - refunds, 2)}


@app.get("/trips", response_class=HTMLResponse)
def trips_view(request: Request, session: Session = Depends(get_session), msg: str = "", msg_type: str = "info"):
    trips = session.exec(select(Trip).order_by(Trip.start_date.desc())).all()
    return templates.TemplateResponse("trips.html", {
        "request": request,
        "trips": trips,
        "stats": {t.id: _trip_stats(t, session) for t in trips},
        "today": date.today().isoformat(),
        "flash": _flash_from_query(msg, msg_type),
    })


def _parse_trip_dates(start_date: str, end_date: str) -> tuple[Optional[date], Optional[date], Optional[str]]:
    """Returns (start, end, error). Rejects unparsable dates and end < start."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return None, None, "Date non valide"
    if end < start:
        return None, None, "La data di fine non può precedere quella di inizio"
    return start, end, None


@app.post("/trips/add")
def add_trip(
    name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    session: Session = Depends(get_session),
):
    existing = session.exec(select(Trip).where(Trip.name == name.strip())).first()
    if existing:
        return RedirectResponse(f"/trips/{existing.id}", status_code=303)
    start, end, err = _parse_trip_dates(start_date, end_date)
    if err:
        return _redirect_flash("/trips", err, "error")
    trip = Trip(name=name.strip(), start_date=start, end_date=end)
    session.add(trip)
    session.commit()
    session.refresh(trip)
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@app.get("/trips/{trip_id}", response_class=HTMLResponse)
def trip_detail(trip_id: int, request: Request, session: Session = Depends(get_session), msg: str = "", msg_type: str = "info"):
    trip = session.get(Trip, trip_id)
    if not trip:
        return RedirectResponse("/trips", status_code=303)
    txs = session.exec(
        select(Transaction).where(Transaction.trip_id == trip.id).order_by(Transaction.date.desc())
    ).all()

    cat_totals: dict[int, dict] = {}
    for tx in txs:
        if tx.amount >= 0:
            continue
        cat = session.get(Category, tx.category_id) if tx.category_id else None
        key = cat.id if cat else 0
        row = cat_totals.setdefault(key, {
            "name": cat.name if cat else "Altro",
            "icon": cat.icon if cat else "❓",
            "color": cat.color if cat else "#6b6b88",
            "total": 0.0, "count": 0,
        })
        row["total"] += abs(_effective_amount(tx, session))
        row["count"] += 1
    cat_rows = sorted(cat_totals.values(), key=lambda r: -r["total"])
    max_total = max((r["total"] for r in cat_rows), default=0)
    for r in cat_rows:
        r["total"] = round(r["total"], 2)
        r["pct"] = round(r["total"] / max_total * 100) if max_total else 0

    return templates.TemplateResponse("trip_detail.html", {
        "request": request,
        "trip": trip,
        "transactions": [_enrich_tx(tx, session) for tx in txs],
        "stats": _trip_stats(trip, session),
        "cat_rows": cat_rows,
        "days": max(1, (trip.end_date - trip.start_date).days + 1),
        "candidates_count": len(_trip_candidates(trip, session)),
        "flash": _flash_from_query(msg, msg_type),
    })


@app.post("/trips/{trip_id}/update")
def update_trip(
    trip_id: int,
    name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    session: Session = Depends(get_session),
):
    trip = session.get(Trip, trip_id)
    if trip:
        start, end, err = _parse_trip_dates(start_date, end_date)
        if err:
            return _redirect_flash(f"/trips/{trip_id}", err, "error")
        clash = session.exec(select(Trip).where(Trip.name == name.strip(), Trip.id != trip_id)).first()
        if not clash:
            trip.name = name.strip()
        trip.start_date = start
        trip.end_date = end
        session.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/delete")
def delete_trip(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip:
        for tx in session.exec(select(Transaction).where(Transaction.trip_id == trip_id)).all():
            tx.trip_id = None
        session.delete(trip)
        session.commit()
    return RedirectResponse("/trips", status_code=303)


@app.post("/trips/{trip_id}/assign-range")
def assign_trip_range(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip:
        for tx in _trip_candidates(trip, session):
            tx.trip_id = trip.id
        session.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/unassign/{tx_id}")
def unassign_trip_tx(trip_id: int, tx_id: int, session: Session = Depends(get_session)):
    tx = session.get(Transaction, tx_id)
    if tx and tx.trip_id == trip_id:
        tx.trip_id = None
        session.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/transactions/{tx_id}/trip")
def update_transaction_trip(
    tx_id: int,
    request: Request,
    trip_id: str = Form(default=""),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.trip_id = int(trip_id) if trip_id else None
        session.commit()
    if request.headers.get("X-Fetch"):
        trip = session.get(Trip, tx.trip_id) if tx and tx.trip_id else None
        return JSONResponse({"trip_id": tx.trip_id if tx else None, "trip_name": trip.name if trip else ""})
    return RedirectResponse(redirect_to, status_code=303)


def _parse_ids(ids: str) -> list[int]:
    return [int(i) for i in ids.split(",") if i.strip().isdigit()]


@app.post("/transactions/bulk-category")
def bulk_assign_category(
    ids: str = Form(...),
    category_id: int = Form(...),
    session: Session = Depends(get_session),
):
    count = 0
    for tx_id in _parse_ids(ids):
        tx = session.get(Transaction, tx_id)
        if tx:
            tx.category_id = category_id
            tx.is_confirmed = True
            count += 1
    session.commit()
    return JSONResponse({"updated": count})


@app.post("/transactions/bulk-trip")
def bulk_assign_trip(
    ids: str = Form(...),
    trip_id: str = Form(default=""),
    session: Session = Depends(get_session),
):
    tid = int(trip_id) if trip_id else None
    count = 0
    for tx_id in _parse_ids(ids):
        tx = session.get(Transaction, tx_id)
        if tx:
            tx.trip_id = tid
            count += 1
    session.commit()
    return JSONResponse({"updated": count})


# ── Budgets ───────────────────────────────────────────────────────────────────

@app.get("/budgets", response_class=HTMLResponse)
def budgets_view(request: Request, session: Session = Depends(get_session), msg: str = "", msg_type: str = "info"):
    categories = session.exec(select(Category).where(Category.type.in_(["expense", "both"]))).all()
    budgets = {b.category_id: b for b in session.exec(select(Budget)).all()}
    return templates.TemplateResponse("budgets.html", {
        "request": request,
        "categories": categories,
        "budgets": budgets,
        "flash": _flash_from_query(msg, msg_type),
    })


@app.post("/budgets/save")
def save_budget(
    category_id: int = Form(...),
    amount: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    amount = _opt_float(amount)
    if amount is None or amount < 0:
        return _redirect_flash("/budgets", "Importo budget non valido", "error")
    existing = session.exec(
        select(Budget).where(Budget.category_id == category_id, Budget.period == "monthly")
    ).first()
    if existing:
        existing.amount = amount
        existing.active = True
    else:
        session.add(Budget(category_id=category_id, amount=amount, period="monthly"))
    session.commit()
    return RedirectResponse("/budgets", status_code=303)


@app.post("/budgets/{budget_id}/delete")
def delete_budget(budget_id: int, session: Session = Depends(get_session)):
    b = session.get(Budget, budget_id)
    if b:
        session.delete(b)
        session.commit()
    return RedirectResponse("/budgets", status_code=303)


# ── Categories ────────────────────────────────────────────────────────────────

@app.get("/categories", response_class=HTMLResponse)
def categories_view(request: Request, session: Session = Depends(get_session), msg: str = "", msg_type: str = "info"):
    categories = session.exec(select(Category)).all()
    rules = session.exec(select(CategoryRule).order_by(CategoryRule.priority.desc())).all()
    rules_by_cat: dict[int, list] = defaultdict(list)
    for r in rules:
        rules_by_cat[r.category_id].append(r)
    merchants = session.exec(select(MerchantCategory)).all()
    merchants_by_cat: dict[int, list] = defaultdict(list)
    for m in merchants:
        merchants_by_cat[m.category_id].append(m)
    cat_map = {c.id: c for c in categories}
    macros = session.exec(select(MacroCategory).order_by(MacroCategory.sort, MacroCategory.name)).all()
    cats_by_macro: dict[Optional[int], list] = defaultdict(list)
    for c in categories:
        cats_by_macro[c.macrocategory_id].append(c)
    return templates.TemplateResponse("categories.html", {
        "request": request,
        "categories": categories,
        "rules_by_cat": dict(rules_by_cat),
        "merchants_by_cat": dict(merchants_by_cat),
        "all_merchants": merchants,
        "cat_map": cat_map,
        "macros": macros,
        "cats_by_macro": dict(cats_by_macro),
        "flash": _flash_from_query(msg, msg_type),
    })


def _recolor_macro(macro_id: int, session: Session):
    """Repaint a macro's child categories as evenly-spread shades of its hue."""
    macro = session.get(MacroCategory, macro_id)
    if not macro:
        return
    children = session.exec(
        select(Category).where(Category.macrocategory_id == macro_id).order_by(Category.id)
    ).all()
    shades = derive_leaf_colors(macro.color, len(children))
    for cat, shade in zip(children, shades):
        cat.color = shade
        session.add(cat)


@app.post("/categories/add")
def add_category(
    name: str = Form(...),
    cat_type: str = Form(default="expense"),
    color: str = Form(default="#6B7280"),
    icon: str = Form(default="💳"),
    macrocategory_id: Optional[str] = Form(default=None),
    session: Session = Depends(get_session),
):
    macro_id = _opt_int(macrocategory_id) or None
    name = name.strip()
    if not name:
        return _redirect_flash("/categories", "Nome categoria obbligatorio", "error")
    if session.exec(select(Category).where(Category.name == name)).first():
        return _redirect_flash("/categories", f"La categoria «{name}» esiste già", "error")
    cat = Category(name=name, type=cat_type, color=color, icon=icon, macrocategory_id=macro_id)
    session.add(cat)
    session.commit()
    if macro_id:
        _recolor_macro(macro_id, session)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{cat_id}/assign")
def assign_category_macro(
    cat_id: int,
    macrocategory_id: Optional[str] = Form(default=None),
    session: Session = Depends(get_session),
):
    cat = session.get(Category, cat_id)
    if cat:
        old_macro = cat.macrocategory_id
        new_macro = _opt_int(macrocategory_id) or None
        cat.macrocategory_id = new_macro
        session.add(cat)
        session.commit()
        for mid in {old_macro, new_macro}:
            if mid:
                _recolor_macro(mid, session)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/macros/add")
def add_macro(
    name: str = Form(...),
    color: str = Form(default="#6B7280"),
    session: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        return _redirect_flash("/categories", "Nome macrocategoria obbligatorio", "error")
    if session.exec(select(MacroCategory).where(MacroCategory.name == name)).first():
        return _redirect_flash("/categories", f"La macrocategoria «{name}» esiste già", "error")
    session.add(MacroCategory(name=name, color=color))
    session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/macros/{macro_id}/recolor")
def recolor_macro(macro_id: int, color: str = Form(default=None), session: Session = Depends(get_session)):
    macro = session.get(MacroCategory, macro_id)
    if macro:
        if color:
            macro.color = color
            session.add(macro)
        _recolor_macro(macro_id, session)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/macros/{macro_id}/delete")
def delete_macro(macro_id: int, session: Session = Depends(get_session)):
    children = session.exec(select(Category).where(Category.macrocategory_id == macro_id)).all()
    if not children:
        macro = session.get(MacroCategory, macro_id)
        if macro:
            session.delete(macro)
            session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{cat_id}/delete")
def delete_category(cat_id: int, session: Session = Depends(get_session)):
    has_txs = session.exec(select(Transaction).where(Transaction.category_id == cat_id)).first()
    if not has_txs:
        # Remove every row pointing at this category so no dangling ids remain.
        for rule in session.exec(select(CategoryRule).where(CategoryRule.category_id == cat_id)).all():
            session.delete(rule)
        for mc in session.exec(select(MerchantCategory).where(MerchantCategory.category_id == cat_id)).all():
            session.delete(mc)
        for b in session.exec(select(Budget).where(Budget.category_id == cat_id)).all():
            session.delete(b)
        cat = session.get(Category, cat_id)
        if cat:
            session.delete(cat)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{cat_id}/rules/add")
def add_rule(
    cat_id: int,
    pattern: str = Form(...),
    priority: int = Form(default=5),
    session: Session = Depends(get_session),
):
    session.add(CategoryRule(pattern=pattern.strip(), category_id=cat_id, priority=priority))
    session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(CategoryRule, rule_id)
    if rule:
        session.delete(rule)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


def _txs_with_merchant(session: Session, key: str) -> list[Transaction]:
    """Transactions whose merchant equals `key` (already lowercased).
    Compared in Python: SQLite's lower() is ASCII-only, so accented merchants
    ("È", "É"…) would never match a Python-lowercased key."""
    rows = session.exec(select(Transaction).where(Transaction.merchant != None)).all()
    return [t for t in rows if (t.merchant or "").lower() == key]


@app.post("/transactions/{tx_id}/merchant-category")
def assign_merchant_category(
    tx_id: int,
    request: Request,
    category_id: Optional[str] = Form(default=None),
    remove: str = Form(default=""),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    is_fetch = bool(request.headers.get("X-Fetch"))
    cat_id = _opt_int(category_id)
    tx = session.get(Transaction, tx_id)
    if not tx or not tx.merchant:
        if is_fetch:
            return JSONResponse({"error": "Transazione senza esercente"}, status_code=400)
        return RedirectResponse(redirect_to, status_code=303)

    key = tx.merchant.lower()
    existing = session.exec(
        select(MerchantCategory).where(MerchantCategory.merchant == key)
    ).first()

    if remove:
        if existing:
            session.delete(existing)
            session.commit()
        if is_fetch:
            return JSONResponse({"mapped": False})
        return RedirectResponse(redirect_to, status_code=303)

    if cat_id is None:
        if is_fetch:
            return JSONResponse({"error": "Categoria mancante"}, status_code=400)
        return RedirectResponse(redirect_to, status_code=303)

    if existing:
        existing.category_id = cat_id
    else:
        session.add(MerchantCategory(merchant=key, category_id=cat_id))
    for t in _txs_with_merchant(session, key):
        t.category_id = cat_id
    session.commit()
    if is_fetch:
        cat = session.get(Category, cat_id)
        color = cat.color if cat else "#6b6b88"
        return JSONResponse({"mapped": True, "color": color, "name": cat.name if cat else "Altro", "icon": cat.icon if cat else "❓", "text_color": _tag_text_color(color)})
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/merchant-categories/{mc_id}/update")
def update_merchant_category(
    mc_id: int,
    category_id: int = Form(...),
    session: Session = Depends(get_session),
):
    mc = session.get(MerchantCategory, mc_id)
    if mc:
        mc.category_id = category_id
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/merchant-categories/sync")
def sync_merchant_categories(session: Session = Depends(get_session)):
    mappings = session.exec(select(MerchantCategory)).all()
    # Group once in Python (Unicode-aware lower, see _txs_with_merchant).
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    for tx in session.exec(select(Transaction).where(Transaction.merchant != None)).all():
        by_merchant[(tx.merchant or "").lower()].append(tx)
    updated = 0
    for mc in mappings:
        for tx in by_merchant.get(mc.merchant, []):
            if tx.category_id != mc.category_id:
                tx.category_id = mc.category_id
                updated += 1
    session.commit()
    return RedirectResponse(f"/categories?msg={updated}+transazioni+aggiornate", status_code=303)


@app.post("/merchant-categories/{mc_id}/delete")
def delete_merchant_category(mc_id: int, session: Session = Depends(get_session)):
    mc = session.get(MerchantCategory, mc_id)
    if mc:
        session.delete(mc)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/recategorize")
def recategorize(session: Session = Depends(get_session)):
    from categorizer import recategorize_all
    recategorize_all(session)
    return RedirectResponse("/categories?msg=Ricategorizzazione+completata", status_code=303)



@app.post("/setup/reparse")
def reparse_transactions(session: Session = Depends(get_session)):
    import json as _json
    from parsers import parse_transaction
    txs = session.exec(select(Transaction)).all()
    updated = 0
    for tx in txs:
        if not tx.raw_data:
            continue
        raw = _json.loads(tx.raw_data)
        account = session.get(Account, tx.account_id)
        if not account:
            continue
        parsed = parse_transaction(raw, account.bank_name)
        tx.merchant = parsed["merchant"]
        tx.description = parsed["description"]
        updated += 1
    session.commit()
    return RedirectResponse(f"/setup?msg=Re-analizzate+{updated}+transazioni", status_code=303)


# ── Setup ─────────────────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request, session: Session = Depends(get_session), msg: str = "", msg_type: str = "info"):
    accounts = session.exec(select(Account).where(Account.deleted == False)).all()
    archived_accounts = session.exec(select(Account).where(Account.deleted == True)).all()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "accounts": accounts,
        "archived_accounts": archived_accounts,
        "supported_banks": SUPPORTED_BANKS,
        "flash": {"message": msg, "type": msg_type} if msg else None,
    })


@app.post("/setup/connect")
def connect_bank(bank: str = Form(...)):
    try:
        url = build_auth_url(bank)
        return RedirectResponse(url, status_code=302)
    except Exception as e:
        return RedirectResponse(f"/setup?msg=Errore+connessione:+{e}", status_code=303)


@app.get("/setup/callback")
def oauth_callback(request: Request, code: str, state: str):
    try:
        handle_callback(code, state)
        return RedirectResponse("/setup?msg=Banca+connessa+con+successo", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/setup?msg=Errore+callback:+{e}", status_code=303)


@app.post("/setup/account/{account_id}/name")
def update_account_name(account_id: int, display_name: str = Form(...), session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc:
        acc.display_name = display_name.strip() or None
        session.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/account/{account_id}/type")
def update_account_type(account_id: int, type: str = Form(...), session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc and type in ("checking", "savings", "investment", "cash"):
        acc.type = type
        session.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/account/{account_id}/sync")
def sync_one(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc:
        from database import engine as _engine
        with Session(_engine) as sync_session:
            acc2 = sync_session.get(Account, account_id)
            if acc2:
                sync_account(acc2, sync_session)
        with Session(_engine) as s:
            acc_fresh = s.get(Account, account_id)
        if acc_fresh and acc_fresh.sync_error:
            from urllib.parse import quote
            return RedirectResponse(f"/setup?msg={quote(acc_fresh.sync_error)}&msg_type=error", status_code=303)
    return RedirectResponse("/setup?msg=Sync+completato", status_code=303)


@app.post("/setup/account/{account_id}/delete")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc:
        acc.deleted = True
        acc.connected = False
        session.add(acc)
        session.commit()
    return RedirectResponse("/setup?msg=Conto+archiviato", status_code=303)


@app.post("/setup/account/{account_id}/restore")
def restore_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc:
        acc.deleted = False
        # Manual accounts have no bank session — fully active again on restore.
        # Bank accounts stay unsynced until reconnected via PSD2.
        acc.connected = acc.session_id == "manual"
        session.add(acc)
        session.commit()
    return RedirectResponse("/setup?msg=Conto+ripristinato", status_code=303)


# ── Manual accounts & transactions ───────────────────────────────────────────

@app.post("/setup/account/manual")
def create_manual_account(
    name: str = Form(...),
    bank_name: str = Form(...),
    acc_type: str = Form("checking"),
    currency: str = Form("EUR"),
    initial_balance: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    import uuid as _uuid
    initial_balance = _opt_float(initial_balance) or 0.0
    acc = Account(
        bank_name=bank_name.strip() or name.strip(),
        external_id=f"manual_{_uuid.uuid4().hex}",
        name=name.strip(),
        display_name=name.strip(),
        type=acc_type,
        currency=currency.upper().strip(),
        session_id="manual",
        connected=True,
    )
    session.add(acc)
    session.flush()
    session.add(BalanceSnapshot(account_id=acc.id, date=date.today(), balance=initial_balance))
    session.commit()
    return RedirectResponse("/setup?msg=Conto+aggiunto", status_code=303)


@app.post("/setup/account/{account_id}/threshold")
def update_account_threshold(
    account_id: int,
    threshold: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    threshold = _opt_float(threshold)
    acc = session.get(Account, account_id)
    if acc:
        acc.balance_threshold = threshold if threshold is not None and threshold >= 0 else None
        session.commit()
    return RedirectResponse("/setup?msg=Soglia+aggiornata", status_code=303)


@app.post("/setup/account/{account_id}/balance")
def update_manual_balance(
    account_id: int,
    balance: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    balance = _opt_float(balance)
    if balance is None:
        return _redirect_flash("/setup", "Inserisci un saldo valido", "error")
    acc = session.get(Account, account_id)
    if acc and acc.session_id == "manual":
        snap = session.exec(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == account_id)
            .order_by(BalanceSnapshot.date.desc())
        ).first()
        if snap and snap.date == date.today():
            snap.balance = balance
        else:
            session.add(BalanceSnapshot(account_id=account_id, date=date.today(), balance=balance))
        session.commit()
    return RedirectResponse("/setup?msg=Saldo+aggiornato", status_code=303)


@app.post("/transactions/new")
def add_manual_transaction(
    account_id: int = Form(...),
    tx_date: str = Form(...),
    amount: float = Form(...),
    description: str = Form(""),
    merchant: str = Form(""),
    category_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    import uuid as _uuid
    from categorizer import categorize as _cat
    account = session.get(Account, account_id)
    if not account:
        return _redirect_flash("/transactions", "Conto non trovato", "error")
    merchant = merchant.strip() or None
    description = description.strip()
    cat_id = _opt_int(category_id)
    if not cat_id:
        cat_id = _cat(description, merchant, session)
    tx_day = date.fromisoformat(tx_date)
    currency = (account.currency or "EUR").upper()
    eur_amount = None
    if currency != "EUR":
        try:
            eur_amount = _fx.convert_on(amount, currency, tx_day, session=session)
        except FxUnavailable:
            eur_amount = None
    tx = Transaction(
        account_id=account_id,
        external_id=f"manual_{_uuid.uuid4().hex}",
        date=tx_day,
        amount=amount,
        currency=currency,
        eur_amount=eur_amount,
        description=description,
        merchant=merchant,
        category_id=cat_id,
        raw_data="",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(tx)
    # Update balance snapshot
    snap = session.exec(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account_id)
        .order_by(BalanceSnapshot.date.desc())
    ).first()
    current_balance = snap.balance if snap else 0.0
    new_balance = current_balance + amount
    if snap and snap.date == date.today():
        snap.balance = new_balance
    else:
        session.add(BalanceSnapshot(account_id=account_id, date=date.today(), balance=new_balance))
    session.commit()
    return RedirectResponse("/transactions?msg=Transazione+aggiunta", status_code=303)


# ── Sync endpoint ─────────────────────────────────────────────────────────────

@app.post("/transactions/detect-transfers")
def detect_transfers(session: Session = Depends(get_session)):
    transfer_cat = session.exec(select(Category).where(Category.name == "Trasferimento")).first()
    if not transfer_cat:
        return RedirectResponse("/transactions?msg=Categoria+Trasferimento+non+trovata", status_code=303)

    # Candidate transactions: not already linked, not already transfer category
    # (uncategorized rows included: `NULL != x` alone would drop them)
    from sqlalchemy import or_
    candidates = session.exec(
        select(Transaction).where(
            Transaction.transfer_partner_id == None,
            or_(Transaction.category_id == None, Transaction.category_id != transfer_cat.id),
        )
    ).all()

    # Group by (date, currency, abs_amount); pending entries are skipped since
    # their amount/date may still change.
    by_key: dict = defaultdict(list)
    for tx in candidates:
        if tx.status == "PDNG":
            continue
        key = (tx.date, (tx.currency or "EUR").upper(), round(abs(tx.amount), 2))
        by_key[key].append(tx)

    matched = 0
    for txs in by_key.values():
        positives = [t for t in txs if t.amount > 0]
        negatives = [t for t in txs if t.amount < 0]
        for pos, neg in zip(positives, negatives):
            # Must be different accounts
            if pos.account_id == neg.account_id:
                continue
            pos.transfer_partner_id = neg.id
            neg.transfer_partner_id = pos.id
            pos.category_id = transfer_cat.id
            neg.category_id = transfer_cat.id
            matched += 1

    session.commit()
    return RedirectResponse(f"/transactions?msg={matched}+trasferimenti+rilevati", status_code=303)


@app.post("/transactions/detect-prelievi")
def detect_prelievi(
    cash_account_id: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    if cash_account_id:
        cash_acc = session.get(Account, cash_account_id)
    else:
        cash_accs = session.exec(
            select(Account).where(Account.type == "cash", Account.deleted == False)
        ).all()
        if len(cash_accs) != 1:
            return RedirectResponse("/transactions?msg=Seleziona+conto+contante", status_code=303)
        cash_acc = cash_accs[0]

    if not cash_acc:
        return RedirectResponse("/transactions?msg=Conto+contante+non+trovato", status_code=303)

    transfer_cat = session.exec(select(Category).where(Category.name == "Trasferimento")).first()
    prelievo_cat = session.exec(select(Category).where(Category.name == "Prelievo ATM")).first()
    if not transfer_cat or not prelievo_cat:
        return RedirectResponse("/transactions?msg=Categorie+mancanti", status_code=303)

    prelievi = session.exec(
        select(Transaction).where(
            Transaction.category_id == prelievo_cat.id,
            Transaction.transfer_partner_id == None,
            Transaction.amount < 0,
        )
    ).all()

    # Only withdrawals dated on/after the cash account's first snapshot adjust
    # today's balance: older ones predate the tracked balance (which already
    # reflects them) and are only categorized/linked. With no snapshot at all
    # the balance starts today, so nothing historical is added.
    first_snap = session.exec(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == cash_acc.id)
        .order_by(BalanceSnapshot.date)
    ).first()
    cutoff = first_snap.date if first_snap else date.today()

    import uuid as _uuid
    created = 0
    balance_delta = 0.0
    for tx in prelievi:
        cash_tx = Transaction(
            account_id=cash_acc.id,
            external_id=f"prelievo_{tx.id}_{_uuid.uuid4().hex[:8]}",
            date=tx.date,
            amount=abs(tx.amount),
            currency=tx.currency,
            description=f"Prelievo da {tx.description or 'banca'}",
            category_id=transfer_cat.id,
            raw_data="",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(cash_tx)
        session.flush()

        tx.transfer_partner_id = cash_tx.id
        tx.category_id = transfer_cat.id
        cash_tx.transfer_partner_id = tx.id

        if tx.date >= cutoff:
            balance_delta += abs(tx.amount)
        created += 1

    if balance_delta:
        snap = session.exec(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == cash_acc.id)
            .order_by(BalanceSnapshot.date.desc())
        ).first()
        current = snap.balance if snap else 0.0
        new_bal = current + balance_delta
        if snap and snap.date == date.today():
            snap.balance = new_bal
        else:
            session.add(BalanceSnapshot(account_id=cash_acc.id, date=date.today(), balance=new_bal))

    session.commit()
    return RedirectResponse(f"/transactions?msg={created}+prelievi+collegati", status_code=303)


@app.post("/sync")
def trigger_sync(request: Request):
    # HTMX callers get a text fragment (+ HX-Redirect when there is something
    # to show); plain form posts (setup "Forza sync completo") get a real
    # redirect so the browser does not land on a bare text page.
    is_htmx = bool(request.headers.get("HX-Request"))

    def _plain(text: str, type_: str = "info", link: Optional[str] = None):
        if is_htmx:
            resp = HTMLResponse(text)
            if link:
                resp.headers["HX-Redirect"] = link
            return resp
        if link:
            return RedirectResponse(link, status_code=303)
        return _redirect_flash("/setup", text, type_)

    try:
        sync_start = datetime.now(timezone.utc).replace(tzinfo=None)
        sync_all()
        with Session(engine) as s:
            new_count = len(s.exec(select(Transaction).where(Transaction.created_at >= sync_start)).all())
            failed = s.exec(
                select(Account).where(Account.sync_error != None, Account.connected == True, Account.session_id != "manual")
            ).all()
        if failed:
            names = ", ".join(a.display_name or a.bank_name for a in failed)
            suffix = f" ({new_count} nuove)" if new_count > 0 else ""
            return _plain(f'✗ Errore: {names}{suffix}', "error")
        if new_count > 0:
            word = "nuova" if new_count == 1 else "nuove"
            link = f"/transactions?new_since={sync_start.isoformat()}"
            return _plain(f'✓ {new_count} {word}', link=link)
        return _plain('✓ Sync completato')
    except Exception as e:
        return _plain(f'✗ {e}', "error")


@app.post("/sync/{account_id}")
def trigger_sync_one(account_id: int, session: Session = Depends(get_session)):
    try:
        acc = session.get(Account, account_id)
        if not acc:
            return HTMLResponse('✗ Conto non trovato')
        sync_start = datetime.now(timezone.utc).replace(tzinfo=None)
        with Session(engine) as sync_session:
            acc2 = sync_session.get(Account, account_id)
            if acc2:
                sync_account(acc2, sync_session)
        with Session(engine) as s:
            new_count = len(s.exec(
                select(Transaction)
                .where(Transaction.account_id == account_id, Transaction.created_at >= sync_start)
            ).all())
        label = acc.display_name or acc.name
        with Session(engine) as s:
            acc_fresh = s.get(Account, account_id)
        if acc_fresh and acc_fresh.sync_error:
            return HTMLResponse(f'✗ {label}: {acc_fresh.sync_error}')
        if new_count > 0:
            word = "nuova" if new_count == 1 else "nuove"
            link = f"/transactions?new_since={sync_start.isoformat()}"
            resp = HTMLResponse(f'✓ {label}: {new_count} {word}')
            resp.headers["HX-Redirect"] = link
            return resp
        return HTMLResponse(f'✓ {label}: sync ok')
    except Exception as e:
        return HTMLResponse(f'✗ {e}')


@app.get("/sync/accounts")
def sync_accounts_dropdown(session: Session = Depends(get_session)):
    accounts = session.exec(
        select(Account).where(
            Account.connected == True,
            Account.deleted == False,
            Account.session_id != "manual",
        )
    ).all()
    items = "".join(
        f'<button onclick="window.dispatchEvent(new CustomEvent(\'sync-run\',{{detail:{{accountId:{acc.id}}}}}))" '
        f'class="sync-dropdown-item">{acc.display_name or acc.name}<span class="sync-dropdown-bank">{acc.bank_name}</span></button>'
        for acc in accounts
    )
    return HTMLResponse(items or '<span style="padding:8px 12px;color:var(--text-muted);font-size:0.75rem">Nessun conto connesso</span>')


@app.get("/sync/list")
def sync_list(account_id: Optional[int] = None, session: Session = Depends(get_session)):
    """Accounts to sync, for the blocking progress modal. Optional single account."""
    q = select(Account).where(
        Account.connected == True,
        Account.deleted == False,
        Account.session_id != "manual",
    )
    if account_id is not None:
        q = q.where(Account.id == account_id)
    accounts = session.exec(q).all()
    since = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return JSONResponse({
        "since": since,
        "accounts": [
            {"id": a.id, "label": a.display_name or a.name, "bank": a.bank_name}
            for a in accounts
        ],
    })


@app.post("/sync/run/{account_id}")
def sync_run_one(account_id: int, since: str):
    """Sync a single account, return JSON progress result (no redirect)."""
    sync_start = datetime.fromisoformat(since)
    label = str(account_id)
    try:
        with Session(engine) as sync_session:
            acc = sync_session.get(Account, account_id)
            if not acc:
                return JSONResponse({"ok": False, "label": label, "error": "Conto non trovato"})
            label = acc.display_name or acc.name
            sync_account(acc, sync_session)
        with Session(engine) as s:
            new_count = len(s.exec(
                select(Transaction)
                .where(Transaction.account_id == account_id, Transaction.created_at >= sync_start)
            ).all())
            acc_fresh = s.get(Account, account_id)
        if acc_fresh and acc_fresh.sync_error:
            return JSONResponse({"ok": False, "label": label, "error": acc_fresh.sync_error})
        return JSONResponse({"ok": True, "label": label, "new_count": new_count})
    except Exception as e:
        return JSONResponse({"ok": False, "label": label, "error": str(e)})


@app.post("/admin/fix-dates")
def fix_transaction_dates(session: Session = Depends(get_session)):
    """One-shot: reparse booking_date from raw_data for all transactions."""
    from datetime import date as date_type
    txs = session.exec(select(Transaction)).all()
    updated = 0
    skipped = 0
    for tx in txs:
        if not tx.raw_data:
            skipped += 1
            continue
        try:
            raw = json.loads(tx.raw_data)
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        date_str = raw.get("transaction_date") or raw.get("booking_date") or raw.get("value_date")
        if not date_str:
            skipped += 1
            continue
        new_date = date_type.fromisoformat(date_str)
        if tx.date != new_date:
            tx.date = new_date
            updated += 1
    session.commit()
    return {"updated": updated, "skipped": skipped, "total": len(txs)}


@app.post("/admin/backfill-eur-amounts")
def backfill_eur_amounts(session: Session = Depends(get_session)):
    """One-shot: compute eur_amount for existing non-EUR transactions that lack it."""
    txs = session.exec(
        select(Transaction).where(Transaction.currency != "EUR", Transaction.eur_amount == None)
    ).all()
    updated = 0
    failed = 0
    for tx in txs:
        try:
            tx.eur_amount = _fx.convert_on(tx.amount, tx.currency, tx.date, session=session)
            updated += 1
        except Exception:  # FxUnavailable or anything else: leave eur_amount None
            failed += 1
    session.commit()
    return {"updated": updated, "failed": failed, "total": len(txs)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
