import logging
import os
import re
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Token de bot do Telegram ("bot<id>:<segredo>") — aparece na URL das exceções do requests
# (ex: timeout de conexão em api.telegram.org) e vazaria pro console/logs/bot.log.
_TELEGRAM_TOKEN_RE = re.compile(r"bot(\d+):[A-Za-z0-9_-]{20,}")


class _RedactingFormatter(logging.Formatter):
    """Mascara o token do Telegram na linha final (mensagem + traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        return _TELEGRAM_TOKEN_RE.sub(r"bot\1:***", super().format(record))


_handlers: list[logging.Handler] = []


def _shared_handlers() -> list[logging.Handler]:
    """
    Console + arquivo criados UMA vez e compartilhados por todos os loggers. Um
    RotatingFileHandler por logger deixava o bot.log aberto N vezes — no Windows a rotação
    (renomear o arquivo) falha com ele aberto, e toda linha depois dos 5 MB era descartada.
    """
    if not _handlers:
        fmt = _RedactingFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler()
        # utf-8 explícito: o default no Windows é cp1252 e os acentos viravam "n�o"
        file_handler = RotatingFileHandler(
            os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=5,
            encoding="utf-8",
        )
        for handler in (console, file_handler):
            handler.setFormatter(fmt)
            _handlers.append(handler)
    return _handlers


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # já configurado, evita handlers duplicados

    logger.setLevel(logging.INFO)
    for handler in _shared_handlers():
        logger.addHandler(handler)

    return logger
