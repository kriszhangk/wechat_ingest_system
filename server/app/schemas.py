from pydantic import BaseModel
from typing import List, Optional, Union


class WorkerPollRequest(BaseModel):
    worker_id: str
    version: Optional[str] = None
    status: Optional[str] = "idle"


class TaskItem(BaseModel):
    task_id: str
    target_id: int
    account_name: str
    keyword: str
    priority: int
    resolved_alias: Optional[str] = None
    resolved_fakeid: Optional[str] = None
    min_publish_time: Optional[int] = None
    max_items: Optional[int] = None


class WorkerPollResponse(BaseModel):
    tasks: List[TaskItem]
    next_poll_seconds: int = 120


class ArticleItem(BaseModel):
    title: str
    url: str
    publish_time: Optional[Union[str, int]] = None


class WorkerFetchStats(BaseModel):
    min_publish_time: Optional[int] = None
    params_source: Optional[str] = None
    params_refreshed: bool = False
    retry_after_refresh: bool = False
    pages_fetched: int = 0
    raw_item_count: int = 0
    accepted_item_count: int = 0
    filtered_by_min_publish_time: int = 0
    skipped_missing_title_or_url: int = 0
    skipped_missing_publish_time: int = 0
    duplicate_count: int = 0
    oldest_ts_seen: Optional[int] = None


class WorkerReportRequest(BaseModel):
    worker_id: str
    task_id: str
    success: bool
    error: Optional[str] = None
    resolved_nickname: Optional[str] = None
    resolved_alias: Optional[str] = None
    resolved_fakeid: Optional[str] = None
    items: List[ArticleItem] = []
    stats: Optional[WorkerFetchStats] = None
