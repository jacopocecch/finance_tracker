import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from database import (
    Instrument, PAC, PACComponent, InvestmentTransaction, MarketQuote,
    engine, get_session,
)
from market_data import refresh_quote, refresh_all_quotes, latest_quote
from portfolio import compute_position, compute_pac_position, compute_portfolio
import fx as _fx

log = logging.getLogger(__name__)

router = APIRouter(prefix="/investments")
templates = Jinja2Templates(directory="templates")

# Mirrors main.py's filter (separate Jinja environment; main imports this module).
_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CHF": "CHF",
                     "SEK": "kr", "NOK": "kr", "DKK": "kr", "PLN": "zł", "CZK": "Kč",
                     "HUF": "Ft", "RON": "lei", "TRY": "₺", "CNY": "¥", "HKD": "HK$",
                     "SGD": "S$", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "MXN": "MX$"}


def _currency_symbol(code: str) -> str:
    return _CURRENCY_SYMBOLS.get((code or "EUR").upper(), code or "€")


templates.env.filters["currency_symbol"] = _currency_symbol

TRANSACTION_TYPES = ("BUY", "SELL")
INSTRUMENT_TYPES = ("ETF", "ETC", "Fondo")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _flash(msg: str = "", err: str = "") -> Optional[dict]:
    """Build the `flash` context consumed by base.html from ?msg= / ?err=."""
    if err:
        return {"message": err, "type": "error"}
    if msg:
        return {"message": msg, "type": "info"}
    return None


def _price_in_inst_currency(quote, inst, session: Session) -> Optional[float]:
    """Latest quote price expressed in the instrument's reporting currency.
    Returns None when there is no quote or the FX rate is unavailable."""
    if quote is None or quote.price is None:
        return None
    if quote.currency and quote.currency != inst.currency:
        try:
            return _fx.convert(quote.price, quote.currency, inst.currency, session)
        except _fx.FxUnavailable as e:
            log.warning("Quote for %s not convertible %s→%s: %s", inst.ticker, quote.currency, inst.currency, e)
            return None
    return quote.price


def _rate_to_eur(currency: str, session: Session) -> Optional[float]:
    if currency == "EUR":
        return 1.0
    try:
        return _fx.get_rate(currency, "EUR", session)
    except _fx.FxUnavailable as e:
        log.warning("FX %s→EUR unavailable: %s", currency, e)
        return None


def _txs_in_inst_currency(txs, inst, session: Session) -> tuple[list, bool]:
    """Return lightweight copies of the transactions with unit_price/fees
    converted to the instrument currency using the trade-date rate.
    The second element is False when at least one conversion failed
    (the raw value is kept and the position should be flagged stale)."""
    out = []
    ok = True
    for tx in txs:
        price, fees = tx.unit_price, tx.fees
        if tx.currency and tx.currency != inst.currency:
            try:
                rate = _fx.get_rate_on(tx.currency, tx.trade_date, inst.currency, session)
                price, fees = tx.unit_price * rate, tx.fees * rate
            except _fx.FxUnavailable as e:
                log.warning("Tx %s: FX %s→%s on %s unavailable: %s", tx.id, tx.currency, inst.currency, tx.trade_date, e)
                ok = False
        out.append(SimpleNamespace(
            id=tx.id,
            instrument_id=tx.instrument_id,
            transaction_type=tx.transaction_type,
            trade_date=tx.trade_date,
            quantity=tx.quantity,
            unit_price=price,
            fees=fees,
            pac_id=tx.pac_id,
        ))
    return out, ok


def _instrument_transactions(instrument_id: int, session: Session):
    return session.exec(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.instrument_id == instrument_id)
        .order_by(InvestmentTransaction.trade_date, InvestmentTransaction.id)
    ).all()


