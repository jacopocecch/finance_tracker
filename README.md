# Ledger — Personal Finance Tracker

A self-hosted personal finance dashboard that connects to Italian banks via [Enable Banking](https://enablebanking.com/) (PSD2/Open Banking). Tracks transactions, expenses by category, investments, and net worth.

**Stack:** FastAPI · SQLModel · SQLite · Jinja2 · HTMX · Alpine.js · Chart.js · Tailwind CSS

---

## Features

- Automatic transaction sync from FinecoBank, ING, Revolut, PayPal
- Per-bank transaction parsing (clean merchant names)
- Expense categorization via regex rules and merchant mappings
- Monthly budget tracking
- Investment portfolio P&L (via yfinance)
- PAC (recurring investment plan) execution log
- Multi-currency support with historical FX rates
- HTTP Basic Auth for remote access
- Nightly automatic DB backup

---

## Prerequisites

### 1. Enable Banking account

This app uses [Enable Banking](https://enablebanking.com/) as the PSD2 aggregator.

1. Sign up at [enablebanking.com](https://enablebanking.com/) and create an **application**
2. In the dashboard, generate an **RSA private key** and download it (`.pem` file)
3. Note your **Application ID** (UUID shown in the dashboard)
4. Set the **redirect URI** to `http://127.0.0.1:8000/setup/callback` (or your domain)

> **Important:** Enable Banking requires you to register accounts through their portal before connecting them here. The first connection flow (Setup → Connect) redirects you to your bank's PSD2 authorization page.

### 2. Python 3.11+

```bash
python --version  # must be 3.11 or higher
```

---

## Installation

```bash
git clone https://github.com/youruser/finance-tracker.git
cd finance-tracker

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Configuration

Copy the example config and fill in your values:

```bash
cp config.example.py config.py
```

Edit `config.py`:

```python
APPLICATION_ID = "your-enable-banking-application-id"
PRIVATE_KEY_PATH = Path("/path/to/your/private.pem")

ENABLE_BANKING_SANDBOX = False   # True = synthetic test data

DB_PATH = Path(__file__).parent / "finance.db"
HOST = "127.0.0.1"
PORT = 8000
REDIRECT_URI = f"http://{HOST}:{PORT}/setup/callback"

SYNC_HOUR = 7       # daily auto-sync time
SYNC_MINUTE = 0
```

> `config.py` and `*.pem` files are in `.gitignore` — never commit them.

---

## Running

```bash
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## Connecting your bank accounts

1. Go to **Setup** in the nav
2. Select your bank from the dropdown and click **Connetti →**
3. You will be redirected to your bank's PSD2 authorization page — log in and authorize access
4. After authorization, you are redirected back and the account appears in Setup
5. Click **Sync** to fetch transactions

Repeat for each bank account (FinecoBank, ING, Revolut, PayPal, etc.).

---

## Remote access (optional)

To expose the app over the internet securely:

1. Use [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — no open ports required
2. Set `APP_USER` and `APP_PASS` environment variables to enable HTTP Basic Auth:

```bash
APP_USER=yourname APP_PASS=yourpassword uvicorn main:app --host 127.0.0.1 --port 8000
```

Both variables must be set for auth to be enforced. If either is empty, auth is bypassed (safe for local use).

---

## Nightly backup

The scheduler automatically backs up `finance.db` to `/Users/yourname/Documents/Backup/` every night at 03:00, keeping the last 30 snapshots. Change `BACKUP_DIR` and `MAX_BACKUPS` in `scheduler.py` if needed.

---

## Project structure

```
main.py          # FastAPI routes
database.py      # SQLModel models + migrations
sync.py          # Enable Banking API sync
parsers.py       # Per-bank transaction parsing
categorizer.py   # Category matching (merchant map + regex)
investments.py   # Portfolio routes + P&L
fx.py            # FX rate fetching (frankfurter.app)
market_data.py   # Market quotes (yfinance)
scheduler.py     # APScheduler jobs (sync, quotes, backup)
config.example.py
templates/       # Jinja2 HTML templates
static/          # Static assets
```

---

## License

MIT
