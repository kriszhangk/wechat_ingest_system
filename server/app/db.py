import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH


class Database:
    def __init__(self):
        self.init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self):
        with self.connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass

            conn.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                keyword TEXT NOT NULL,
                resolved_nickname TEXT,
                resolved_alias TEXT,
                resolved_fakeid TEXT,
                last_resolved_at TEXT,
                force_full_sync_once INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 10,
                check_interval_minutes INTEGER NOT NULL DEFAULT 180,
                last_dispatched_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                target_id INTEGER NOT NULL,
                assigned_worker_id TEXT,
                force_full_sync INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reported_at TEXT,
                error_message TEXT,
                task_stats_json TEXT,
                min_publish_time TEXT,
                FOREIGN KEY(target_id) REFERENCES targets(id)
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER,
                title TEXT NOT NULL,
                article_url TEXT NOT NULL UNIQUE,
                publish_time TEXT,
                discovered_at TEXT NOT NULL,
                content_md TEXT,
                content_html TEXT,
                image_urls_json TEXT,
                video_urls_json TEXT,
                fetch_status TEXT NOT NULL DEFAULT 'pending',
                fetch_error TEXT,
                fetch_updated_at TEXT,
                user_pref_state TEXT,
                user_pref_updated_at TEXT,
                FOREIGN KEY(target_id) REFERENCES targets(id)
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT NOT NULL UNIQUE,
                last_seen_at TEXT,
                status TEXT,
                version TEXT
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                window_start TEXT,
                window_end TEXT,
                stats_json TEXT,
                report_md TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)

            target_cols = [r[1] for r in conn.execute("PRAGMA table_info(targets)").fetchall()]
            if "resolved_nickname" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN resolved_nickname TEXT")
            if "resolved_alias" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN resolved_alias TEXT")
            if "resolved_fakeid" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN resolved_fakeid TEXT")
            if "last_resolved_at" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN last_resolved_at TEXT")
            if "force_full_sync_once" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN force_full_sync_once INTEGER NOT NULL DEFAULT 0")
            if "created_at" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN created_at TEXT")
            if "updated_at" not in target_cols:
                conn.execute("ALTER TABLE targets ADD COLUMN updated_at TEXT")

            task_cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            if "force_full_sync" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN force_full_sync INTEGER NOT NULL DEFAULT 0")
            if "min_publish_time" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN min_publish_time TEXT")
            if "task_stats_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN task_stats_json TEXT")

            article_cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
            if "image_urls_json" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN image_urls_json TEXT")
            if "video_urls_json" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN video_urls_json TEXT")
            if "fetch_status" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN fetch_status TEXT DEFAULT 'pending'")
            if "fetch_error" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN fetch_error TEXT")
            if "fetch_updated_at" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN fetch_updated_at TEXT")
            if "user_pref_state" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN user_pref_state TEXT")
            if "user_pref_updated_at" not in article_cols:
                conn.execute("ALTER TABLE articles ADD COLUMN user_pref_updated_at TEXT")

            conn.execute("UPDATE articles SET fetch_status='done' WHERE COALESCE(content_md, '') != '' AND COALESCE(fetch_status, '') IN ('', 'pending')")
            conn.execute("UPDATE articles SET fetch_status='pending' WHERE fetch_status IS NULL OR fetch_status=''")
            conn.execute("UPDATE articles SET fetch_status='deleted_author' WHERE fetch_status='deleted' AND fetch_error IN ('该内容已被发布者删除','此内容已被发布者删除','内容已被发布者删除')")
            conn.execute("UPDATE articles SET fetch_status='deleted_violation' WHERE fetch_status='deleted' AND fetch_error IN ('该内容因违规无法查看','此内容因违规无法查看')")
            conn.execute("UPDATE articles SET fetch_status='deleted_complaint' WHERE fetch_status='deleted' AND fetch_error='此内容被投诉且经审核涉嫌侵权，无法查看'")
            conn.execute("UPDATE articles SET fetch_status='deleted_unavailable' WHERE fetch_status='deleted' AND fetch_error='此内容已无法查看'")

            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_account_name_unique ON targets(account_name)")
            except Exception:
                pass
            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_keyword_unique ON targets(keyword)")
            except Exception:
                pass
