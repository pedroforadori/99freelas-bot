import json
import os
import threading
from datetime import date, datetime

_LOCK = threading.Lock()
DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "applied_jobs.json")


def _load() -> dict:
    if not os.path.exists(DATA_PATH):
        return {"applied": {}, "daily_count": {}}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)  # escrita atômica, evita corromper o arquivo


def already_applied(project_id: str) -> bool:
    with _LOCK:
        data = _load()
        return project_id in data["applied"]


def register_application(project_id: str, title: str, status: str, detail: str = "") -> None:
    with _LOCK:
        data = _load()
        data["applied"][project_id] = {
            "title": title,
            "status": status,  # "sent" | "failed" | "skipped_duplicate"
            "detail": detail,
            "timestamp": datetime.utcnow().isoformat(),
        }
        today = date.today().isoformat()
        if status == "sent":
            data["daily_count"][today] = data["daily_count"].get(today, 0) + 1
        _save(data)


def proposals_sent_today() -> int:
    with _LOCK:
        data = _load()
        return data["daily_count"].get(date.today().isoformat(), 0)
