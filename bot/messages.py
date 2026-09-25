"""
Lê o badge de mensagens não lidas do header do site (presente em qualquer página
autenticada, ex: /dashboard — reaproveitada aqui, mesma URL já usada por connections.py)
e notifica via Telegram quando esse número AUMENTA em relação ao último valor conhecido.

Existe porque o sistema de notificação nativo do 99Freelas é lento/não confiável — isso
serve como alerta em tempo quase real de resposta de cliente, mesmo molde de
connections.py: parse via query_selector (não regex de texto solto, já que aqui há um
elemento isolado e estável), cache em data/messages_state.json com escrita atômica,
try/except que nunca derruba o ciclo em caso de falha.
"""
import json
import os
import threading
from datetime import datetime

from bot.sources.freelas99 import views
from bot import site_selectors as sel
from bot.logger_setup import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "messages_state.json")


def _load_cache() -> dict | None:
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CACHE_PATH)  # escrita atômica, evita corromper o arquivo


def read_cached() -> dict | None:
    with _LOCK:
        return _load_cache()


def _read_unread_count(page) -> int:
    container = page.query_selector(sel.MESSAGES_BADGE_CONTAINER)
    if container is None:
        return 0
    classes = (container.get_attribute("class") or "").split()
    if "show" not in classes:
        # Container existe mas está escondido — sem mensagens não lidas, mesmo que o
        # <span> interno ainda tenha um valor antigo.
        return 0
    valor = page.query_selector(sel.MESSAGES_BADGE_VALUE)
    texto = (valor.inner_text().strip() if valor else "")
    return int(texto) if texto.isdigit() else 0


def refresh(page) -> dict | None:
    """
    Navega pra /dashboard, lê o badge de mensagens não lidas no header e salva no cache.
    Em caso de falha (layout mudou, elemento ausente, erro de rede), loga warning e
    devolve o último cache válido em vez de derrubar o ciclo — mesma filosofia de
    connections.refresh.
    """
    try:
        # "load" em vez de "networkidle": mesmo timeout intermitente confirmado em
        # connections.py (produção, 2026-09-18) — nesta sessão, 6/6 checagens de
        # mensagens falharam por timeout de networkidle em /dashboard, então a
        # notificação de mensagens novas nunca chegou a funcionar de fato até este fix.
        page.goto(sel.DASHBOARD_URL, wait_until="load")
        data = {
            "unread_count": _read_unread_count(page),
            "fetched_at": datetime.utcnow().isoformat(),
        }
        with _LOCK:
            _save_cache(data)
        return data
    except Exception as e:
        log.warning("Erro ao checar mensagens não lidas: %s", e)
        return read_cached()


def check_and_notify(page) -> None:
    """
    Chamada a partir do loop em main.py/dry_run.py. Compara o valor cacheado ANTES deste
    refresh com o novo valor lido — só notifica quando o contador AUMENTA (uma queda ou
    igualdade não gera nada, pra não virar ruído quando o usuário lê as mensagens direto
    no site, fora do bot).
    """
    anterior = read_cached()
    anterior_count = anterior["unread_count"] if anterior else 0

    novo = refresh(page)
    if novo is None:
        return

    if novo["unread_count"] > anterior_count:
        views.notify_new_messages(novo["unread_count"], anterior_count)
