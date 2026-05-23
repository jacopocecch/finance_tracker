import shutil
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session
from sync import sync_all
from database import engine
import config

log = logging.getLogger(__name__)
_scheduler = BackgroundScheduler()

BACKUP_DIR = Path("/Users/jacopo/Documents/Backup")
MAX_BACKUPS = 30


def _backup_db():
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = BACKUP_DIR / f"finance_{ts}.db"
        shutil.copy2(config.DB_PATH, dest)
        # Keep only the most recent MAX_BACKUPS files
        backups = sorted(BACKUP_DIR.glob("finance_*.db"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink()
        log.info(f"DB backup → {dest}")
    except Exception as e:
        log.error(f"DB backup failed: {e}")


def _refresh_quotes():
    from market_data import refresh_all_quotes
    with Session(engine) as session:
        refresh_all_quotes(session)


def start():
    _scheduler.add_job(
        sync_all,
        trigger="cron",
        hour=config.SYNC_HOUR,
        minute=config.SYNC_MINUTE,
        id="daily_sync",
        replace_existing=True,
    )
    _scheduler.add_job(
        _refresh_quotes,
        trigger="cron",
        hour=17,
        minute=45,
        id="daily_quotes",
        replace_existing=True,
    )
    _scheduler.add_job(
        _backup_db,
        trigger="cron",
        hour=3,
        minute=0,
        id="daily_backup",
        replace_existing=True,
    )
    _scheduler.start()


def stop():
    _scheduler.shutdown(wait=False)
