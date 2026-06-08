import json
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
    Instrument, engine, init_db, get_session,
)
from sync import build_auth_url, handle_callback, sync_all, sync_account
from investments import router as investments_router, _build_portfolio_data
import fx as _fx

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _enrich_tx(tx: Transaction, session: Session) -> SimpleNamespace:
    ns = SimpleNamespace(**{k: v for k, v in vars(tx).items() if not k.startswith('_')})
    ns.category = session.get(Category, tx.category_id)
    ns.account  = session.get(Account, tx.account_id)
    return ns


def _effective_amount(tx, session: Session = None) -> float:
    """Expense amount in EUR, respecting personal_share and FX conversion."""
    amount = tx.amount
    cur = getattr(tx, 'currency', 'EUR') or 'EUR'
    if cur != "EUR":
        eur_amount = getattr(tx, 'eur_amount', None)
        if eur_amount is not None:
            ps = getattr(tx, 'personal_share', None)
            if amount < 0 and ps is not None and amount != 0:
                return -(eur_amount * abs(ps / amount))
            return eur_amount
        if session:
            tx_date = getattr(tx, 'date', None)
            if tx_date:
                amount = _fx.convert_on(amount, cur, tx_date, session=session)
            else:
                amount = _fx.convert(amount, cur, session=session)
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
                eur = _fx.convert(native, currency, session=session)
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
    liquidity_accounts = {
        a.id: a for a in session.exec(
            select(Account).where(Account.connected == True, Account.type.in_(("checking", "savings", "cash")))
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
            last_snap = None
            for s in by_account.get(acc_id, []):
                if s.date <= d:
                    last_snap = s
                else:
                    break
            if last_snap is not None:
                has_any = True
                balance = last_snap.balance
                if acc.currency and acc.currency != "EUR":
                    balance = _fx.convert(balance, acc.currency, session=session)
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
        liquidity_etf = sum(
            (p.market_value or p.total_invested)
            for p in portfolio.positions if p.instrument_id in liquidity_ids
        )
        full_value = portfolio.total_market_value if portfolio.total_market_value is not None else portfolio.total_invested
        portfolio_value = full_value - liquidity_etf
        portfolio_pl = portfolio.total_unrealized_pl
        portfolio_pl_pct = portfolio.total_unrealized_pl_pct
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

    # Weekly aggregation (4 weeks)
    weekly_in, weekly_out, weekly_labels = [], [], []
    for week in range(4):
        w_start = first_day + timedelta(weeks=week)
        w_end   = min(w_start + timedelta(days=6), last_day)
        w_txs   = [tx for tx in transactions if w_start <= tx.date <= w_end and not _is_transfer(tx)]
        weekly_in.append(round(sum(_effective_amount(t, session) for t in w_txs if t.amount > 0 and not t.is_reimbursement), 2))
        weekly_out.append(round(abs(sum(_effective_amount(t, session) for t in w_txs if t.amount < 0)), 2))
        weekly_labels.append(f"Sett {week+1}")

    # Category breakdown (expenses only, with budget)
    cat_totals: dict[str, float] = defaultdict(float)
    cat_id_map: dict[str, int] = {}
    cat_colors_map: dict[str, str] = {}
    for tx in transactions:
        if tx.amount < 0 and tx.category and not _is_transfer(tx):
            cat_totals[tx.category.name] += abs(_effective_amount(tx, session))
            cat_colors_map[tx.category.name] = tx.category.color
            cat_id_map[tx.category.name] = tx.category.id

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
            "budget": budget_amount,
            "pct": min(round(total / budget_amount * 100), 100) if budget_amount else None,
        })
    cat_rows.sort(key=lambda r: r["total"], reverse=True)

    cat_labels = [r["name"] for r in cat_rows]
    cat_data   = [r["total"] for r in cat_rows]
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
    from_: Optional[str] = None,
    to_: Optional[str] = None,
    tx_type: Optional[str] = Query(default=None, alias="type"),
    search: Optional[str] = None,
    new_since: Optional[str] = None,
    page: int = 1,
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

    return templates.TemplateResponse("transactions.html", {
        "request": request,
        "transactions": txs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "categories": _categories_by_frequency(session),
        "accounts": session.exec(select(Account).where(Account.connected == True)).all(),
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
        "new_since": new_since,
        "new_since_dt": new_since_dt,
        "cash_accounts": session.exec(
            select(Account).where(Account.type == "cash", Account.deleted == False)
        ).all(),
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


@app.post("/transactions/{tx_id}/share")
def update_share(
    tx_id: int,
    personal_share: Optional[float] = Form(default=None),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx:
        tx.personal_share = personal_share if personal_share and personal_share > 0 else None
        session.commit()
    return RedirectResponse(redirect_to, status_code=303)


# ── Budgets ───────────────────────────────────────────────────────────────────

@app.get("/budgets", response_class=HTMLResponse)
def budgets_view(request: Request, session: Session = Depends(get_session)):
    categories = session.exec(select(Category).where(Category.type.in_(["expense", "both"]))).all()
    budgets = {b.category_id: b for b in session.exec(select(Budget)).all()}
    return templates.TemplateResponse("budgets.html", {
        "request": request,
        "categories": categories,
        "budgets": budgets,
    })


@app.post("/budgets/save")
def save_budget(
    category_id: int = Form(...),
    amount: float = Form(...),
    session: Session = Depends(get_session),
):
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
def categories_view(request: Request, session: Session = Depends(get_session)):
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
    return templates.TemplateResponse("categories.html", {
        "request": request,
        "categories": categories,
        "rules_by_cat": dict(rules_by_cat),
        "merchants_by_cat": dict(merchants_by_cat),
        "all_merchants": merchants,
        "cat_map": cat_map,
    })


@app.post("/categories/add")
def add_category(
    name: str = Form(...),
    cat_type: str = Form(default="expense"),
    color: str = Form(default="#6B7280"),
    icon: str = Form(default="💳"),
    session: Session = Depends(get_session),
):
    session.add(Category(name=name.strip(), type=cat_type, color=color, icon=icon))
    session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{cat_id}/delete")
def delete_category(cat_id: int, session: Session = Depends(get_session)):
    has_txs = session.exec(select(Transaction).where(Transaction.category_id == cat_id)).first()
    if not has_txs:
        for rule in session.exec(select(CategoryRule).where(CategoryRule.category_id == cat_id)).all():
            session.delete(rule)
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


@app.post("/transactions/{tx_id}/merchant-category")
def assign_merchant_category(
    tx_id: int,
    request: Request,
    category_id: int = Form(...),
    redirect_to: str = Form(default="/transactions"),
    session: Session = Depends(get_session),
):
    tx = session.get(Transaction, tx_id)
    if tx and tx.merchant:
        key = tx.merchant.lower()
        existing = session.exec(
            select(MerchantCategory).where(MerchantCategory.merchant == key)
        ).first()
        if existing:
            existing.category_id = category_id
        else:
            session.add(MerchantCategory(merchant=key, category_id=category_id))
        all_txs = session.exec(
            select(Transaction).where(func.lower(Transaction.merchant) == key)
        ).all()
        for t in all_txs:
            t.category_id = category_id
        session.commit()
    if request.headers.get("X-Fetch"):
        cat = session.get(Category, category_id)
        color = cat.color if cat else "#6b6b88"
        return JSONResponse({"color": color, "name": cat.name if cat else "Altro", "icon": cat.icon if cat else "❓", "text_color": _tag_text_color(color)})
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
    updated = 0
    for mc in mappings:
        txs = session.exec(
            select(Transaction).where(func.lower(Transaction.merchant) == mc.merchant)
        ).all()
        for tx in txs:
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
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "accounts": accounts,
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
    return RedirectResponse("/setup?msg=Conto+rimosso", status_code=303)


# ── Manual accounts & transactions ───────────────────────────────────────────

@app.post("/setup/account/manual")
def create_manual_account(
    name: str = Form(...),
    bank_name: str = Form(...),
    acc_type: str = Form("checking"),
    currency: str = Form("EUR"),
    initial_balance: float = Form(0.0),
    session: Session = Depends(get_session),
):
    import uuid as _uuid
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
    threshold: Optional[float] = Form(None),
    session: Session = Depends(get_session),
):
    acc = session.get(Account, account_id)
    if acc:
        acc.balance_threshold = threshold if threshold is not None and threshold >= 0 else None
        session.commit()
    return RedirectResponse("/setup?msg=Soglia+aggiornata", status_code=303)


@app.post("/setup/account/{account_id}/balance")
def update_manual_balance(
    account_id: int,
    balance: float = Form(...),
    session: Session = Depends(get_session),
):
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
    category_id: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    import uuid as _uuid
    from categorizer import categorize as _cat
    merchant = merchant.strip() or None
    description = description.strip()
    if not category_id:
        category_id = _cat(description, merchant, session)
    tx = Transaction(
        account_id=account_id,
        external_id=f"manual_{_uuid.uuid4().hex}",
        date=date.fromisoformat(tx_date),
        amount=amount,
        currency="EUR",
        description=description,
        merchant=merchant,
        category_id=category_id,
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
    candidates = session.exec(
        select(Transaction).where(
            Transaction.transfer_partner_id == None,
            Transaction.category_id != transfer_cat.id,
        )
    ).all()

    # Group by (date, abs_amount)
    from collections import defaultdict
    by_key: dict = defaultdict(list)
    for tx in candidates:
        key = (tx.date, round(abs(tx.amount), 2))
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

    import uuid as _uuid
    created = 0
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

        snap = session.exec(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == cash_acc.id)
            .order_by(BalanceSnapshot.date.desc())
        ).first()
        current = snap.balance if snap else 0.0
        new_bal = current + abs(tx.amount)
        if snap and snap.date == date.today():
            snap.balance = new_bal
        else:
            session.add(BalanceSnapshot(account_id=cash_acc.id, date=date.today(), balance=new_bal))

        created += 1

    session.commit()
    return RedirectResponse(f"/transactions?msg={created}+prelievi+collegati", status_code=303)


@app.post("/sync")
def trigger_sync():
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
            return HTMLResponse(f'✗ Errore: {names}{suffix}')
        if new_count > 0:
            word = "nuova" if new_count == 1 else "nuove"
            link = f"/transactions?new_since={sync_start.isoformat()}"
            resp = HTMLResponse(f'✓ {new_count} {word}')
            resp.headers["HX-Redirect"] = link
            return resp
        return HTMLResponse('✓ Sync completato')
    except Exception as e:
        return HTMLResponse(f'✗ {e}')


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
        f'<button hx-post="/sync/{acc.id}" hx-target="#sync-status" hx-swap="innerHTML" '
        f'hx-on::before-request="document.getElementById(\'sync-spinner\').style.animation=\'spin 1s linear infinite\'; document.getElementById(\'sync-btn\').disabled=true; $el.closest(\'[x-data]\').open=false" '
        f'hx-on::after-request="document.getElementById(\'sync-spinner\').style.animation=\'\'; document.getElementById(\'sync-btn\').disabled=false; var t=event.detail.xhr.responseText; if(t&&t.trim().startsWith(\'✗\')) showToast(t.trim().slice(2).trim(),\'error\')" '
        f'class="sync-dropdown-item">{acc.display_name or acc.name}<span class="sync-dropdown-bank">{acc.bank_name}</span></button>'
        for acc in accounts
    )
    return HTMLResponse(items or '<span style="padding:8px 12px;color:var(--text-muted);font-size:0.75rem">Nessun conto connesso</span>')


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
        except Exception:
            failed += 1
    session.commit()
    return {"updated": updated, "failed": failed, "total": len(txs)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
