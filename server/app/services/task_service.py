import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from app.db import Database
from app.utils.time_util import now_str


db = Database()
MAX_LOOKBACK_HOURS = 24
MAX_ITEMS_PER_TARGET = 50
PROJECT_DIR = Path("/root/.openclaw/workspace/wechat_ingest_system")
LOGIN_ALERT_STATE_PATH = PROJECT_DIR / "server" / "data" / "worker_login_alert_state.json"
LOGIN_ALERT_TARGET_CHAT = str(os.getenv("WECHAT_INGEST_LOGIN_ALERT_TARGET_CHAT", "6820808476")).strip()
LOGIN_ALERT_MENTION = str(os.getenv("WECHAT_INGEST_LOGIN_ALERT_MENTION", "@fenfenzan")).strip()
try:
    LOGIN_ALERT_COOLDOWN_SECONDS = int(str(os.getenv("WECHAT_INGEST_LOGIN_ALERT_COOLDOWN_SECONDS", "1800")).strip())
except Exception:
    LOGIN_ALERT_COOLDOWN_SECONDS = 1800

LOGIN_REQUIRED_ERROR_MARKERS = [
    "未找到草稿箱入口",
    "扫码登录",
    "请扫码登录",
    "重新登录",
    "登录失效",
    "登录超时",
    "invalid session",
    "session expired",
]


def _needs_worker_login_alert(error_text: str) -> bool:
    raw = str(error_text or "").strip().lower()
    if not raw:
        return False
    return any(marker.lower() in raw for marker in LOGIN_REQUIRED_ERROR_MARKERS)


