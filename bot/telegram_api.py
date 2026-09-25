"""
Transporte da Bot API do Telegram (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no .env) — só
chamadas HTTP, sem regra de negócio. Notificações ficam em notifier.py e nas views de cada
fonte (bot/sources/*/views.py); o roteamento de cliques em telegram_dispatcher.py.

Falha sempre silenciosamente (só loga um warning): Telegram fora do ar nunca pode
derrubar o ciclo do bot nem impedir o registro de uma proposta em storage.py.

Não renomear pra telegram.py: com `python bot/main.py` a pasta bot/ fica no início do
sys.path e o nome colidiria com o pacote de terceiros de mesmo nome (mesmo problema que já
houve com selectors.py).
"""
import html
import json
import os

import requests

from bot.logger_setup import get_logger

log = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
MSG_LIMIT = 4096

_OFFSET_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "telegram_offset.json")


def chat_id() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID")


def esc(text) -> str:
    """Escapa texto dinâmico (títulos, motivos) pro parse_mode HTML do Telegram."""
    return html.escape(str(text))


def truncate_escaped(text: str, max_chars: int, suffix: str = "… (veja mais no link)") -> str:
    """
    Trunca texto JÁ escapado (esc) sem partir uma entidade HTML no meio (&amp; etc.) —
    uma entidade cortada faz o Telegram rejeitar a mensagem inteira.
    """
    if len(text) <= max_chars:
        return text
    corte = text.rfind("&", max_chars - 8, max_chars)
    return text[: corte if corte != -1 else max_chars] + suffix


def send_message(text: str, reply_markup: dict | None = None) -> None:
    """Mensagem simples (HTML, sem preview de link). Não devolve o message_id — ver call()."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id():
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não configurados — notificação não enviada.")
        return

    payload = {
        "chat_id": chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(TELEGRAM_API_URL.format(token=token, method="sendMessage"), json=payload, timeout=10)
        if resp.status_code != 200:
            log.warning("Falha ao enviar notificação Telegram (%s): %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Erro ao enviar notificação Telegram: %s", e)


def call(method: str, payload: dict) -> dict | list | None:
    """
    Chamada genérica pra qualquer método da Bot API (sendMessage com teclado,
    editMessageReplyMarkup, answerCallbackQuery, getUpdates). Retorna o campo "result" da
    resposta (dict ou list, depende do método), ou None se falhar — nunca levanta exceção.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN não configurado — chamada Telegram (%s) ignorada.", method)
        return None
    try:
        resp = requests.post(TELEGRAM_API_URL.format(token=token, method=method), json=payload, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            log.warning("Chamada Telegram %s falhou: %s", method, data)
            return None
        return data.get("result")
    except Exception as e:
        log.warning("Erro na chamada Telegram %s: %s", method, e)
        return None


def send_with_keyboard(text: str, reply_markup: dict) -> int | None:
    """Manda uma mensagem com teclado e devolve o message_id (None se falhar)."""
    result = call(
        "sendMessage",
        {
            "chat_id": chat_id(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        },
    )
    return result.get("message_id") if isinstance(result, dict) else None


def edit_text(message_id: int, text: str, reply_markup: dict) -> None:
    call(
        "editMessageText",
        {
            "chat_id": chat_id(),
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        },
    )


def edit_reply_markup(message_id: int, reply_markup: dict) -> None:
    call("editMessageReplyMarkup", {"chat_id": chat_id(), "message_id": message_id, "reply_markup": reply_markup})


def static_label_keyboard(label: str) -> dict:
    """Teclado de um botão só, sem ação ("noop") — usado pra mostrar estado ("⏳ Processando...")."""
    return {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}


def single_button_keyboard(text: str, callback_data: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": callback_data}]]}


def answer_callback(callback_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    call("answerCallbackQuery", payload)


def load_offset() -> int:
    if not os.path.exists(_OFFSET_PATH):
        return 0
    with open(_OFFSET_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("offset", 0)


def save_offset(offset: int) -> None:
    os.makedirs(os.path.dirname(_OFFSET_PATH), exist_ok=True)
    tmp_path = _OFFSET_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    os.replace(tmp_path, _OFFSET_PATH)
