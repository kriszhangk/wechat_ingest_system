import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "server" / "data"
DB_PATH = DATA_DIR / "app.db"
EXPORT_DIR = DATA_DIR / "exports"
WORKER_TOKEN = os.getenv("WECHAT_INGEST_WORKER_TOKEN", "wechat-worker-2026-04-16-4f9b7c2a")

DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
