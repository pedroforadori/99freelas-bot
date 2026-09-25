"""
Notificações via Telegram comuns a todas as fontes de vagas: ciclo de vida do bot,
log de atividade, encaminhamento de erros e o pedido/resultado genérico de aprovação.

Textos específicos de cada fonte (mensagem de aprovação, resultado de envio etc.) ficam em
bot/sources/<fonte>/views.py; o transporte HTTP em telegram_api.py; o polling de
cliques/respostas em telegram_dispatcher.py. Este módulo NÃO importa bot.sources (as fontes
é que importam ele) — mantém o grafo de imports sem ciclo.

Falha sempre silenciosamente (só loga um warning): notificação nunca deve derrubar o
ciclo do bot nem impedir o registro da proposta em storage.py.
"""
import logging
import os
import time

from bot import telegram_api
from bot.logger_setup import get_logger
from bot.telegram_api import esc

log = get_logger(__name__)


def notify_activity(text: str, tag: str = "") -> None:
    """
    Log de atividade rotineira do bot pro Telegram (diferente de notify_bot_status, que só
    cobre transições de ciclo de vida). Desliga com ACTIVITY_LOG_TELEGRAM=false no .env.
    `tag` é a tag de origem da fonte ("<b>[99Freelas]</b>", ver JobSource.tag) — mensagens
    sobre o bot em si (erros encaminhados) vão sem tag.
    """
    if os.environ.get("ACTIVITY_LOG_TELEGRAM", "true").strip().lower() in ("false", "0", "no", "off"):
        return
    telegram_api.send_message(f"{tag} {text}" if tag else text)


class TelegramErrorHandler(logging.Handler):
    """
    Encaminha WARNING/ERROR de qualquer módulo pro Telegram, com módulo de origem e a
    exceção (se houver) — pra mapear onde o bot mais falha. Ignora o próprio transporte
    do Telegram (evita loop: falha de envio ao Telegram gera warning, que geraria outro
    envio) e descarta repetição idêntica dentro de 60s (ex: mesmo 503 em retries seguidos).
    """

    _DEDUPE_SECONDS = 60
    _IGNORED_LOGGERS = (__name__, telegram_api.__name__)

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._recent: dict[str, float] = {}
        self._sending = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._sending or record.name in self._IGNORED_LOGGERS:
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                exc = record.exc_info[1]
                msg += f"\n{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            key = f"{record.name}|{msg}"
            now = time.time()
            if now - self._recent.get(key, 0) < self._DEDUPE_SECONDS:
                return
            self._recent = {k: t for k, t in self._recent.items() if now - t < self._DEDUPE_SECONDS}
            self._recent[key] = now
            emoji = "🔴" if record.levelno >= logging.ERROR else "⚠️"
            self._sending = True
            notify_activity(f"{emoji} <b>{record.levelname}</b> em <code>{esc(record.name)}</code>\n{esc(msg)[:3500]}")
        except Exception:
            pass
        finally:
            self._sending = False


def install_error_forwarding() -> None:
    """Liga o TelegramErrorHandler no logger raiz (chamado uma vez por main.py)."""
    logging.getLogger().addHandler(TelegramErrorHandler())


_STATUS_EMOJI = {
    "started": "🟢",
    "stopped": "🟡",
    "stopped_error": "🔴",
    "auth_failed": "🔴",
    "cycle_error": "⚠️",
    "cycle_recovered": "✅",
}
_STATUS_TITLE = {
    "started": "Bot online",
    "stopped": "Bot parado",
    "stopped_error": "Bot caiu com erro fatal",
    "auth_failed": "Bot offline — falha ao autenticar",
    "cycle_error": "Problema num ciclo (bot continua rodando)",
    "cycle_recovered": "Ciclo voltou ao normal",
}


def notify_bot_status(event: str, detail: str = "", simulated: bool = False) -> None:
    """
    Notifica mudanças de ESTADO do bot (ciclo de vida: online/offline/erro). Só dispara em
    transições de estado — rodando normalmente não gera nenhuma mensagem, de propósito,
    pra não virar ruído numa operação 24/7 sem supervisão. `event` é uma das chaves de
    _STATUS_EMOJI/_STATUS_TITLE (ver main.py pra onde cada uma é chamada).

    simulated: True quando vem de bot/dry_run.py — prefixa a mensagem pra nunca ser
    confundida com o status do bot real rodando em produção.
    """
    emoji = _STATUS_EMOJI.get(event, "ℹ️")
    titulo = _STATUS_TITLE.get(event, event)
    if simulated:
        titulo = f"[DRY RUN] {titulo}"

    linhas = [f"{emoji} <b>{titulo}</b>"]
    if detail:
        linhas.append(detail)

    telegram_api.send_message("\n".join(linhas))


# --- Fluxo de aprovação (genérico; texto/teclado vêm de JobSource.render_approval) ---
#
# O pedido de aprovação é montado por cada fonte e mandado aqui. A decisão (clique) é
# gravada por telegram_dispatcher em approvals.py IMEDIATAMENTE, antes de qualquer envio
# real — é isso que garante que uma decisão nunca se perde se o processo cair logo depois.
# finalize_approval_message troca só o teclado da mensagem original (nunca o texto) pra
# mostrar o resultado FINAL, mantendo a descrição/proposta completas no histórico do chat.


def send_approval_message(text: str, keyboard: dict) -> int | None:
    """Manda o pedido de aprovação já renderizado. Retorna o message_id, ou None se falhar."""
    return telegram_api.send_with_keyboard(text, keyboard)


def finalize_approval_message(message_id: int | None, approved: bool, detail: str) -> None:
    """
    Troca o teclado da mensagem de aprovação original por um rótulo estático mostrando o
    resultado final — nunca mexe no TEXTO da mensagem, então a descrição/proposta
    completas continuam visíveis no histórico do chat pra referência futura.
    """
    if not message_id:
        return
    label = "✅ Aprovada e enviada" if approved else "❌ Rejeitada"
    if detail:
        label = f"{label} — {detail}"
    telegram_api.edit_reply_markup(message_id, telegram_api.static_label_keyboard(label[:64]))
