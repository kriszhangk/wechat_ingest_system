import requests
from app.config import SERVER_BASE_URL, WORKER_ID, WORKER_TOKEN


class ServerClient:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {WORKER_TOKEN}",
            "Content-Type": "application/json",
        }

    def poll(self):
        resp = requests.post(
            f"{SERVER_BASE_URL}/worker/poll",
            json={"worker_id": WORKER_ID, "version": "0.1.0", "status": "idle"},
            headers=self.headers,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def report(self, payload: dict):
        resp = requests.post(
            f"{SERVER_BASE_URL}/worker/report",
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
