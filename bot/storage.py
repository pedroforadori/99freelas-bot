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


def register_application(
    project_id: str, title: str, status: str, detail: str = "", extra: dict | None = None
) -> None:
    """
    `extra`: campos adicionais gravados junto no registro — usado pra guardar a estratégia
    da proposta enviada (versão do teste A/B, oferta, origem do valor, estilo do texto; ver
    main.process_pending_approvals), base pra comparar as versões depois.
    """
    with _LOCK:
        data = _load()
        data["applied"][project_id] = {
            "title": title,
            "status": status,  # "sent" | "failed" | "skipped_duplicate"
            "detail": detail,
            "timestamp": datetime.utcnow().isoformat(),
            **(extra or {}),
        }
        today = date.today().isoformat()
        if status == "sent":
            data["daily_count"][today] = data["daily_count"].get(today, 0) + 1
        _save(data)


def record_outcome(project_id: str, resultado: str) -> str | None:
    """
    Grava o resultado de uma proposta enviada ("respondeu" | "fechou"), marcado pelo usuário
    no Telegram (link colado no chat + botão "💬 Respondeu"/"🏆 Fechou", ver
    notifier._handle_link_action). "fechou" nunca é rebaixado pra "respondeu".
    Retorna o resultado final gravado, ou None se o projeto não existir no histórico.
    """
    with _LOCK:
        data = _load()
        rec = data["applied"].get(project_id)
        if rec is None:
            return None
        if rec.get("resultado") != "fechou":
            rec["resultado"] = resultado
            rec["resultado_em"] = datetime.utcnow().isoformat()
            _save(data)
        return rec["resultado"]


def get_application(project_id: str) -> dict | None:
    with _LOCK:
        return _load()["applied"].get(project_id)


def list_by_status(status: str) -> dict:
    """{project_id: registro} de todos os registros com esse status (ex: "awaiting_average")."""
    with _LOCK:
        data = _load()
        return {pid: rec for pid, rec in data["applied"].items() if rec.get("status") == status}


def proposals_sent_today() -> int:
    with _LOCK:
        data = _load()
        return data["daily_count"].get(date.today().isoformat(), 0)
