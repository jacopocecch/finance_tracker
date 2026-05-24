from datetime import date, datetime, timezone
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

router = APIRouter(prefix="/investments")
templates = Jinja2Templates(directory="templates")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_portfolio_data(session: Session):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()

    positions = []
    last_prices: dict[int, float] = {}

    for inst in instruments:
        txs = session.exec(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.instrument_id == inst.id)
            .order_by(InvestmentTransaction.trade_date)
        ).all()
        if not txs:
            continue

        quote = latest_quote(inst.id, session)

        # Convert quote price to instrument's reporting currency if they differ
        raw_price = quote.price if quote else None
        if raw_price is not None and quote and quote.currency and quote.currency != inst.currency:
            raw_price = _fx.convert(raw_price, quote.currency, inst.currency, session)

        if quote:
            last_prices[inst.id] = raw_price

        pos = compute_position(
            instrument_id=inst.id,
            name=inst.name,
            isin=inst.isin,
            ticker=inst.ticker,
            currency=inst.currency,
            transactions=txs,
            last_price=raw_price,
            is_stale=quote.is_stale if quote else True,
            quote_timestamp=quote.quote_timestamp if quote else None,
        )
        positions.append(pos)

    positions.sort(key=lambda p: p.market_value or p.total_invested, reverse=True)

    pac_positions = []
    for pac in pacs:
        pac_txs = session.exec(
            select(InvestmentTransaction).where(InvestmentTransaction.pac_id == pac.id)
        ).all()
        if not pac_txs:
            continue
        pp = compute_pac_position(pac.id, pac.name, pac_txs, last_prices)
        pac_positions.append(pp)

    return compute_portfolio(positions, pac_positions)


def _get_instrument_or_404(instrument_id: int, session: Session):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        raise ValueError("Strumento non trovato")
    return inst


# ── Overview ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_session)):
    summary = _build_portfolio_data(session)
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()

    liquidity_ids = {
        inst.id for inst in session.exec(
            select(Instrument).where(Instrument.is_liquidity == True, Instrument.active == True)
        ).all()
    }
    inv_positions = [p for p in summary.positions if p.instrument_id not in liquidity_ids]
    liq_positions = [p for p in summary.positions if p.instrument_id in liquidity_ids]
    inv_summary = compute_portfolio(inv_positions, summary.pac_positions)

    chart_labels = [p.name for p in inv_positions]
    chart_invested = [p.total_invested for p in inv_positions]
    chart_market = [p.market_value or p.total_invested for p in inv_positions]

    return templates.TemplateResponse("investments/overview.html", {
        "request": request,
        "summary": inv_summary,
        "liq_positions": liq_positions,
        "pacs": pacs,
        "chart_labels": chart_labels,
        "chart_invested": chart_invested,
        "chart_market": chart_market,
    })


# ── Quotes ───────────────────────────────────────────────────────────────────

@router.post("/quotes/refresh")
def refresh_quotes_all(session: Session = Depends(get_session)):
    refresh_all_quotes(session)
    return RedirectResponse("/investments", status_code=303)


