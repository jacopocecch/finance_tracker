import re


def parse_transaction(raw: dict, bank_name: str) -> dict:
    """Returns {'merchant': str|None, 'description': str} from raw Enable Banking transaction."""
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


def _remittance(raw: dict) -> str:
    ri = raw.get("remittance_information")
    if isinstance(ri, list) and ri:
        return ri[0]
    return ri or ""


def _creditor_debtor_name(raw: dict) -> str | None:
    return (raw.get("creditor") or {}).get("name") or (raw.get("debtor") or {}).get("name")


def _parse_fineco(raw: dict) -> dict:
    code = (raw.get("bank_transaction_code") or {}).get("code") or ""
    rem = _remittance(raw)

    # Card payment: "MERCHANT CITY CC Carta N. ***** XXX Data operazione DD/MM/YY"
    if "Carta N." in rem:
        before = rem.split(" Carta N.")[0]
        # Strip trailing CITY CC (one or more uppercase words + 2-letter country code)
        merchant = re.sub(r'(\s+[A-Z][A-Z0-9\s]*){0,2}\s+[A-Z]{2}\s*$', '', before).strip()
        return {"merchant": merchant or before.strip(), "description": rem}

    # Bonifico / transfer: creditor.name + remittance as description
    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": rem or name or ""}


def _parse_ing(raw: dict) -> dict:
    rem = _remittance(raw)

    # Card payment: contains "Operazione Mastercard" and "presso"
    if "Operazione Mastercard" in rem or "Operazione Visa" in rem:
        m = re.search(r'presso (.+?)(?:\s*-\s*Transazione|\s*$)', rem)
        merchant = m.group(1).strip() if m else None
        return {"merchant": merchant, "description": merchant or rem}

    # Bonifico with Note: and Anagrafica Ordinante
    if "Note:" in rem:
        note_m = re.search(r'Note:\s*(.+)$', rem)
        desc = note_m.group(1).strip() if note_m else rem

        ord_m = re.search(r'Anagrafica Ordinante\s+(.+?)\s+Note:', rem)
        sender = ord_m.group(1).strip() if ord_m else None

        return {"merchant": sender, "description": desc}

    # Generic: use remittance as-is
    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": rem or name or ""}


def _parse_revolut(raw: dict) -> dict:
    code = (raw.get("bank_transaction_code") or {}).get("code") or ""
    rem = _remittance(raw)

    # Card payment/refund: remittance[0] IS the merchant name
    if code in ("CARD_PAYMENT", "CARD_REFUND"):
        merchant = rem.strip() or None
        return {"merchant": merchant, "description": merchant or rem}

    # Exchange, topup, etc.
    return {"merchant": None, "description": rem}


def _parse_paypal(raw: dict) -> dict:
    # PayPal always has empty remittance_information
    name = _creditor_debtor_name(raw)
    return {"merchant": name, "description": name or "PayPal"}


def _parse_generic(raw: dict) -> dict:
    name = _creditor_debtor_name(raw)
    rem = _remittance(raw)
    return {"merchant": name, "description": rem or name or ""}
