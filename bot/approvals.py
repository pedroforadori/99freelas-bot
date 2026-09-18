"""
Fila de propostas prontas aguardando aprovação humana via Telegram (ver notifier.py e
main.process_pending_approvals). Persiste em data/pending_approvals.json — mesmo padrão
de escrita atômica (.tmp + os.replace) e lock de thread de storage.py.

O campo "decision" é o que dá segurança contra crash: notifier.poll_decisions() grava a
decisão aqui IMEDIATAMENTE ao receber o clique no Telegram, antes de qualquer interação
com o Playwright. Se o processo cair entre o clique do usuário e o envio real, a decisão
já está em disco e será processada na próxima vez que o bot rodar — nada se perde.
"""
import json
import os
import threading
from datetime import datetime

_LOCK = threading.Lock()
DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pending_approvals.json")


def _load() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


def add_pending(project: dict, proposal: dict, message_id: int | None) -> None:
    with _LOCK:
        data = _load()
        data[project["id"]] = {
            "project": project,
            "proposal": proposal,
            "telegram_message_id": message_id,
            "decision": None,
            "queued_at": datetime.utcnow().isoformat(),
            "decided_at": None,
            "pending_edit": None,
        }
        _save(data)


def update_proposal(project_id: str, proposal: dict) -> bool:
    """
    Substitui a proposta pendente (usado pela edição de oferta/prazo por texto livre no
    Telegram, ANTES da decisão final — ver notifier._handle_edit_reply). Só aplica se a
    entrada existir e ainda não tiver decisão gravada, mesma proteção de
    record_decision: evita reescrever a oferta/prazo depois que o usuário já
    aprovou/rejeitou (ou entre o clique e o "⏳ Processando...", já em voo).
    """
    with _LOCK:
        data = _load()
        entry = data.get(project_id)
        if entry is None or entry["decision"] is not None:
            return False
        entry["proposal"] = proposal
        _save(data)
        return True


def set_pending_edit(project_id: str, field: str, prompt_message_id: int) -> bool:
    """
    Registra que a entrada está aguardando resposta (reply) a `prompt_message_id` — a
    mensagem de force_reply mandada por notifier._handle_edit_request pedindo o novo
    valor de `field` ("o" oferta | "p" prazo) em texto livre. Mesma proteção de
    update_proposal: recusa se a entrada não existir ou já tiver decisão gravada.
    """
    with _LOCK:
        data = _load()
        entry = data.get(project_id)
        if entry is None or entry["decision"] is not None:
            return False
        entry["pending_edit"] = {"field": field, "prompt_message_id": prompt_message_id}
        _save(data)
        return True


def find_by_prompt_message_id(prompt_message_id: int) -> tuple[str, str] | None:
    """
    Acha (project_id, field) a partir do message_id de um prompt de force_reply — é assim
    que notifier.poll_decisions correlaciona uma resposta de texto livre (que só traz
    reply_to_message.message_id) de volta à edição que a originou, sem precisar de
    nenhum parsing heurístico do texto da resposta em si.
    """
    with _LOCK:
        data = _load()
        for pid, entry in data.items():
            pending_edit = entry.get("pending_edit")
            if pending_edit and pending_edit.get("prompt_message_id") == prompt_message_id:
                return pid, pending_edit["field"]
        return None


def clear_pending_edit(project_id: str) -> None:
    with _LOCK:
        data = _load()
        entry = data.get(project_id)
        if entry is not None:
            entry["pending_edit"] = None
            _save(data)


def record_decision(project_id: str, action: str) -> bool:
    """
    Grava "approved"/"rejected" pro project_id. Idempotente e tolerante: se o id não
    existir (already resolvido, ou callback duplicado/atrasado do Telegram), não faz nada
    e retorna False — quem chama deve tratar isso como um no-op seguro, nunca uma exceção.
    Se já havia uma decisão registrada, não sobrescreve (mantém a primeira).
    """
    with _LOCK:
        data = _load()
        entry = data.get(project_id)
        if entry is None:
            return False
        if entry["decision"] is not None:
            return True  # já decidido — trata como sucesso (no-op), cobre duplo-clique/redelivery
        entry["decision"] = action
        entry["decided_at"] = datetime.utcnow().isoformat()
        _save(data)
        return True


def get_pending(project_id: str) -> dict | None:
    with _LOCK:
        data = _load()
        return data.get(project_id)


def get_decided_unresolved() -> list[dict]:
    """Retorna as entradas com decision != None — prontas pra process_pending_approvals resolver."""
    with _LOCK:
        data = _load()
        return [{"project_id": pid, **entry} for pid, entry in data.items() if entry["decision"] is not None]


def resolve(project_id: str) -> None:
    """Remove a entrada depois que main.process_pending_approvals já tratou a decisão."""
    with _LOCK:
        data = _load()
        data.pop(project_id, None)
        _save(data)
