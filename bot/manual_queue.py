"""
Fila de projetos enviados manualmente pelo usuário (link colado no chat do Telegram) pra
passarem pelo mesmo fluxo de aprovação de um projeto novo da varredura — ver
Freelas99Source._on_link_action (enfileira) e Freelas99Source.process_manual_projects (prepara),
em bot/sources/freelas99/source.py.

Existe separada de approvals.py porque o polling do Telegram nunca toca o Playwright:
ele só grava o link aqui, e o loop principal (dono da Page) prepara a proposta depois.
Persiste em data/manual_queue.json — mesmo padrão de escrita atômica (.tmp + os.replace)
e lock de thread de storage.py, pra um link recebido não se perder se o processo cair
antes de ser processado.
"""
import json
import os
import threading
from datetime import datetime

_LOCK = threading.Lock()
DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "manual_queue.json")


def _load() -> list:
    if not os.path.exists(DATA_PATH):
        return []
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: list) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


def add(project_id: str, url: str) -> bool:
    """Enfileira o projeto. Retorna False se ele já estava na fila (link colado duas vezes)."""
    with _LOCK:
        data = _load()
        if any(item["id"] == project_id for item in data):
            return False
        data.append({"id": project_id, "url": url, "queued_at": datetime.utcnow().isoformat()})
        _save(data)
        return True


def peek_all() -> list[dict]:
    with _LOCK:
        return _load()


def remove(project_id: str) -> None:
    """Tira da fila só depois de processado — se cair no meio, é reprocessado no próximo tick."""
    with _LOCK:
        data = [item for item in _load() if item["id"] != project_id]
        _save(data)
