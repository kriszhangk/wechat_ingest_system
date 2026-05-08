import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


SERVER_BASE_URL = os.getenv("WECHAT_INGEST_SERVER_BASE_URL", "http://43.167.177.15:8000/api/v1")
WORKER_ID = os.getenv("WECHAT_INGEST_WORKER_ID", "local-win-main")
WORKER_TOKEN = os.getenv("WECHAT_INGEST_WORKER_TOKEN", "wechat-worker-2026-04-16-4f9b7c2a")
BROWSER_STATE_PATH = os.getenv("WECHAT_INGEST_BROWSER_STATE_PATH", "./data/wechat_storage_state.json")
PERSISTENT_PROFILE_DIR = os.getenv("WECHAT_INGEST_PERSISTENT_PROFILE_DIR", "./data/chrome_profile")
HEADLESS = _env_bool("WECHAT_INGEST_HEADLESS", False)
POLL_INTERVAL_SECONDS = _env_int("WECHAT_INGEST_POLL_INTERVAL_SECONDS", 120)
DEBUG_SAVE_ARTIFACTS = _env_bool("WECHAT_INGEST_DEBUG_SAVE_ARTIFACTS", False)
PARAM_CACHE_TTL_SECONDS = _env_int("WECHAT_INGEST_PARAM_CACHE_TTL_SECONDS", 7200)
