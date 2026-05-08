from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api.routes import router
from app.web.routes import router as web_router, templates
from app.db import Database
from app.utils.time_util import now_str

app = FastAPI(title="wechat-ingest-server")
Database()

# best-effort schema migration for sqlite MVP
try:
    from app.db import Database as _DB
    with _DB().connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(targets)").fetchall()]
        if "created_at" not in cols:
            conn.execute("ALTER TABLE targets ADD COLUMN created_at TEXT")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE targets ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE targets SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?) WHERE 1=1", (now_str(), now_str()))
except Exception:
    pass

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router, prefix="/api/v1")
app.include_router(web_router)


def format_ts(value):
    if value is None or value == "":
        return ""
    try:
        return datetime.fromtimestamp(int(str(value))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


templates.env.filters["format_ts"] = format_ts


@app.get("/health")
def health():
    return {"ok": True}