@router.post("/quotes/{instrument_id}/refresh")
def refresh_quote_one(instrument_id: int, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if inst:
        refresh_quote(inst, session)
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
def instruments_list(request: Request, session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    data = []
    for inst in instruments:
        quote = latest_quote(inst.id, session)
        txs = session.exec(
            select(InvestmentTransaction).where(InvestmentTransaction.instrument_id == inst.id)
        ).all()
        total_qty = sum(tx.quantity * (1 if tx.transaction_type == "BUY" else -1) for tx in txs)
        total_invested = sum(tx.quantity * tx.unit_price + tx.fees for tx in txs if tx.transaction_type == "BUY")
        data.append({
            "inst": inst,
            "quote": quote,
            "total_qty": round(total_qty, 8),
            "total_invested": round(total_invested, 2),
        })
    return templates.TemplateResponse("investments/instruments.html", {
        "request": request,
        "instruments": data,
    })


@router.get("/instruments/add", response_class=HTMLResponse)
def instrument_add_form(request: Request):
    return templates.TemplateResponse("investments/instrument_form.html", {
        "request": request,
        "instrument": None,
        "title": "Nuovo strumento",
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
    session: Session = Depends(get_session),
):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        return RedirectResponse("/investments/instruments", status_code=303)

    txs = session.exec(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.instrument_id == instrument_id)
        .order_by(InvestmentTransaction.trade_date.desc())
    ).all()
    quote = latest_quote(instrument_id, session)
    pacs_map = {p.id: p for p in session.exec(select(PAC)).all()}

    pos = compute_position(
        instrument_id=inst.id,
        name=inst.name,
        isin=inst.isin,
        ticker=inst.ticker,
        currency=inst.currency,
        transactions=txs,
        last_price=quote.price if quote else None,
        is_stale=quote.is_stale if quote else True,
        quote_timestamp=quote.quote_timestamp if quote else None,
    ) if txs else None

    return templates.TemplateResponse("investments/instrument_detail.html", {
        "request": request,
        "inst": inst,
        "pos": pos,
        "quote": quote,
        "transactions": txs,
        "pacs_map": pacs_map,
        "flash": {"message": msg, "type": "info"} if msg else None,
    })


@router.get("/instruments/{instrument_id}/edit", response_class=HTMLResponse)
def instrument_edit_form(instrument_id: int, request: Request, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if not inst:
        return RedirectResponse("/investments/instruments", status_code=303)
    return templates.TemplateResponse("investments/instrument_form.html", {
        "request": request,
        "instrument": inst,
        "title": "Modifica strumento",
    })


@router.post("/instruments/{instrument_id}/edit")
def instrument_edit(
    instrument_id: int,
    name: str = Form(...),
    ticker: str = Form(...),
    exchange: str = Form(""),
    currency: str = Form("EUR"),
    is_liquidity: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    inst = session.get(Instrument, instrument_id)
    if inst:
        inst.name = name.strip()
        inst.ticker = ticker.strip()
        inst.exchange = exchange.strip()
        inst.currency = currency.strip().upper()
        inst.is_liquidity = is_liquidity == "1"
        inst.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse(f"/investments/instruments/{instrument_id}", status_code=303)


@router.post("/instruments/{instrument_id}/delete")
def instrument_delete(instrument_id: int, session: Session = Depends(get_session)):
    inst = session.get(Instrument, instrument_id)
    if inst:
        inst.active = False
        inst.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse("/investments/instruments", status_code=303)


# ── Investment Transactions ──────────────────────────────────────────────────

@router.get("/transactions", response_class=HTMLResponse)
def inv_transactions_list(request: Request, session: Session = Depends(get_session)):
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
    })


@router.get("/transactions/add", response_class=HTMLResponse)
def inv_transaction_add_form(request: Request, session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    return templates.TemplateResponse("investments/transaction_form.html", {
        "request": request,
        "transaction": None,
        "instruments": instruments,
        "pacs": pacs,
        "title": "Nuovo acquisto",
        "today": date.today().isoformat(),
    })


@router.post("/transactions/add")
def inv_transaction_add(
    instrument_id: str = Form(...),
    # New instrument fields (used when instrument_id == "new")
    new_name: str = Form(""),
    new_isin: str = Form(""),
    new_ticker: str = Form(""),
    new_exchange: str = Form(""),
    new_currency: str = Form("EUR"),
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

    # Resolve instrument
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
            )
            session.add(inst)
            session.flush()
            refresh_quote(inst, session)
        inst_id = inst.id
    else:
        inst_id = int(instrument_id)

    tx = InvestmentTransaction(
        instrument_id=inst_id,
        transaction_type=transaction_type,
        broker_name=broker_name.strip(),
        trade_date=date.fromisoformat(trade_date),
        quantity=quantity,
        unit_price=unit_price,
        fees=fees,
        currency=currency.strip().upper(),
        pac_id=int(pac_id) if pac_id else None,
        notes=notes.strip() or None,
    )
    session.add(tx)
    session.commit()
    return RedirectResponse("/investments", status_code=303)


@router.get("/transactions/{tx_id}/edit", response_class=HTMLResponse)
def inv_transaction_edit_form(tx_id: int, request: Request, session: Session = Depends(get_session)):
    tx = session.get(InvestmentTransaction, tx_id)
    if not tx:
        return RedirectResponse("/investments/transactions", status_code=303)
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    return templates.TemplateResponse("investments/transaction_form.html", {
        "request": request,
        "transaction": tx,
        "instruments": instruments,
        "pacs": pacs,
        "title": "Modifica acquisto",
        "today": date.today().isoformat(),
    })


@router.post("/transactions/{tx_id}/edit")
def inv_transaction_edit(
    tx_id: int,
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
    tx = session.get(InvestmentTransaction, tx_id)
    if tx:
        tx.broker_name = broker_name.strip()
        tx.trade_date = date.fromisoformat(trade_date)
        tx.quantity = quantity
        tx.unit_price = unit_price
        tx.fees = fees
        tx.currency = currency.strip().upper()
        tx.pac_id = int(pac_id) if pac_id else None
        tx.notes = notes.strip() or None
        tx.updated_at = datetime.now(timezone.utc)
        session.commit()
    return RedirectResponse("/investments/transactions", status_code=303)


@router.post("/transactions/{tx_id}/delete")
def inv_transaction_delete(tx_id: int, session: Session = Depends(get_session)):
    tx = session.get(InvestmentTransaction, tx_id)
    if tx:
        session.delete(tx)
        session.commit()
    return RedirectResponse("/investments/transactions", status_code=303)


# ── PAC ──────────────────────────────────────────────────────────────────────

@router.get("/pac", response_class=HTMLResponse)
def pac_list(request: Request, session: Session = Depends(get_session)):
    pacs = session.exec(select(PAC).where(PAC.active == True)).all()
    last_prices: dict[int, float] = {}
    for inst in session.exec(select(Instrument).where(Instrument.active == True)).all():
        q = latest_quote(inst.id, session)
        if q:
            last_prices[inst.id] = q.price

    pac_data = []
    for pac in pacs:
        pac_txs = session.exec(
            select(InvestmentTransaction).where(InvestmentTransaction.pac_id == pac.id)
        ).all()
        pp = compute_pac_position(pac.id, pac.name, pac_txs, last_prices)
        components = session.exec(
            select(PACComponent).where(PACComponent.pac_id == pac.id)
        ).all()
        insts = {c.instrument_id: session.get(Instrument, c.instrument_id) for c in components}
        pac_data.append({"pac": pac, "position": pp, "components": components, "instruments": insts})

    return templates.TemplateResponse("investments/pac_list.html", {
        "request": request,
        "pac_data": pac_data,
    })


@router.get("/pac/add", response_class=HTMLResponse)
def pac_add_form(request: Request, session: Session = Depends(get_session)):
    instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()
    return templates.TemplateResponse("investments/pac_form.html", {
        "request": request,
        "pac": None,
        "instruments": instruments,
        "title": "Nuovo PAC",
    })


@router.post("/pac/add")
def pac_add(
    name: str = Form(...),
    description: str = Form(""),
    instrument_ids: list[int] = Form(default=[]),
    target_weights: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    pac = PAC(name=name.strip(), description=description.strip() or None)
    session.add(pac)
    session.flush()
    for i, iid in enumerate(instrument_ids):
        w_str = target_weights[i] if i < len(target_weights) else ""
        weight = float(w_str) if w_str else None
        session.add(PACComponent(pac_id=pac.id, instrument_id=iid, target_weight=weight))
    session.commit()
    return RedirectResponse(f"/investments/pac/{pac.id}", status_code=303)


@router.get("/pac/{pac_id}", response_class=HTMLResponse)
def pac_detail(pac_id: int, request: Request, session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac", status_code=303)

    components = session.exec(select(PACComponent).where(PACComponent.pac_id == pac_id)).all()
    insts = {c.instrument_id: session.get(Instrument, c.instrument_id) for c in components}

    last_prices: dict[int, float] = {}
    for iid in insts:
        q = latest_quote(iid, session)
        if q:
            last_prices[iid] = q.price

    pac_txs = session.exec(
        select(InvestmentTransaction).where(InvestmentTransaction.pac_id == pac_id)
        .order_by(InvestmentTransaction.trade_date.desc())
    ).all()
    pp = compute_pac_position(pac_id, pac.name, pac_txs, last_prices)
    all_instruments = session.exec(select(Instrument).where(Instrument.active == True)).all()

    return templates.TemplateResponse("investments/pac_detail.html", {
        "request": request,
        "pac": pac,
        "position": pp,
        "components": components,
        "instruments_map": insts,
        "transactions": pac_txs,
        "all_instruments": all_instruments,
    })


@router.get("/pac/{pac_id}/execute", response_class=HTMLResponse)
def pac_execute_form(pac_id: int, request: Request, session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac", status_code=303)
    components = session.exec(select(PACComponent).where(PACComponent.pac_id == pac_id)).all()
    instruments = [session.get(Instrument, c.instrument_id) for c in components]
    return templates.TemplateResponse("investments/pac_execute.html", {
        "request": request,
        "pac": pac,
        "components": list(zip(components, instruments)),
        "today": date.today().isoformat(),
        "broker": "Fineco",
    })


@router.post("/pac/{pac_id}/execute")
def pac_execute(
    pac_id: int,
    trade_date: str = Form(...),
    broker_name: str = Form("Fineco"),
    instrument_ids: list[int] = Form(default=[]),
    quantities: list[str] = Form(default=[]),
    unit_prices: list[str] = Form(default=[]),
    fees_list: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    pac = session.get(PAC, pac_id)
    if not pac:
        return RedirectResponse("/investments/pac", status_code=303)

    tx_date = date.fromisoformat(trade_date)
    added = 0
    for i, iid in enumerate(instrument_ids):
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
    return RedirectResponse(f"/investments/pac/{pac_id}", status_code=303)


@router.post("/pac/{pac_id}/delete")
def pac_delete(pac_id: int, session: Session = Depends(get_session)):
    pac = session.get(PAC, pac_id)
    if pac:
        pac.active = False
        session.commit()
    return RedirectResponse("/investments/pac", status_code=303)
