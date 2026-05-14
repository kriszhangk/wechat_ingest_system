import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from app.api.client import ServerClient
from app.collector.wechat_collector import WechatCollector
from app.config import WORKER_ID, POLL_INTERVAL_SECONDS, HOMEPAGE_REFRESH_INTERVAL_SECONDS


class WorkerRunner:
    def __init__(self):
        self.client = ServerClient()
        self.collector = WechatCollector()
        self.log_dir = Path("./data/debug")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "runner.log"
        self.last_homepage_refresh_at = None

    def log(self, message: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [runner] {message}"
        print(line, flush=True)
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _homepage_refresh_due(self):
        if HOMEPAGE_REFRESH_INTERVAL_SECONDS <= 0:
            return False
        if self.last_homepage_refresh_at is None:
            return True
        return datetime.now() - self.last_homepage_refresh_at >= timedelta(seconds=HOMEPAGE_REFRESH_INTERVAL_SECONDS)

    def _refresh_homepage_if_due(self):
        if not self._homepage_refresh_due():
            return
        self.log(
            f"达到公众号首页保活刷新间隔，暂停获取任务并刷新页面: interval_seconds={HOMEPAGE_REFRESH_INTERVAL_SECONDS}"
        )
        self.collector.refresh_homepage_session(reason="scheduled_before_poll")
        self.last_homepage_refresh_at = datetime.now()
        self.log("公众号首页刷新成功，恢复获取任务")

    def run_once(self):
        self._refresh_homepage_if_due()
        self.log("开始 poll server")
        data = self.client.poll()
        tasks = data.get("tasks", [])
        self.log(f"poll 完成，拿到任务数={len(tasks)}")
        if not tasks:
            next_poll = data.get("next_poll_seconds", POLL_INTERVAL_SECONDS)
            self.log(f"当前无任务，next_poll_seconds={next_poll}")
            return next_poll

        for task in tasks:
            task_id = task["task_id"]
            keyword = task["keyword"]
            resolved_fakeid = task.get("resolved_fakeid")
            min_publish_time = task.get("min_publish_time")
            max_items = task.get("max_items")
            self.log(f"开始处理任务 task_id={task_id}, keyword={keyword}, resolved_fakeid={resolved_fakeid}, min_publish_time={min_publish_time}, max_items={max_items}")
            try:
                items, resolved, stats = self.collector.fetch_articles(keyword, resolved_fakeid=resolved_fakeid, min_publish_time=min_publish_time, max_items=max_items)
                self.log(
                    f"任务成功 task_id={task_id}, items={len(items)}, raw={stats.get('raw_item_count', 0)}, filtered={stats.get('filtered_by_min_publish_time', 0)}, dup={stats.get('duplicate_count', 0)}, params_source={stats.get('params_source')}，准备上报 server"
                )
                self.client.report({
                    "worker_id": WORKER_ID,
                    "task_id": task_id,
                    "success": True,
                    "error": None,
                    "resolved_nickname": resolved.get("nickname"),
                    "resolved_alias": resolved.get("alias"),
                    "resolved_fakeid": resolved.get("fakeid"),
                    "items": items,
                    "stats": stats,
                })
                self.log(f"任务上报完成 task_id={task_id}, success=True")
            except Exception as e:
                self.log(f"任务失败 task_id={task_id}, err={e}")
                self.log(traceback.format_exc())
                stats = getattr(self.collector, "last_fetch_stats", None) or {}
                self.client.report({
                    "worker_id": WORKER_ID,
                    "task_id": task_id,
                    "success": False,
                    "error": str(e),
                    "items": [],
                    "stats": stats,
                })
                self.log(f"任务上报完成 task_id={task_id}, success=False")
        return data.get("next_poll_seconds", POLL_INTERVAL_SECONDS)

    def run_forever(self):
        self.log(f"worker 启动: WORKER_ID={WORKER_ID}, HOMEPAGE_REFRESH_INTERVAL_SECONDS={HOMEPAGE_REFRESH_INTERVAL_SECONDS}")
        self.collector.start()
        self.log("collector 已预热完成，进入轮询循环")
        while True:
            try:
                sleep_seconds = self.run_once()
            except Exception as e:
                self.log(f"run_once 异常: {e}")
                self.log(traceback.format_exc())
                sleep_seconds = POLL_INTERVAL_SECONDS
            self.log(f"sleep {sleep_seconds} 秒后继续")
            time.sleep(sleep_seconds)
