import re
from sqlmodel import Session, select
from database import Category, CategoryRule, MerchantCategory, Transaction


def categorize(description: str, merchant: str | None, session: Session) -> int | None:
    # 1. Exact merchant mapping wins
    if merchant:
        mc = session.exec(
            select(MerchantCategory).where(MerchantCategory.merchant == merchant.lower())
        ).first()
        if mc:
            return mc.category_id

    # 2. Regex rules
    rules = session.exec(
        select(CategoryRule).order_by(CategoryRule.priority.desc())
    ).all()
    text = f"{description} {merchant or ''}".lower()
    for rule in rules:
        try:
            if re.search(rule.pattern, text, re.IGNORECASE):
                return rule.category_id
        except re.error:
            continue
    altro = session.exec(select(Category).where(Category.name == "Altro")).first()
    return altro.id if altro else None


def recategorize_all(session: Session):
    transactions = session.exec(select(Transaction)).all()
    for tx in transactions:
        tx.category_id = categorize(tx.description, tx.merchant, session)
    session.commit()
