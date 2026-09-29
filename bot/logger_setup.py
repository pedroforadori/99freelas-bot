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


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # já configurado, evita handlers duplicados

    logger.setLevel(logging.INFO)
    fmt = _RedactingFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