def _load_login_alert_state() -> dict:
    try:
        return json.loads(LOGIN_ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_login_alert_state(state: dict) -> None:
    try:
        LOGIN_ALERT_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _should_send_login_alert(now_ts: int) -> bool:
    state = _load_login_alert_state()
    last_sent_ts = int(state.get("last_sent_ts") or 0)
    return now_ts - last_sent_ts >= LOGIN_ALERT_COOLDOWN_SECONDS


def _mark_login_alert_sent(now_ts: int, message_text: str, target_chat: str) -> None:
    state = _load_login_alert_state()
    state.update({
        "last_sent_ts": now_ts,
        "last_sent_at": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
        "last_message": message_text,
        "last_target_chat": target_chat,
        "last_send_ok": True,
        "last_send_error": "",
    })
    _save_login_alert_state(state)


def _mark_login_alert_failed(now_ts: int, message_text: str, target_chat: str, error_text: str) -> None:
    state = _load_login_alert_state()
    state.update({
        "last_attempt_ts": now_ts,
        "last_attempt_at": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
        "last_attempt_message": message_text,
        "last_target_chat": target_chat,
        "last_send_ok": False,
        "last_send_error": (error_text or "")[:500],
    })
    _save_login_alert_state(state)


def _send_worker_login_alert(account_name: str, task, error_text: str) -> bool:
    now_ts = int(datetime.now().timestamp())
    if not _needs_worker_login_alert(error_text):
        return False
    if not _should_send_login_alert(now_ts):
        return False

    task_id = str(task["task_id"])
    mention_prefix = f"{LOGIN_ALERT_MENTION} " if (LOGIN_ALERT_MENTION and str(LOGIN_ALERT_TARGET_CHAT).startswith("-")) else ""
    message_text = (
        f"{mention_prefix}⚠️ 微信 worker 可能掉登录态了，需要扫码重登\n"
        f"公众号: {account_name or '-'}\n"
        f"任务: {task_id[:8]}\n"
        f"错误: {str(error_text or '').strip()[:160]}\n"
        f"创建: {task['created_at'] or '-'}\n"
        f"min_publish_time: {task['min_publish_time'] or '-'}"
    )
    try:
        result = subprocess.run(
            [
                "openclaw", "message", "send",
                "--channel", "telegram",
                "--target", LOGIN_ALERT_TARGET_CHAT,
                "--message", message_text,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode == 0:
            _mark_login_alert_sent(now_ts, message_text, LOGIN_ALERT_TARGET_CHAT)
            return True
        error_summary = "\n".join(x for x in [result.stdout.strip(), result.stderr.strip()] if x).strip() or f"openclaw message send exited {result.returncode}"
        _mark_login_alert_failed(now_ts, message_text, LOGIN_ALERT_TARGET_CHAT, error_summary)
    except Exception as exc:
        _mark_login_alert_failed(now_ts, message_text, LOGIN_ALERT_TARGET_CHAT, repr(exc))
        return False
    return False


class TaskService:
    def poll_tasks(self, worker_id: str, limit: int = 5):
        with db.connect() as conn:
            now = now_str()
            conn.execute(
                "INSERT INTO workers(worker_id,last_seen_at,status,version) VALUES(?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,status=excluded.status,version=excluded.version",
                (worker_id, now, "idle", "0.1.0")
            )
            rows = conn.execute(
                "SELECT * FROM targets WHERE enabled=1 ORDER BY priority DESC, id ASC"
            ).fetchall()
            tasks = []
            for row in rows:
                if len(tasks) >= limit:
                    break
                if not self._should_dispatch(conn, row, now):
                    continue
                task_id = str(uuid.uuid4())
                force_full_sync = int(row["force_full_sync_once"] or 0) == 1
                latest_publish_ts = self._latest_publish_ts(conn, row["id"])
                lookback_cutoff = int((datetime.now() - timedelta(hours=MAX_LOOKBACK_HOURS)).timestamp())
                if force_full_sync:
                    min_publish_time = lookback_cutoff
                elif latest_publish_ts is not None:
                    min_publish_time = max(latest_publish_ts, lookback_cutoff)
                else:
                    min_publish_time = lookback_cutoff
                conn.execute(
                    "INSERT INTO tasks(task_id,target_id,assigned_worker_id,force_full_sync,status,created_at,min_publish_time) VALUES(?,?,?,?,?,?,?)",
                    (task_id, row["id"], worker_id, 1 if force_full_sync else 0, "pending", now, str(min_publish_time) if min_publish_time is not None else None)
                )
                conn.execute(
                    "UPDATE targets SET last_dispatched_at=?, force_full_sync_once=0, updated_at=? WHERE id=?",
                    (now, now, row["id"])
                )
                tasks.append({
                    "task_id": task_id,
                    "target_id": row["id"],
                    "account_name": row["account_name"],
                    "keyword": row["keyword"],
                    "priority": row["priority"],
                    "resolved_alias": row["resolved_alias"],
                    "resolved_fakeid": row["resolved_fakeid"],
                    "min_publish_time": min_publish_time,
                    "max_items": MAX_ITEMS_PER_TARGET,
                })
            return tasks

    def _should_dispatch(self, conn, target_row, now_text: str):
        try:
            now_dt = datetime.fromisoformat(now_text)
        except Exception:
            now_dt = datetime.now()

        pending_rows = conn.execute(
            "SELECT id, created_at FROM tasks WHERE target_id=? AND status='pending' ORDER BY id DESC",
            (target_row["id"],)
        ).fetchall()
        for row in pending_rows:
            try:
                created_dt = datetime.fromisoformat(row["created_at"])
            except Exception:
                created_dt = None
            if created_dt is None or now_dt - created_dt > timedelta(minutes=10):
                conn.execute(
                    "UPDATE tasks SET status='failed', reported_at=?, error_message=? WHERE id=?",
                    (now_text, 'stale pending auto-closed by scheduler', row["id"])
                )
            else:
                return False

        if int(target_row["force_full_sync_once"] or 0) == 1:
            return True

        last_dispatched_at = target_row["last_dispatched_at"]
        if not last_dispatched_at:
            return True

        try:
            last_dt = datetime.fromisoformat(last_dispatched_at)
        except Exception:
            return True

        interval_minutes = target_row["check_interval_minutes"] or 180
        return now_dt >= last_dt + timedelta(minutes=interval_minutes)

    def _to_timestamp(self, value):
        if value is None or value == "":
            return None
        try:
            return int(str(value))
        except Exception:
            return None

    def _latest_publish_ts(self, conn, target_id: int):
        rows = conn.execute(
            "SELECT publish_time FROM articles WHERE target_id=? AND publish_time IS NOT NULL",
            (target_id,)
        ).fetchall()
        values = [self._to_timestamp(r["publish_time"]) for r in rows]
        values = [x for x in values if x is not None]
        return max(values) if values else None

    def report_result(self, payload):
        pending_fetches = []

        with db.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (payload.task_id,)).fetchone()
            if not task:
                return []

            stats_payload = payload.stats
            if stats_payload is None:
                task_stats = {}
            elif hasattr(stats_payload, "dict"):
                task_stats = stats_payload.dict()
            else:
                task_stats = stats_payload.model_dump()
            task_stats["reported_item_count"] = len(payload.items or [])

            conn.execute(
                "UPDATE tasks SET status=?, reported_at=?, error_message=?, task_stats_json=? WHERE task_id=?",
                (
                    "done" if payload.success else "failed",
                    now_str(),
                    payload.error,
                    json.dumps(task_stats, ensure_ascii=False),
                    payload.task_id,
                )
            )

            if payload.resolved_nickname or payload.resolved_alias or payload.resolved_fakeid:
                conn.execute(
                    "UPDATE targets SET resolved_nickname=COALESCE(?, resolved_nickname), resolved_alias=COALESCE(?, resolved_alias), resolved_fakeid=COALESCE(?, resolved_fakeid), last_resolved_at=?, updated_at=? WHERE id=?",
                    (payload.resolved_nickname, payload.resolved_alias, payload.resolved_fakeid, now_str(), now_str(), task["target_id"])
                )

            target_row = conn.execute("SELECT account_name FROM targets WHERE id=?", (task["target_id"],)).fetchone()
            account_name = (target_row["account_name"] if target_row else "") or ""

            if not payload.success:
                _send_worker_login_alert(account_name, task, payload.error)
                return []

            target_id = task["target_id"]
            force_full_sync = int(task["force_full_sync"] or 0) == 1
            latest_publish_ts = self._latest_publish_ts(conn, target_id)
            task_min_publish_time = self._to_timestamp(task["min_publish_time"])
            inserted_count = 0
            existing_url_skip_count = 0
            publish_time_skip_count = 0
            missing_publish_time_skip_count = 0
            max_items_limit = MAX_ITEMS_PER_TARGET
            limit_hit = False

            for item in payload.items:
                if inserted_count >= max_items_limit:
                    limit_hit = True
                    break
                exists = conn.execute("SELECT 1 FROM articles WHERE article_url=?", (item.url,)).fetchone()
                if exists:
                    existing_url_skip_count += 1
                    continue

                item_publish_ts = self._to_timestamp(item.publish_time)
                if task_min_publish_time is not None:
                    if item_publish_ts is None or item_publish_ts <= task_min_publish_time:
                        if item_publish_ts is None:
                            missing_publish_time_skip_count += 1
                        else:
                            publish_time_skip_count += 1
                        continue

                article = {
                    "target_id": target_id,
                    "title": item.title,
                    "article_url": item.url,
                    "publish_time": str(item_publish_ts) if item_publish_ts is not None else None,
                    "discovered_at": now_str(),
                }
                conn.execute(
                    "INSERT INTO articles(target_id,title,article_url,publish_time,discovered_at,content_md,content_html,image_urls_json,video_urls_json,fetch_status,fetch_error,fetch_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (article["target_id"], article["title"], article["article_url"], article["publish_time"], article["discovered_at"], None, None, None, None, 'pending', None, now_str())
                )
                pending_fetches.append(article)
                inserted_count += 1

            task_stats.update({
                "server_force_full_sync": force_full_sync,
                "server_max_items_limit": max_items_limit,
                "server_limit_hit": limit_hit,
                "server_inserted_count": inserted_count,
                "server_existing_url_skip_count": existing_url_skip_count,
                "server_publish_time_skip_count": publish_time_skip_count,
                "server_missing_publish_time_skip_count": missing_publish_time_skip_count,
                "task_min_publish_time": task_min_publish_time,
                "latest_publish_ts_before_insert": latest_publish_ts,
            })
            conn.execute(
                "UPDATE tasks SET task_stats_json=? WHERE task_id=?",
                (json.dumps(task_stats, ensure_ascii=False), payload.task_id)
            )

        return pending_fetches

    def fetch_and_export_articles(self, pending_fetches: list[dict]):
        if not pending_fetches:
            return

        from app.services.article_fetcher import ArticleFetcher, ArticleDeletedError
        from app.services.article_service import ArticleService
        fetcher = ArticleFetcher()
        exporter = ArticleService()

        for article in pending_fetches:
            try:
                detail = fetcher.fetch(article["article_url"])
                content_md = detail.get("content_md")
                content_html = detail.get("content_html")
                image_urls = detail.get("image_urls") or []
                video_urls = detail.get("video_urls") or []
                final_title = detail.get("title") or article["title"]

                with db.connect() as conn:
                    conn.execute(
                        "UPDATE articles SET title=?, content_md=?, content_html=?, image_urls_json=?, video_urls_json=?, fetch_status='done', fetch_error=NULL, fetch_updated_at=? WHERE article_url=?",
                        (
                            final_title,
                            content_md,
                            content_html,
                            json.dumps(image_urls, ensure_ascii=False),
                            json.dumps(video_urls, ensure_ascii=False),
                            now_str(),
                            article["article_url"],
                        )
                    )

                exporter.export_markdown({
                    "title": final_title,
                    "article_url": article["article_url"],
                    "publish_time": article["publish_time"],
                    "content_md": content_md,
                    "image_urls": image_urls,
                    "video_urls": video_urls,
                })
            except ArticleDeletedError as e:
                try:
                    with db.connect() as conn:
                        conn.execute(
                            "UPDATE articles SET fetch_status=?, fetch_error=?, fetch_updated_at=? WHERE article_url=?",
                            (getattr(e, "status", "deleted"), str(e), now_str(), article["article_url"])
                        )
                except Exception:
                    pass
            except Exception as e:
                try:
                    with db.connect() as conn:
                        conn.execute(
                            "UPDATE articles SET fetch_status='failed', fetch_error=?, fetch_updated_at=? WHERE article_url=?",
                            (str(e), now_str(), article["article_url"])
                        )
                except Exception:
                    pass
                continue
