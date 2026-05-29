from datetime import date, datetime
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine, Session, select
from config import DB_PATH

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)


class Account(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bank_name: str
    external_id: str = Field(unique=True)
    name: str
    display_name: Optional[str] = None
    iban: Optional[str] = None
    type: str = "checking"  # checking | savings | investment
    currency: str = "EUR"
    session_id: str = ""         # Enable Banking session ID (from /sessions)
    last_sync: Optional[datetime] = None
    connected: bool = False
    deleted: bool = False
    balance_threshold: Optional[float] = None


class Transaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    external_id: str = Field(unique=True)  # Enable Banking transaction ID
    date: date
    amount: float                # negative = expense
    currency: str = "EUR"
    description: str = ""
    merchant: Optional[str] = None
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    transfer_partner_id: Optional[int] = Field(default=None, foreign_key="transaction.id")
    personal_share: Optional[float] = None  # personally-owed portion (positive); None = full amount
    raw_data: str = ""           # JSON blob from Enable Banking
    status: str = "BOOK"         # BOOK | PDNG
    is_confirmed: bool = Field(default=False)
    created_at: Optional[datetime] = Field(default=None)


class BalanceSnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    date: date
    balance: float


class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    type: str = "expense"        # income | expense | transfer
    color: str = "#6B7280"       # tailwind gray-500 default
    icon: str = "💳"


class Instrument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = "ETF"
    name: str
    isin: str = Field(unique=True)
    ticker: str                   # yfinance symbol, e.g. VWCE.MI
    exchange: str = ""
    currency: str = "EUR"
    data_source: str = "yfinance"
    active: bool = True
    is_liquidity: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PAC(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PACComponent(SQLModel, table=True):
    __tablename__ = "pac_component"
    id: Optional[int] = Field(default=None, primary_key=True)
    pac_id: int = Field(foreign_key="pac.id")
    instrument_id: int = Field(foreign_key="instrument.id")
    target_weight: Optional[float] = None
    note: Optional[str] = None


class InvestmentTransaction(SQLModel, table=True):
    __tablename__ = "investment_transaction"
    id: Optional[int] = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instrument.id")
    transaction_type: str = "BUY"  # BUY | SELL
    broker_name: str = "Fineco"
    trade_date: date
    quantity: float
    unit_price: float
    fees: float = 0.0
    currency: str = "EUR"
    pac_id: Optional[int] = Field(default=None, foreign_key="pac.id")
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MarketQuote(SQLModel, table=True):
    __tablename__ = "market_quote"
    id: Optional[int] = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instrument.id")
    price: float
    currency: str
    quote_timestamp: datetime
    source: str = "yfinance"
    is_stale: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Budget(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("category_id", "period"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="category.id")
    amount: float
    period: str = "monthly"
    active: bool = True


class ExchangeRate(SQLModel, table=True):
    __tablename__ = "exchange_rate"
    id: Optional[int] = Field(default=None, primary_key=True)
    from_currency: str = Field(index=True)
    to_currency: str = "EUR"
    rate: float
    rate_date: Optional[date] = None   # None = current rate, date = historical
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class CategoryRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    pattern: str                 # regex applied to description (case-insensitive)
    category_id: int = Field(foreign_key="category.id")
    priority: int = 0            # higher = checked first


class MerchantCategory(SQLModel, table=True):
    __tablename__ = "merchant_category"
    __table_args__ = (UniqueConstraint("merchant"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    merchant: str = Field(index=True)  # lowercase, exact match
    category_id: int = Field(foreign_key="category.id")


DEFAULT_CATEGORIES = [
    ("Stipendio",     "income",   "#10B981", "💰"),
    ("Rimborsi",      "income",   "#34D399", "↩️"),
    ("Interessi",           "income",   "#10B981", "🏦"),
    ("Vendite",             "income",   "#6EE7B7", "🏷️"),
    ("Affitto/Mutuo", "expense",  "#EF4444", "🏠"),
    ("Casa",          "expense",  "#F87171", "🔌"),
    ("Spesa",         "expense",  "#F97316", "🛒"),
    ("Cucina",        "expense",  "#FB923C", "🍳"),
    ("Ristoranti",    "expense",  "#F59E0B", "🍽️"),
    ("Bar/Caffè",     "expense",  "#FBBF24", "☕"),
    ("Pasticceria/Gelateria", "expense",  "#FCD34D", "🍰"),
    ("Automobile",    "expense",  "#7C3AED", "🚗"),
    ("Carburante",    "expense",  "#8B5CF6", "⛽"),
    ("Trasporti",     "expense",  "#6366F1", "🚇"),
    ("Viaggi",        "expense",  "#818CF8", "✈️"),
    ("Shopping",      "expense",  "#EC4899", "🛍️"),
    ("Abbonamenti",   "expense",  "#14B8A6", "📱"),
    ("Salute",        "expense",  "#06B6D4", "🏥"),
    ("Sport",         "expense",  "#22D3EE", "🏋️"),
    ("Libri/Fumetti", "expense",  "#A78BFA", "📚"),
    ("Cinema",        "expense",  "#C084FC", "🎬"),
    ("Regali",        "both",     "#F472B6", "🎁"),
    ("Vestiti",       "expense",  "#BE185D", "👕"),
    ("Investimento",  "investment", "#3B82F6", "📈"),
    ("Trasferimento", "transfer", "#9CA3AF", "↔️"),
    ("Prelievo ATM",  "transfer", "#9CA3AF", "💵"),
    ("Altro",         "expense",  "#6B7280", "❓"),
]

NEW_CATEGORIES = [
    ("Automobile",          "expense",  "#7C3AED", "🚗"),
    ("Casa",                "expense",  "#F87171", "🔌"),
    ("Interessi",           "income",   "#10B981", "🏦"),
    ("Cucina",        "expense",  "#FB923C", "🍳"),
    ("Bar/Caffè",     "expense",  "#FBBF24", "☕"),
    ("Pasticceria/Gelateria", "expense",  "#FCD34D", "🍰"),
    ("Sport",         "expense",  "#22D3EE", "🏋️"),
    ("Viaggi",        "expense",  "#818CF8", "✈️"),
    ("Libri/Fumetti", "expense",  "#A78BFA", "📚"),
    ("Cinema",        "expense",  "#C084FC", "🎬"),
    ("Vestiti",       "expense",  "#BE185D", "👕"),
    ("Vendite",       "income",   "#6EE7B7", "🏷️"),
    ("Regali",        "both",     "#F472B6", "🎁"),
    ("Prelievo ATM",  "transfer", "#9CA3AF", "💵"),
]

DEFAULT_RULES = [
    (r"stipendio|salary|retribuzione",                          "Stipendio",     10),
    (r"rimborso|refund|cashback",                               "Rimborsi",      10),
    (r"vinted|subito\.it|wallapop",                             "Vendite",       10),
    (r"affitto|mutuo|condominio",                               "Affitto/Mutuo", 10),
    (r"luce|gas|enel|eni gas|acqua|tari|internet|tim |vodafone|wind|fastweb", "Casa", 8),
    (r"esselunga|coop|carrefour|lidl|aldi|eurospin|pam|conad", "Spesa",          8),
    (r"supermercato|ipermercato|alimentari",                    "Spesa",          7),
    (r"ristorante|pizzeria|trattoria|osteria|mcdonald|burger",  "Ristoranti",     8),
    (r"pasticceria|gelateria|bakery",                           "Pasticceria",    8),
    (r"bar |caffè|caffe |coffee",                               "Bar/Caffè",      7),
    (r"eni |q8|totalerg|shell|ip gas|agip|tamoil",             "Carburante",     9),
    (r"atm|ataf|trenitalia|italo|flixbus|taxi|uber|bolt",      "Trasporti",      8),
    (r"ryanair|easyjet|booking|airbnb|hotel|aereo|volo",       "Viaggi",         9),
    (r"amazon|zalando|shein|zara|h&m|ikea|mediaworld",         "Shopping",       8),
    (r"netflix|spotify|disney|prime video|youtube|dazn|apple", "Abbonamenti",    9),
    (r"farmacia|medico|dottore|ospedale|dentista|ottico",      "Salute",         9),
    (r"palestra|gym|fitness|piscina|sport",                    "Sport",          8),
    (r"libreria|feltrinelli|mondadori|fumett",                 "Libri/Fumetti",  8),
    (r"cinema|uci|the space|multisala",                        "Cinema",         8),
    (r"etf|titol|azion|fondo|fineco invest|directa",           "Investimento",   9),
    (r"revolut|n26|wise|paypal|satispay|bonifico",             "Trasferimento",  5),
    (r"prelievo|bancomat|sportello atm",                       "Prelievo ATM",  12),
]


def init_db():
    SQLModel.metadata.create_all(engine)  # creates MerchantCategory if missing
    from sqlalchemy import text
    with engine.connect() as conn:
        tx_cols = [r[1] for r in conn.execute(text("PRAGMA table_info('transaction')")).fetchall()]
        if "transfer_partner_id" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN transfer_partner_id INTEGER REFERENCES 'transaction'(id)"))
            conn.commit()
        if "personal_share" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN personal_share REAL"))
            conn.commit()
        if "currency" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'"))
            conn.commit()
        if "is_confirmed" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN is_confirmed INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("UPDATE 'transaction' SET is_confirmed = 1 WHERE category_id IS NOT NULL"))
            conn.commit()
        if "created_at" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN created_at DATETIME"))
            conn.commit()
        if "status" not in tx_cols:
            conn.execute(text("ALTER TABLE 'transaction' ADD COLUMN status TEXT NOT NULL DEFAULT 'BOOK'"))
            conn.commit()
    with engine.connect() as conn:
        acols = [r[1] for r in conn.execute(text("PRAGMA table_info('account')")).fetchall()]
        if "display_name" not in acols:
            conn.execute(text("ALTER TABLE 'account' ADD COLUMN display_name TEXT"))
            conn.commit()
    with engine.connect() as conn:
        fxcols = [r[1] for r in conn.execute(text("PRAGMA table_info('exchange_rate')")).fetchall()]
        if "rate_date" not in fxcols:
            conn.execute(text("ALTER TABLE 'exchange_rate' ADD COLUMN rate_date DATE"))
            conn.commit()
    with engine.connect() as conn:
        instcols = [r[1] for r in conn.execute(text("PRAGMA table_info('instrument')")).fetchall()]
        if "is_liquidity" not in instcols:
            conn.execute(text("ALTER TABLE 'instrument' ADD COLUMN is_liquidity INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
    with engine.connect() as conn:
        acccols = [r[1] for r in conn.execute(text("PRAGMA table_info('account')")).fetchall()]
        if "deleted" not in acccols:
            conn.execute(text("ALTER TABLE 'account' ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
        if "balance_threshold" not in acccols:
            conn.execute(text("ALTER TABLE 'account' ADD COLUMN balance_threshold REAL"))
            conn.commit()
    with Session(engine) as session:
        existing_cats = {c.name for c in session.exec(select(Category)).all()}
        if not existing_cats:
            # Fresh install
            cat_map = {}
            for name, type_, color, icon in DEFAULT_CATEGORIES:
                cat = Category(name=name, type=type_, color=color, icon=icon)
                session.add(cat)
                session.flush()
                cat_map[name] = cat.id
            for pattern, cat_name, priority in DEFAULT_RULES:
                session.add(CategoryRule(pattern=pattern, category_id=cat_map[cat_name], priority=priority))
            session.commit()
        else:
            # Existing install — add only missing categories
            for name, type_, color, icon in NEW_CATEGORIES:
                if name not in existing_cats:
                    session.add(Category(name=name, type=type_, color=color, icon=icon))
            # Rename migrations
            old = session.exec(select(Category).where(Category.name == "Pasticceria")).first()
            target_exists = session.exec(select(Category).where(Category.name == "Pasticceria/Gelateria")).first()
            if old and not target_exists:
                old.name = "Pasticceria/Gelateria"
            elif old and target_exists:
                session.delete(old)
            # Migrate "Investimento" from transfer → investment type
            inv = session.exec(select(Category).where(Category.name == "Investimento", Category.type == "transfer")).first()
            if inv:
                inv.type = "investment"
            # Migrate "Regali" from expense → both type
            reg = session.exec(select(Category).where(Category.name == "Regali", Category.type.in_(["expense", "income"]))).first()
            if reg:
                reg.type = "both"
            # Add Prelievo ATM rule if missing
            prelievo_cat = session.exec(select(Category).where(Category.name == "Prelievo ATM")).first()
            if prelievo_cat:
                existing_rule = session.exec(select(CategoryRule).where(CategoryRule.category_id == prelievo_cat.id)).first()
                if not existing_rule:
                    session.add(CategoryRule(pattern=r"prelievo|bancomat|sportello atm", category_id=prelievo_cat.id, priority=12))
            session.commit()


def get_session():
    with Session(engine) as session:
        yield session
