import os
import random
import signal
import sys
import time

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # permite `python bot/main.py`

from bot import approvals, notifier  # noqa: E402
from bot.logger_setup import get_logger  # noqa: E402
from bot.sources import registry  # noqa: E402
from bot.sources.base import JobSource  # noqa: E402
from bot.telegram_dispatcher import TelegramDispatcher  # noqa: E402

log = get_logger("main")


def _handle_sigterm(signum, frame):
    # docker compose stop / restart / down mandam SIGTERM, não SIGINT — convertendo pra
    # KeyboardInterrupt reaproveita o mesmo caminho de parada "limpa" (com notificação
    # via Telegram) que já existe pro Ctrl+C, em vez do processo simplesmente morrer.
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, _handle_sigterm)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        log.error(
            "config.yaml não encontrado. Copie config.example.yaml para config.yaml e preencha seus critérios."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def process_pending_approvals() -> None:
    """
    Resolve aprovações/rejeições já decididas no Telegram (decision gravada pelo
    TelegramDispatcher ANTES desta função rodar — ver bot/approvals.py). É aqui que o envio
    de verdade acontece, pela fonte dona de cada item (JobSource.resolve_approval).
    Aprovação envia mesmo acima da cota diária — o clique do usuário é a decisão.
    Usa ALL_SOURCES (não só as ativas): um item já na fila continua resolvível mesmo que a
    fonte tenha sido desligada no config.yaml depois.
    """
    for entry in approvals.get_decided_unresolved():
        registry.source_of(entry["project"]).resolve_approval(entry)


class _CycleGuard:
    """
    Roda o run_cycle de uma fonte sem deixar uma exceção derrubar o processo 24/7. Conta
    falhas consecutivas POR fonte e notifica só no início do problema e na recuperação —
    se o mesmo erro persistir por horas (ex: seletor quebrou), repetir a cada ciclo seria spam.
    """

    def __init__(self):
        self._errors: dict[str, int] = {}

    def run(self, source: JobSource, config: dict) -> None:
        errors = self._errors.get(source.name, 0)
        try:
            source.run_cycle(config)
        except Exception as e:
            errors += 1
            self._errors[source.name] = errors
            log.exception("Erro não tratado no ciclo de %s (%dª consecutiva): %s", source.name, errors, e)
            if errors == 1:
                notifier.notify_bot_status("cycle_error", f"{source.tag} {notifier.esc(e)}")
            return
        if errors > 0:
            log.info("Ciclo de %s voltou ao normal após %d falha(s) consecutiva(s).", source.name, errors)
            notifier.notify_bot_status(
                "cycle_recovered", f"{source.tag} Voltou ao normal após {errors} ciclo(s) com erro."
            )
        self._errors[source.name] = 0


def main() -> None:
    load_dotenv()
    config = load_config()
    notifier.install_error_forwarding()

    headless = os.environ.get("HEADLESS", "true").lower() == "true"
    interval_min = int(os.environ.get("CHECK_INTERVAL_MIN_SECONDS", 180))
    interval_max = int(os.environ.get("CHECK_INTERVAL_MAX_SECONDS", 420))
    approval_poll_interval = int(os.environ.get("APPROVAL_POLL_INTERVAL_SECONDS", 20))

    sources = registry.enabled_sources(config)
    log.info("Fontes ativas: %s", ", ".join(s.name for s in sources))
    dispatcher = TelegramDispatcher(registry.ALL_SOURCES, config)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless) if any(s.needs_browser for s in sources) else None
        try:
            for source in sources:
                if not source.start(browser):
                    sys.exit(1)  # ex: sessão do 99Freelas expirada (a fonte já notificou)

            log.info("Bot iniciado. Ctrl+C para parar.")
            notifier.notify_bot_status("started")
            guard = _CycleGuard()
            while True:
                for source in sources:
                    guard.run(source, config)

                # Entre varreduras, fica de olho nas aprovações respondidas no Telegram numa
                # cadência bem mais curta (APPROVAL_POLL_INTERVAL_SECONDS) — sem isso, uma
                # aprovação só seria processada no próximo ciclo completo (até
                # CHECK_INTERVAL_MAX_SECONDS), atrasando demais projetos sensíveis a velocidade.
                next_scrape_at = time.time() + random.uniform(interval_min, interval_max)
                log.info("Aguardando até %.0fs pro próximo ciclo (checando aprovações a cada %ds)...",
                         next_scrape_at - time.time(), approval_poll_interval)
                while time.time() < next_scrape_at:
                    try:
                        dispatcher.poll()
                        process_pending_approvals()
                        for source in sources:
                            source.tick(config)
                    except Exception as e:
                        # Erros aqui ficam só no log (o warning já chega no Telegram via
                        # TelegramErrorHandler) — categoria de falha diferente da varredura,
                        # não entra no contador de ciclos com erro.
                        log.exception("Erro ao processar aprovações pendentes/tarefas de fundo: %s", e)
                    time.sleep(approval_poll_interval)
        except KeyboardInterrupt:
            log.info("Interrompido (Ctrl+C ou parada do container).")
            notifier.notify_bot_status("stopped")
        except Exception as e:
            log.exception("Erro fatal fora do ciclo, bot encerrando: %s", e)
            notifier.notify_bot_status("stopped_error", str(e))
            raise
        finally:
            if browser is not None:
                browser.close()


if __name__ == "__main__":
    main()
