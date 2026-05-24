import base64
import json
import os
import secrets
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, select

import config
import scheduler
from database import (
    Account, Transaction, BalanceSnapshot, Category, CategoryRule, MerchantCategory, Budget,
    engine, init_db, get_session,
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

_AUTH_USER = os.environ.get("APP_USER", "")
_AUTH_PASS = os.environ.get("APP_PASS", "")

_UNAUTH = Response(
    content="Unauthorized",
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="Ledger"'},
)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if _AUTH_USER and _AUTH_PASS:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return _UNAUTH
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, pwd = decoded.partition(":")
        except Exception:
            return _UNAUTH
        ok = secrets.compare_digest(user, _AUTH_USER) and secrets.compare_digest(pwd, _AUTH_PASS)
        if not ok:
            return _UNAUTH
    return await call_next(request)

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
    if cur != "EUR" and session:
        tx_date = getattr(tx, 'date', None)
        if tx_date:
            amount = _fx.convert_on(amount, cur, tx_date, session=session)
        else:
            amount = _fx.convert(amount, cur, session=session)
    if amount < 0 and getattr(tx, 'personal_share', None) is not None:
        return -tx.personal_share
    return amount


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

def _networth_series(session: Session, days: int = 90) -> tuple[list[str], list[float]]:
    start = CHART_START_DATE
    liquidity_accounts = {
        a.id: a for a in session.exec(
            select(Account).where(Account.connected == True, Account.type.in_(("checking", "savings")))
        ).all()
    }
    if not liquidity_accounts:
        return [], []
    snaps = session.exec(
        select(BalanceSnapshot).where(
            BalanceSnapshot.date >= start,
            BalanceSnapshot.account_id.in_(list(liquidity_accounts.keys()))
        )
    ).all()
    by_date: dict[date, float] = defaultdict(float)
    for s in snaps:
        acc = liquidity_accounts[s.account_id]
        balance = s.balance
        if acc.currency and acc.currency != "EUR":
            balance = _fx.convert(balance, acc.currency, session=session)
        by_date[s.date] += balance
    dates = sorted(by_date)
    return (
        [d.strftime("%d/%m") for d in dates],
        [round(by_date[d], 2) for d in dates],
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    accounts = session.exec(select(Account).where(Account.connected == True)).all()
    balances = _balances_by_account(session)
    labels, networth_data = _networth_series(session)

    liquidity = sum(
        balances.get(a.id, {"eur": 0})["eur"] for a in accounts if a.type in ("checking", "savings")
    )
    bank_investments = sum(
        balances.get(a.id, {"eur": 0})["eur"] for a in accounts if a.type == "investment"
    )

    try:
        portfolio = _build_portfolio_data(session)
        portfolio_value = portfolio.total_market_value if portfolio.total_market_value is not None else portfolio.total_invested
        portfolio_pl = portfolio.total_unrealized_pl
        portfolio_pl_pct = portfolio.total_unrealized_pl_pct
    except Exception:
        portfolio_value = 0.0
        portfolio_pl = None
        portfolio_pl_pct = None

    investments_total = bank_investments + portfolio_value
    net_worth = liquidity + investments_total

    acc_with_balance = sorted(
        [(a, balances.get(a.id, {"eur": 0.0, "native": 0.0, "currency": "EUR"})) for a in accounts],
        key=lambda x: (x[0].session_id == "manual", -x[1]["eur"])
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "liquidity": liquidity,
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

    transactions = [tx for tx in all_transactions if not _is_transfer(tx) and tx.amount != 0]
    if tx_type == "in":
        transactions = [tx for tx in transactions if tx.amount > 0]
    elif tx_type == "out":
        transactions = [tx for tx in transactions if tx.amount < 0]

    total_in  = sum(_effective_amount(tx, session) for tx in transactions if tx.amount > 0 and not _is_transfer(tx))
    total_out = abs(sum(_effective_amount(tx, session) for tx in transactions if tx.amount < 0 and not _is_transfer(tx)))
    balance   = total_in - total_out

    # Weekly aggregation (4 weeks)
    weekly_in, weekly_out, weekly_labels = [], [], []
    for week in range(4):
        w_start = first_day + timedelta(weeks=week)
        w_end   = min(w_start + timedelta(days=6), last_day)
        w_txs   = [tx for tx in transactions if w_start <= tx.date <= w_end and not _is_transfer(tx)]
        weekly_in.append(round(sum(_effective_amount(t, session) for t in w_txs if t.amount > 0), 2))
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

    total = len(all_txs)
    txs = [_enrich_tx(tx, session) for tx in all_txs[(page-1)*page_size : page*page_size]]

    qs_parts = {k: v for k, v in {"cat": cat, "account": account, "from": from_, "to": to_, "type": tx_type, "search": search}.items() if v}
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
        "manual_accounts": session.exec(select(Account).where(Account.session_id == "manual")).all(),
        "today": date.today().isoformat(),
        "selected_cat": cat or "",
        "selected_account": account or "",
        "selected_type": tx_type or "",
        "date_from": from_,
        "date_to": to_,
        "search": search or "",
        "query_string": query_string,
        "mapped_merchants": mapped_merchants,
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
        session.commit()
    if request.headers.get("X-Fetch"):
        cat = session.get(Category, category_id)
        color = cat.color if cat else "#6b6b88"
        return JSONResponse({"color": color, "name": cat.name if cat else "Altro", "icon": cat.icon if cat else "❓", "text_color": _tag_text_color(color)})
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
    categories = session.exec(select(Category).where(Category.type == "expense")).all()
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
def setup(request: Request, session: Session = Depends(get_session), msg: str = ""):
    accounts = session.exec(select(Account)).all()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "accounts": accounts,
        "supported_banks": SUPPORTED_BANKS,
        "flash": {"message": msg, "type": "info"} if msg else None,
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
    if acc and type in ("checking", "savings", "investment"):
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
    return RedirectResponse("/setup?msg=Sync+completato", status_code=303)


@app.post("/setup/account/{account_id}/delete")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(Account, account_id)
    if acc:
        session.exec(select(Transaction).where(Transaction.account_id == account_id))
        for tx in session.exec(select(Transaction).where(Transaction.account_id == account_id)).all():
            session.delete(tx)
        for snap in session.exec(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id)).all():
            session.delete(snap)
        session.delete(acc)
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


@app.post("/sync")
def trigger_sync():
    try:
        sync_all()
        return HTMLResponse('✓ Sync completato')
    except Exception as e:
        return HTMLResponse(f'✗ {e}')


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
