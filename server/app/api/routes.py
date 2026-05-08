from fastapi import APIRouter, Header, HTTPException
import threading
from app.config import WORKER_TOKEN
from app.schemas import WorkerPollRequest, WorkerPollResponse, WorkerReportRequest
from app.services.task_service import TaskService

router = APIRouter()
service = TaskService()


def check_token(auth: str | None):
    if auth != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/worker/poll", response_model=WorkerPollResponse)
def worker_poll(payload: WorkerPollRequest, authorization: str | None = Header(default=None)):
    check_token(authorization)
    tasks = service.poll_tasks(payload.worker_id, limit=20)
    next_poll_seconds = 1 if tasks else 120
    return WorkerPollResponse(tasks=tasks, next_poll_seconds=next_poll_seconds)


@router.post("/worker/report")
def worker_report(payload: WorkerReportRequest, authorization: str | None = Header(default=None)):
    check_token(authorization)
    pending_fetches = service.report_result(payload)
    if pending_fetches:
        threading.Thread(target=service.fetch_and_export_articles, args=(pending_fetches,), daemon=True).start()
    return {"ok": True}
