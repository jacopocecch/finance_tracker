from pathlib import Path

# Enable Banking credentials — get from https://enablebanking.com/
APPLICATION_ID = "your-application-id-here"
PRIVATE_KEY_PATH = Path("~/path/to/private.pem").expanduser()

# True = sandbox (dati sintetici), False = produzione
ENABLE_BANKING_SANDBOX = True

# Genera con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY = "paste-generated-key-here"

DB_PATH = Path(__file__).parent / "finance.db"
HOST = "127.0.0.1"
PORT = 8000
REDIRECT_URI = f"http://{HOST}:{PORT}/setup/callback"

SYNC_HOUR = 7
SYNC_MINUTE = 0

INVESTMENT_ACCOUNT_KEYWORDS = ["invest", "titoli", "portafoglio", "dossier", "etf", "trading"]

BACKUP_DIR = Path("~/Documents/Backup/finance_tracker").expanduser()

# HTTP Basic Auth — set as environment variables (APP_USER / APP_PASS) before launching.
# If either is empty the middleware is bypassed (useful for local dev).
# Example:  APP_USER=jacopo APP_PASS=mysecretpass uvicorn main:app ...
# APP_USER = ""
# APP_PASS = ""