def _compute_instrument_position(inst, txs, session: Session):
    """Position for `inst` from its (chronological) transactions, all figures
    in the instrument currency plus EUR fields. Returns (position, last_price)."""
    quote = latest_quote(inst.id, session)
    price = _price_in_inst_currency(quote, inst, session)
    conv_txs, fx_ok = _txs_in_inst_currency(txs, inst, session)
    pos = compute_position(
        instrument_id=inst.id,
        name=inst.name,
        isin=inst.isin,
        ticker=inst.ticker,
        currency=inst.currency,
        transactions=conv_txs,
        last_price=price,
        is_stale=(quote.is_stale if quote else True) or not fx_ok,
        quote_timestamp=quote.quote_timestamp if quote else None,
        fx_rate_to_eur=_rate_to_eur(inst.currency, session),
    )
    return pos, price


def _build_portfolio_data(session: Session):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()

    positions = []
    last_prices: dict[int, float] = {}
    inst_by_id = {inst.id: inst for inst in instruments}

    for inst in instruments:
        txs = _instrument_transactions(inst.id, session)
        if not txs:
            continue
        pos, price = _compute_instrument_position(inst, txs, session)
        if price is not None:
            last_prices[inst.id] = price
        positions.append(pos)

    positions.sort(
        key=lambda p: (p.is_open, p.market_value_eur or p.total_invested_eur or p.market_value or p.total_invested),
        reverse=True,
    )

    pac_positions = []
    for pac in pacs:
        pac_txs = session.exec(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.pac_id == pac.id)
            .order_by(InvestmentTransaction.trade_date, InvestmentTransaction.id)
        ).all()
        if not pac_txs:
            continue
        pac_positions.append(_compute_pac(pac, pac_txs, inst_by_id, last_prices, session))

    return compute_portfolio(positions, pac_positions)


def _compute_pac(pac, pac_txs, inst_by_id: dict, last_prices: dict, session: Session):
    """PAC position with each instrument's transactions converted to that
    instrument's currency (consistent with `last_prices`)."""
    conv = []
    for iid in {tx.instrument_id for tx in pac_txs}:
        inst = inst_by_id.get(iid) or session.get(Instrument, iid)
        txs = [tx for tx in pac_txs if tx.instrument_id == iid]
        if inst is None:
            conv.extend(txs)
            continue
        c, _ = _txs_in_inst_currency(txs, inst, session)
        conv.extend(c)
    return compute_pac_position(pac.id, pac.name, conv, last_prices)


def _get_instrument_or_404(instrument_id: int, session: Session):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        raise ValueError("Strumento non trovato")
    return inst


# ── Overview ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def overview(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    summary = _build_portfolio_data(session)
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()

    liquidity_ids = {
        inst.id for inst in session.exec(
            select(Instrument).where(Instrument.is_liquidity == True, Instrument.active == True)
        ).all()
    }
    inv_positions = [p for p in summary.positions if p.instrument_id not in liquidity_ids]
    liq_positions = [p for p in summary.positions if p.instrument_id in liquidity_ids and p.is_open]
    inv_summary = compute_portfolio(inv_positions, summary.pac_positions)

    open_inv = [p for p in inv_positions if p.is_open]
    closed_positions = [p for p in inv_positions if not p.is_open]
    chart_labels = [p.name for p in open_inv]
    chart_invested = [p.total_invested_eur or 0.0 for p in open_inv]
    chart_market = [
        (p.market_value_eur if p.market_value_eur is not None else p.total_invested_eur) or 0.0
        for p in open_inv
    ]

    return templates.TemplateResponse("investments/overview.html", {
        "request": request,
        "summary": inv_summary,
        "open_positions": open_inv,
        "closed_positions": closed_positions,
        "liq_positions": liq_positions,
        "pacs": pacs,
        "chart_labels": chart_labels,
        "chart_invested": chart_invested,
        "chart_market": chart_market,
        "flash": _flash(msg, err),
    })


# ── Quotes ───────────────────────────────────────────────────────────────────

@router.post("/quotes/refresh")
def refresh_quotes_all(session: Session = Depends(get_session)):
    res = refresh_all_quotes(session)
    msg = f"Quotazioni aggiornate: {res['success']} ok, {res['failed']} fallite"
    return RedirectResponse(f"/investments?msg={msg.replace(' ', '+')}", status_code=303)


