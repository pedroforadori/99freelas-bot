"""
Telegram do fluxo de portfólio (bot/portfolio.py): envia as imagens capturadas como
DOCUMENTO (foto é recomprimida; documento preserva o arquivo), uma mensagem com título/
descrição e, quando possível, botões Aprovar / Refazer / Reprovar — e lê os cliques.

Os botões usam um SEGUNDO bot do Telegram (PORTFOLIO_TELEGRAM_BOT_TOKEN no .env; o chat é o
mesmo TELEGRAM_CHAT_ID). Motivo: ler cliques é getUpdates, e a confirmação (offset) é
global por bot — dois processos lendo o mesmo bot se atropelam, e este comando ler o bot
principal descartaria os cliques de aprovação de PROPOSTAS. Sem o segundo token, cai no modo
sem botões (só envia, pelo bot principal).

callback_data: "pf:<ok|re|no>:<item_id>" (limite de 64 bytes; item_id ≤ 37 chars).
"""
import json
import os

import requests

from bot import ai_writer
from bot.logger_setup import get_logger
from bot.notifier import esc

log = get_logger(__name__)


def _chat_id() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID")


def buttons_enabled() -> bool:
    return bool(os.environ.get("PORTFOLIO_TELEGRAM_BOT_TOKEN") and _chat_id())


def _token() -> str | None:
    return os.environ.get("PORTFOLIO_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")


def _call(method: str, payload: dict, timeout: int = 30):
    token = _token()
    if not token:
        log.warning("Nenhum token do Telegram configurado — chamada %s ignorada.", method)
        return None
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram %s falhou: %s", method, data)
            return None
        return data.get("result")
    except Exception as e:
        log.warning("Erro na chamada Telegram %s: %s", method, e)
        return None


def send_documents(paths: list[str]) -> bool:
    """sendMediaGroup como documentos (upload multipart)."""
    token, chat_id = _token(), _chat_id()
    if not token or not chat_id:
        log.warning("Telegram não configurado — imagens só salvas em disco.")
        return False
    handles = [open(p, "rb") for p in paths]
    try:
        media = [{"type": "document", "media": f"attach://f{i}"} for i in range(len(paths))]
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files={f"f{i}": h for i, h in enumerate(handles)},
            timeout=120,
        )
        ok = resp.json().get("ok", False)
        if not ok:
            log.warning("sendMediaGroup falhou: %s", resp.text[:300])
        return ok
    except Exception as e:
        log.warning("Erro ao enviar imagens pro Telegram: %s", e)
        return False
    finally:
        for h in handles:
            h.close()


def _text(item: dict) -> str:
    aviso = f"{item['aviso']}\n\n" if item.get("aviso") else ""
    return (
        f"🖼 <b>Portfólio: {esc(item['name'])}</b>\n<b>Fonte:</b> {esc(item['label'])}\n\n{aviso}"
        f"<b>Título</b> ({len(item['titulo'])}/{ai_writer.PORTFOLIO_TITULO_MAX}):\n<code>{esc(item['titulo'])}</code>\n\n"
        f"<b>Descrição</b> ({len(item['descricao'])}/{ai_writer.PORTFOLIO_DESCRICAO_MAX}):\n<code>{esc(item['descricao'])}</code>"
    )


def send_review(item: dict, with_buttons: bool) -> int | None:
    """Manda imagens + texto (com botões se `with_buttons`). Devolve o message_id do texto."""
    send_documents(item["images"])
    payload = {
        "chat_id": _chat_id(),
        "text": _text(item),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if with_buttons:
        i = item["id"]
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "🔁 Refazer capturas", "callback_data": f"pf:re:{i}"}],
                [
                    {"text": "✅ Aprovar", "callback_data": f"pf:ok:{i}"},
                    {"text": "❌ Reprovar", "callback_data": f"pf:no:{i}"},
                ],
            ]
        }
    result = _call("sendMessage", payload)
    return result.get("message_id") if isinstance(result, dict) else None


def set_label(message_id: int | None, label: str) -> None:
    """Troca o teclado por um rótulo estático — nunca mexe no texto da mensagem."""
    if message_id:
        _call(
            "editMessageReplyMarkup",
            {
                "chat_id": _chat_id(),
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[{"text": label[:64], "callback_data": "noop"}]]},
            },
        )


def answer(callback_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text} if text else {"callback_query_id": callback_id})


def drain() -> int:
    """Descarta cliques antigos (de execuções anteriores) e devolve o offset pra começar limpo."""
    updates = _call("getUpdates", {"offset": -1, "timeout": 0})
    if isinstance(updates, list) and updates:
        return updates[-1]["update_id"] + 1
    return 0


def poll(offset: int) -> tuple[list[dict], int]:
    """Long-poll de até 25s por cliques novos. Devolve (callback_queries, próximo offset)."""
    updates = _call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["callback_query"]}, timeout=40)
    if not isinstance(updates, list):
        return [], offset
    callbacks = [u["callback_query"] for u in updates if u.get("callback_query")]
    next_offset = max((u["update_id"] for u in updates), default=offset - 1) + 1
    return callbacks, next_offset
