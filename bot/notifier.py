"""
Notifica o resultado de tentativas de envio de proposta via Telegram — requer
TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env (ver README/CLAUDE.md pra como obter).

Falha sempre silenciosamente (só loga um warning): notificação nunca deve derrubar o
ciclo do bot nem impedir o registro da proposta em storage.py.
"""
import os

import requests

from bot.logger_setup import get_logger
from bot.utils import format_currency_br

log = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TEXTO_PREVIEW_CHARS = 500


def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não configurados — notificação não enviada.")
        return

    try:
        resp = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Falha ao enviar notificação Telegram (%s): %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Erro ao enviar notificação Telegram: %s", e)


def notify_proposal_result(
    project: dict, proposal: dict | None, status: str, detail: str, simulated: bool = False
) -> None:
    """
    Notifica o resultado de uma tentativa de envio de proposta (sucesso ou falha).
    `proposal` pode ser None quando a falha ocorreu antes da proposta ser montada
    (ex: já havia proposta enviada, botão "Enviar proposta" não encontrado).
    Não é chamada para projetos ignorados pelo filtro (status "skipped_duplicate" em main.py).

    simulated: True quando vem de bot/dry_run.py (dry_run=True em submit_proposal) —
    nenhuma proposta real foi enviada, só preenchida. A mensagem sai claramente marcada
    como simulação pra nunca ser confundida com um envio de verdade.
    """
    if status == "sent":
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Proposta preenchida") if simulated else ("✅", "Proposta enviada")
    else:
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Falha simulada") if simulated else ("⚠️", "Falha ao enviar proposta")

    linhas = [
        f"{emoji} <b>{titulo}</b>",
        f"<b>Projeto:</b> {project.get('title', '')}",
        f"<b>Link:</b> {project.get('url', '')}",
    ]
    if proposal:
        linhas.append(f"<b>Oferta:</b> R$ {format_currency_br(proposal['oferta'])}")
        linhas.append(f"<b>Prazo:</b> {proposal['prazo_dias']} dias")
    linhas.append(f"<b>Detalhe:</b> {detail}")
    if proposal and status == "sent":
        texto = proposal["texto"]
        preview = texto if len(texto) <= _TEXTO_PREVIEW_CHARS else texto[:_TEXTO_PREVIEW_CHARS] + "…"
        linhas.append(f"\n<b>Texto enviado:</b>\n{preview}")

    _send_telegram("\n".join(linhas))