@router.post("/quotes/{instrument_id}/refresh")
def refresh_quote_one(instrument_id: int, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if inst:
        ok = refresh_quote(inst, session)
        if not ok:
            return RedirectResponse(
                f"/investments/instruments/{instrument_id}?err=Quotazione+non+disponibile", status_code=303
            )
    return RedirectResponse(f"/investments/instruments/{instrument_id}", status_code=303)


@router.post("/instruments/{instrument_id}/toggle-liquidity")
def toggle_liquidity(instrument_id: int, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if inst:
        inst.is_liquidity = not inst.is_liquidity
        session.add(inst)
        session.commit()
    return RedirectResponse(f"/investments/instruments/{instrument_id}", status_code=303)


# ── Instruments ───────────────────────────────────────────────────────────────

@router.get("/instruments", response_class=HTMLResponse)
def instruments_list(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    data = []
    for inst in instruments:
        quote = latest_quote(inst.id, session)
        txs = _instrument_transactions(inst.id, session)
        if txs:
            pos, _ = _compute_instrument_position(inst, txs, session)
            total_qty, total_invested = pos.total_quantity, pos.total_invested
        else:
            total_qty, total_invested = 0.0, 0.0
        data.append({
            "inst": inst,
            "quote": quote,
            "total_qty": total_qty,
            "total_invested": total_invested,
        })
    return templates.TemplateResponse("investments/instruments.html", {
        "request": request,
        "instruments": data,
        "flash": _flash(msg, err),
    })


@router.get("/instruments/add", response_class=HTMLResponse)
def instrument_add_form(request: Request, msg: str = "", err: str = ""):
    return templates.TemplateResponse("investments/instrument_form.html", {
        "request": request,
        "instrument": None,
        "title": "Nuovo strumento",
        "flash": _flash(msg, err),
    })


@router.post("/instruments/add")
def instrument_add(
    name: str = Form(...),
    isin: str = Form(...),
    ticker: str = Form(...),
    exchange: str = Form(""),
    currency: str = Form("EUR"),
    type_: str = Form("ETF", alias="type"),
    is_liquidity: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    isin = isin.strip().upper()
    ticker = ticker.strip()
    if type_ not in INSTRUMENT_TYPES:
        type_ = "ETF"
    existing = session.exec(select(Instrument).where(Instrument.isin == isin)).first()
    if existing:
        return RedirectResponse(f"/investments/instruments/{existing.id}?msg=ISIN+già+presente", status_code=303)
    inst = Instrument(
        name=name.strip(),
        isin=isin,
        ticker=ticker,
        exchange=exchange.strip(),
        currency=currency.strip().upper(),
        type=type_,
        is_liquidity=is_liquidity == "1",
    )
    session.add(inst)
    session.commit()
    session.refresh(inst)
    # Fetch initial quote
    refresh_quote(inst, session)
    return RedirectResponse(f"/investments/instruments/{inst.id}", status_code=303)


@router.get("/instruments/{instrument_id}", response_class=HTMLResponse)
def instrument_detail(
    instrument_id: int,
    request: Request,
    msg: str = "",
    err: str = "",
    session: Session = Depends(get_session),
):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        return RedirectResponse("/investments/instruments?err=Strumento+non+trovato", status_code=303)

    txs = _instrument_transactions(instrument_id, session)
    quote = latest_quote(instrument_id, session)
    pacs_map = {p.id: p for p in session.exec(select(PAC)).all()}

    pos = _compute_instrument_position(inst, txs, session)[0] if txs else None

    return templates.TemplateResponse("investments/instrument_detail.html", {
        "request": request,
        "inst": inst,
        "pos": pos,
        "quote": quote,
        "transactions": list(reversed(txs)),
        "pacs_map": pacs_map,
        "flash": _flash(msg, err),
    })


@router.get("/instruments/{instrument_id}/edit", response_class=HTMLResponse)
def instrument_edit_form(instrument_id: int, request: Request, msg: str = "", err: str = "",
                         session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        return RedirectResponse("/investments/instruments?err=Strumento+non+trovato", status_code=303)
    return templates.TemplateResponse("investments/instrument_form.html", {
        "request": request,
        "instrument": inst,
        "title": "Modifica strumento",
        "flash": _flash(msg, err),
    })


@router.post("/instruments/{instrument_id}/edit")
def instrument_edit(
    instrument_id: int,
    name: str = Form(...),
    ticker: str = Form(...),
    exchange: str = Form(""),
    currency: str = Form("EUR"),
    type_: str = Form("ETF", alias="type"),
    is_liquidity: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    inst = session.get(Instrument, instrument_id)
    if inst:
        inst.name = name.strip()
        inst.ticker = ticker.strip()
        inst.exchange = exchange.strip()
        inst.currency = currency.strip().upper()
        if type_ in INSTRUMENT_TYPES:
            inst.type = type_
        inst.is_liquidity = is_liquidity == "1"
        inst.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse(f"/investments/instruments/{instrument_id}?msg=Strumento+aggiornato", status_code=303)


@router.post("/instruments/{instrument_id}/delete")
def instrument_delete(instrument_id: int, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if inst:
        inst.active = False
        inst.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse("/investments/instruments?msg=Strumento+archiviato", status_code=303)


# ── Investment Transactions ──────────────────────────────────────────────────

@router.get("/transactions", response_class=HTMLResponse)
def inv_transactions_list(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    txs = session.exec(
        select(InvestmentTransaction).order_by(InvestmentTransaction.trade_date.desc())
    ).all()
    instruments = {i.id: i for i in session.exec(select(Instrument)).all()}
    pacs = {p.id: p for p in session.exec(select(PAC)).all()}
    return templates.TemplateResponse("investments/transactions.html", {
        "request": request,
        "transactions": txs,
        "instruments": instruments,
        "pacs": pacs,
        "flash": _flash(msg, err),
    })


@router.get("/transactions/add", response_class=HTMLResponse)
def inv_transaction_add_form(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    inst_map = {i.id: i for i in instruments}
    pac_components: dict = {}
    for pac in pacs:
        comps = session.exec(select(PACComponent).where(PACComponent.pac_id == pac.id)).all()
        pac_components[pac.id] = [
            {
                "instrument_id": c.instrument_id,
                "ticker": inst_map[c.instrument_id].ticker if c.instrument_id in inst_map else "?",
                "name": (inst_map[c.instrument_id].name[:48] + "…" if len(inst_map[c.instrument_id].name) > 48 else inst_map[c.instrument_id].name) if c.instrument_id in inst_map else "?",
                "target_weight": int(c.target_weight) if c.target_weight else 0,
            }
            for c in comps
        ]
    return templates.TemplateResponse("investments/transaction_form.html", {
        "request": request,
        "transaction": None,
        "instruments": instruments,
        "pacs": pacs,
        "pac_components": pac_components,
        "title": "Nuovo acquisto",
        "today": date.today().isoformat(),
        "flash": _flash(msg, err),
    })


@router.post("/transactions/add")
def inv_transaction_add(
    instrument_id: str = Form(""),
    # New instrument fields (used when instrument_id == "new")
    new_name: str = Form(""),
    new_isin: str = Form(""),
    new_ticker: str = Form(""),
    new_exchange: str = Form(""),
    new_currency: str = Form("EUR"),
    new_is_liquidity: Optional[str] = Form(None),
    # Transaction fields
    transaction_type: str = Form("BUY"),
    broker_name: str = Form("Fineco"),
    trade_date: str = Form(...),
    quantity: float = Form(...),
    unit_price: float = Form(...),
    fees: float = Form(0.0),
    currency: str = Form("EUR"),
    pac_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    # Validate
    if quantity <= 0:
        return RedirectResponse("/investments/transactions/add?err=Quantità+non+valida", status_code=303)
    if unit_price <= 0:
        return RedirectResponse("/investments/transactions/add?err=Prezzo+non+valido", status_code=303)
    if fees < 0:
        return RedirectResponse("/investments/transactions/add?err=Commissioni+non+valide", status_code=303)
    if transaction_type not in TRANSACTION_TYPES:
        return RedirectResponse("/investments/transactions/add?err=Tipo+non+valido", status_code=303)
    try:
        tx_date = date.fromisoformat(trade_date)
    except ValueError:
        return RedirectResponse("/investments/transactions/add?err=Data+non+valida", status_code=303)

    # Resolve instrument
    instrument_id = (instrument_id or "").strip()
    if instrument_id == "new":
        isin = new_isin.strip().upper()
        if not isin or not new_ticker.strip() or not new_name.strip():
            return RedirectResponse("/investments/transactions/add?err=Campi+strumento+mancanti", status_code=303)
        existing = session.exec(select(Instrument).where(Instrument.isin == isin)).first()
        if existing:
            inst = existing
        else:
            inst = Instrument(
                name=new_name.strip(),
                isin=isin,
                ticker=new_ticker.strip(),
                exchange=new_exchange.strip(),
                currency=new_currency.strip().upper(),
                is_liquidity=new_is_liquidity == "1",
            )
            session.add(inst)
            session.flush()
            refresh_quote(inst, session)
        inst_id = inst.id
    else:
        if not instrument_id:
            return RedirectResponse("/investments/transactions/add?err=Strumento+mancante", status_code=303)
        try:
            inst_id = int(instrument_id)
        except ValueError:
            return RedirectResponse("/investments/transactions/add?err=Strumento+non+valido", status_code=303)
        if session.get(Instrument, inst_id) is None:
            return RedirectResponse("/investments/transactions/add?err=Strumento+non+trovato", status_code=303)

    tx = InvestmentTransaction(
        instrument_id=inst_id,
        transaction_type=transaction_type,
        broker_name=broker_name.strip(),
        trade_date=tx_date,
        quantity=quantity,
        unit_price=unit_price,
        fees=fees,
        currency=currency.strip().upper(),
        pac_id=int(pac_id) if pac_id else None,
        notes=notes.strip() or None,
    )
    session.add(tx)
    session.commit()
    return RedirectResponse("/investments?msg=Operazione+registrata", status_code=303)


@router.get("/transactions/{tx_id}/edit", response_class=HTMLResponse)
def inv_transaction_edit_form(tx_id: int, request: Request, msg: str = "", err: str = "",
                              session: Session = Depends(get_session)):
    tx = session.get(InvestmentTransaction, tx_id)
    if not tx:
        return RedirectResponse("/investments/transactions?err=Operazione+non+trovata", status_code=303)
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    return templates.TemplateResponse("investments/transaction_form.html", {
        "request": request,
        "transaction": tx,
        "instruments": instruments,
        "pacs": pacs,
        "title": "Modifica acquisto",
        "today": date.today().isoformat(),
        "flash": _flash(msg, err),
    })


@router.post("/transactions/{tx_id}/edit")
def inv_transaction_edit(
    tx_id: int,
    transaction_type: str = Form("BUY"),
    broker_name: str = Form("Fineco"),
    trade_date: str = Form(...),
    quantity: float = Form(...),
    unit_price: float = Form(...),
    fees: float = Form(0.0),
    currency: str = Form("EUR"),
    pac_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    back = f"/investments/transactions/{tx_id}/edit"
    if quantity <= 0:
        return RedirectResponse(f"{back}?err=Quantità+non+valida", status_code=303)
    if unit_price <= 0:
        return RedirectResponse(f"{back}?err=Prezzo+non+valido", status_code=303)
    if fees < 0:
        return RedirectResponse(f"{back}?err=Commissioni+non+valide", status_code=303)
    if transaction_type not in TRANSACTION_TYPES:
        return RedirectResponse(f"{back}?err=Tipo+non+valido", status_code=303)
    try:
        tx_date = date.fromisoformat(trade_date)
    except ValueError:
        return RedirectResponse(f"{back}?err=Data+non+valida", status_code=303)

    tx = session.get(InvestmentTransaction, tx_id)
    if tx:
        tx.transaction_type = transaction_type
        tx.broker_name = broker_name.strip()
        tx.trade_date = tx_date
        tx.quantity = quantity
        tx.unit_price = unit_price
        tx.fees = fees
        tx.currency = currency.strip().upper()
        tx.pac_id = int(pac_id) if pac_id else None
        tx.notes = notes.strip() or None
        tx.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse("/investments/transactions?msg=Operazione+aggiornata", status_code=303)


@router.post("/transactions/{tx_id}/delete")
def inv_transaction_delete(tx_id: int, session: Session = Depends(get_session)):
    tx = session.get(InvestmentTransaction, tx_id)
    if tx:
        session.delete(tx)
        session.commit()
    return RedirectResponse("/investments/transactions?msg=Operazione+eliminata", status_code=303)


# ── PAC ──────────────────────────────────────────────────────────────────────

def _parse_instrument_ids(raw: list[str]) -> list[Optional[int]]:
    """Form select values → ints, keeping list alignment (None for empty rows)."""
    out: list[Optional[int]] = []
    for v in raw:
        v = (v or "").strip()
        try:
            out.append(int(v) if v else None)
        except ValueError:
            out.append(None)
    return out


@router.get("/pac", response_class=HTMLResponse)
def pac_list(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    inst_by_id = {inst.id: inst for inst in instruments}
    last_prices: dict[int, float] = {}
    for inst in instruments:
        price = _price_in_inst_currency(latest_quote(inst.id, session), inst, session)
        if price is not None:
            last_prices[inst.id] = price

    pac_data = []
    for pac in pacs:
        pac_txs = session.exec(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.pac_id == pac.id)
            .order_by(InvestmentTransaction.trade_date, InvestmentTransaction.id)
        ).all()
        pp = _compute_pac(pac, pac_txs, inst_by_id, last_prices, session)
        components = session.exec(
            select(PACComponent).where(PACComponent.pac_id == pac.id)
        ).all()
        insts = {c.instrument_id: session.get(Instrument, c.instrument_id) for c in components}
        pac_data.append({"pac": pac, "position": pp, "components": components, "instruments": insts})

    return templates.TemplateResponse("investments/pac_list.html", {
        "request": request,
        "pac_data": pac_data,
        "flash": _flash(msg, err),
    })


@router.get("/pac/add", response_class=HTMLResponse)
def pac_add_form(request: Request, msg: str = "", err: str = "", session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    return templates.TemplateResponse("investments/pac_form.html", {
        "request": request,
        "pac": None,
        "instruments": instruments,
        "title": "Nuovo PAC",
        "form_action": "/investments/pac/add",
        "submit_label": "Crea PAC",
        "initial_components": [{"id": "", "weight": ""}],
        "flash": _flash(msg, err),
    })


@router.post("/pac/add")
def pac_add(
    name: str = Form(...),
    description: str = Form(""),
    instrument_ids: list[str] = Form(default=[]),
    target_weights: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    ids = _parse_instrument_ids(instrument_ids)
    if not any(i is not None for i in ids):
        return RedirectResponse("/investments/pac/add?err=Seleziona+almeno+uno+strumento", status_code=303)
    pac = PAC(name=name.strip(), description=description.strip() or None)
    session.add(pac)
    session.flush()
    for i, iid in enumerate(ids):
        if iid is None:
            continue
        w_str = target_weights[i] if i < len(target_weights) else ""
        weight = float(w_str) if w_str else None
        session.add(PACComponent(pac_id=pac.id, instrument_id=iid, target_weight=weight))
    session.commit()
    return RedirectResponse("/investments/pac?msg=PAC+creato", status_code=303)


@router.get("/pac/{pac_id}/edit", response_class=HTMLResponse)
def pac_edit_form(pac_id: int, request: Request, msg: str = "", err: str = "",
                  session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac?err=PAC+non+trovato", status_code=303)
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    components = session.exec(select(PACComponent).where(PACComponent.pac_id == pac_id)).all()
    initial = [
        {"id": str(c.instrument_id), "weight": str(int(c.target_weight)) if c.target_weight else ""}
        for c in components
    ] or [{"id": "", "weight": ""}]
    return templates.TemplateResponse("investments/pac_form.html", {
        "request": request,
        "pac": pac,
        "instruments": instruments,
        "title": f"Modifica {pac.name}",
        "form_action": f"/investments/pac/{pac_id}/edit",
        "submit_label": "Salva modifiche",
        "initial_components": initial,
        "flash": _flash(msg, err),
    })


@router.post("/pac/{pac_id}/edit")
def pac_edit(
    pac_id: int,
    name: str = Form(...),
    description: str = Form(""),
    instrument_ids: list[str] = Form(default=[]),
    target_weights: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac?err=PAC+non+trovato", status_code=303)
    ids = _parse_instrument_ids(instrument_ids)
    if not any(i is not None for i in ids):
        return RedirectResponse(f"/investments/pac/{pac_id}/edit?err=Seleziona+almeno+uno+strumento", status_code=303)
    pac.name = name.strip()
    pac.description = description.strip() or None
    # Replace components
    old = session.exec(select(PACComponent).where(PACComponent.pac_id == pac_id)).all()
    for c in old:
        session.delete(c)
    session.flush()
    for i, iid in enumerate(ids):
        if iid is None:
            continue
        w_str = target_weights[i] if i < len(target_weights) else ""
        weight = float(w_str) if w_str else None
        session.add(PACComponent(pac_id=pac_id, instrument_id=iid, target_weight=weight))
    session.commit()
    return RedirectResponse("/investments/pac?msg=PAC+aggiornato", status_code=303)


@router.get("/pac/{pac_id}/execute", response_class=HTMLResponse)
def pac_execute_form(pac_id: int, request: Request, msg: str = "", err: str = "",
                     session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac?err=PAC+non+trovato", status_code=303)
    components = session.exec(select(PACComponent).where(PACComponent.pac_id == pac_id)).all()
    instruments = [session.get(Instrument, c.instrument_id) for c in components]
    return templates.TemplateResponse("investments/pac_execute.html", {
        "request": request,
        "pac": pac,
        "components": list(zip(components, instruments)),
        "today": date.today().isoformat(),
        "broker": "Fineco",
        "flash": _flash(msg, err),
    })


@router.post("/pac/{pac_id}/execute")
def pac_execute(
    pac_id: int,
    trade_date: str = Form(...),
    broker_name: str = Form("Fineco"),
    instrument_ids: list[str] = Form(default=[]),
    quantities: list[str] = Form(default=[]),
    unit_prices: list[str] = Form(default=[]),
    fees_list: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac?err=PAC+non+trovato", status_code=303)

    try:
        tx_date = date.fromisoformat(trade_date)
    except ValueError:
        return RedirectResponse(f"/investments/pac/{pac_id}/execute?err=Data+non+valida", status_code=303)

    added = 0
    for i, iid in enumerate(_parse_instrument_ids(instrument_ids)):
        if iid is None:
            continue
        qty_str = quantities[i] if i < len(quantities) else ""
        price_str = unit_prices[i] if i < len(unit_prices) else ""
        if not qty_str or not price_str:
            continue
        try:
            qty = float(qty_str)
            price = float(price_str)
            fee = float(fees_list[i]) if i < len(fees_list) and fees_list[i] else 0.0
        except ValueError:
            continue
        if qty <= 0 or price <= 0:
            continue
        inst = session.get(Instrument, iid)
        session.add(InvestmentTransaction(
            instrument_id=iid,
            transaction_type="BUY",
            broker_name=broker_name.strip(),
            trade_date=tx_date,
            quantity=qty,
            unit_price=price,
            fees=fee,
            currency=inst.currency if inst else "EUR",
            pac_id=pac_id,
        ))
        added += 1

    session.commit()
    if added == 0:
        return RedirectResponse(f"/investments/pac/{pac_id}/execute?err=Nessun+acquisto+registrato", status_code=303)
    return RedirectResponse(f"/investments?msg={added}+acquisti+registrati", status_code=303)


@router.post("/pac/{pac_id}/delete")
def pac_delete(pac_id: int, session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if pac:
        pac.active = False
        session.commit()
    return RedirectResponse("/investments/pac?msg=PAC+archiviato", status_code=303)
