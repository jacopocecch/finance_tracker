import re
from datetime import date


def parse_transaction(raw: dict, bank_name: str) -> dict:
    """Returns {'merchant': str|None, 'description': str, 'date': date|None} from raw Enable Banking transaction."""
    bank = bank_name.lower()
    if "fineco" in bank:
        return _parse_fineco(raw)
    if "ing" in bank:
        return _parse_ing(raw)
    if "revolut" in bank:
        return _parse_revolut(raw)
    if "paypal" in bank:
        return _parse_paypal(raw)
    return _parse_generic(raw)


def _parse_date_dmy(s: str) -> date | None:
    """Parse DD/MM/YYYY or DD/MM/YY from string, return None if not found."""
    m = re.search(r'(\d{2})/(\d{2})/(\d{2,4})', s)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _remittance(raw: dict) -> str:
    ri = raw.get("remittance_information")
    if isinstance(ri, list) and ri:
        return ri[0]
    return ri or ""


def _creditor_debtor_name(raw: dict) -> str | None:
    return (raw.get("creditor") or {}).get("name") or (raw.get("debtor") or {}).get("name")


def _parse_fineco(raw: dict) -> dict:
    rem = _remittance(raw)

    # Card payment: "MERCHANT CITY CC Carta N. ***** XXX Data operazione DD/MM/YY"
    if "Carta N." in rem:
        before = rem.split(" Carta N.")[0]
        merchant = re.sub(r'\s+[A-Z][A-Z0-9]*\s+[A-Z]{2}\s*$', '', before).strip()
        tx_date = _parse_date_dmy(rem.split("Data operazione")[-1]) if "Data operazione" in rem else None
        return {"merchant": merchant or before.strip(), "description": rem, "date": tx_date}

    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": rem or name or "", "date": None}


def _parse_ing(raw: dict) -> dict:
    rem = _remittance(raw)

    # Card payment: "Operazione Mastercard del DD/MM/YYYY alle ore ... presso MERCHANT - Transazione"
    if "Operazione Mastercard" in rem or "Operazione Visa" in rem:
        m = re.search(r'presso (.+?)(?:\s*-\s*Transazione|\s*$)', rem)
        merchant = m.group(1).strip() if m else None
        tx_date = _parse_date_dmy(rem)
        return {"merchant": merchant, "description": merchant or rem, "date": tx_date}

    # Bonifico with Note: and Anagrafica Ordinante
    if "Note:" in rem:
        note_m = re.search(r'Note:\s*(.+)$', rem)
        desc = note_m.group(1).strip() if note_m else rem
        ord_m = re.search(r'Anagrafica Ordinante\s+(.+?)\s+Note:', rem)
        sender = ord_m.group(1).strip() if ord_m else None
        return {"merchant": sender, "description": desc, "date": None}

    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": rem or name or "", "date": None}


def _parse_revolut(raw: dict) -> dict:
    code = (raw.get("bank_transaction_code") or {}).get("code") or ""
    rem = _remittance(raw)

    if code in ("CARD_PAYMENT", "CARD_REFUND"):
        merchant = rem.strip() or None
        return {"merchant": merchant, "description": merchant or rem, "date": None}

    return {"merchant": None, "description": rem, "date": None}


def _parse_paypal(raw: dict) -> dict:
    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": name or "PayPal", "date": None}


def _parse_generic(raw: dict) -> dict:
    name = _creditor_debtor_name(raw)
    rem = _remittance(raw)
    return {"merchant": name, "description": rem or name or "", "date": None}
