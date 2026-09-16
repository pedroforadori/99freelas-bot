import calendar
import os
import random
import sys
import time
from datetime import date

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # permite `python bot/main.py`

from bot import scraper, submitter
from bot import site_selectors as sel
from bot.filter import is_match
from bot.logger_setup import get_logger
from bot.storage import already_applied, proposals_sent_today, register_application

log = get_logger("main")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        log.error(
            "config.yaml não encontrado. Copie config.example.yaml para config.yaml e preencha seus critérios."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def daily_quota(monthly_quota: int) -> int:
    """Divide a cota mensal de propostas pelos dias do mês corrente, sem arredondar pra cima."""
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return monthly_quota // days_in_month


def run_cycle(page, config: dict, monthly_quota: int) -> None:
    max_per_day = daily_quota(monthly_quota)
    sent_today = proposals_sent_today()
    if sent_today >= max_per_day:
        log.info("Limite diário de %d propostas atingido (%d enviadas). Aguardando o próximo dia.", max_per_day, sent_today)
        return

    projects = scraper.fetch_open_projects(page)
    new_projects = [p for p in projects if not already_applied(p["id"])]
    log.info("%d projetos novos (de %d na página) ainda não avaliados.", len(new_projects), len(projects))

    for project in new_projects:
        if proposals_sent_today() >= max_per_day:
            log.info("Limite diário atingido no meio do ciclo, parando por hoje.")
            break

        match, reason = is_match(project, config)
        if not match:
            log.info("Ignorado: '%s' — %s", project["title"], reason)
            register_application(project["id"], project["title"], status="skipped_duplicate", detail=reason)
            continue

        success, detail = submitter.submit_proposal(page, project, config)
        status = "sent" if success else "failed"
        log.info("%s: '%s' — %s", "ENVIADA" if success else "FALHOU", project["title"], detail)
        register_application(project["id"], project["title"], status=status, detail=detail)

        # delay curto entre candidaturas dentro do mesmo ciclo, pra não parecer um robô disparando em rajada
        time.sleep(random.uniform(5, 15))


def open_authenticated_page(browser):
    """
    Autentica a sessão do navegador de uma das duas formas:
    - Se existir data/auth_state.json (gerado por `python bot/import_cookies.py`, ou por
      `python bot/manual_login.py` se a conta não usar login via Google), reaproveita essa
      sessão salva. O site usa Cloudflare Turnstile no formulário de login, que bloqueia
      login automatizado (Google OAuth e email/senha) — por isso a sessão precisa ser
      obtida fora do navegador controlado pelo bot.
    - Senão, cai pro login automático por email/senha (bot/submitter.py), usando as
      variáveis NINETY_NINE_EMAIL/NINETY_NINE_PASSWORD do .env — mantido como fallback,
      mas o Turnstile deve rejeitar essa tentativa na prática.
    Retorna a Page autenticada, ou None se não foi possível autenticar.
    """
    if os.path.exists(AUTH_STATE_PATH):
        context = browser.new_context(storage_state=AUTH_STATE_PATH)
        page = context.new_page()
        page.goto(sel.PROJECTS_LIST_URL, wait_until="networkidle")
        if submitter.is_logged_in(page):
            log.info("Sessão reaproveitada de %s", AUTH_STATE_PATH)
            return page
        log.error(
            "Sessão salva em %s expirou ou não é mais válida. "
            "Rode `python bot/import_cookies.py` de novo (exporte os cookies do 99freelas.com.br "
            "do seu navegador comum, já logado) pra renovar a sessão.",
            AUTH_STATE_PATH,
        )
        return None

    email = os.environ.get("NINETY_NINE_EMAIL")
    password = os.environ.get("NINETY_NINE_PASSWORD")
    if not email or not password:
        log.error(
            "Nenhuma sessão salva encontrada e NINETY_NINE_EMAIL/NINETY_NINE_PASSWORD não "
            "configurados no .env. Rode `python bot/import_cookies.py` (recomendado, ver "
            "CLAUDE.md) pra reaproveitar a sessão do seu navegador comum."
        )
        return None

    context = browser.new_context()
    page = context.new_page()
    if not submitter.login(page, email, password):
        log.error("Não foi possível logar. Encerrando.")
        return None
    return page


def main() -> None:
    load_dotenv()
    config = load_config()

    headless = os.environ.get("HEADLESS", "true").lower() == "true"
    interval_min = int(os.environ.get("CHECK_INTERVAL_MIN_SECONDS", 180))
    interval_max = int(os.environ.get("CHECK_INTERVAL_MAX_SECONDS", 420))
    monthly_quota = int(os.environ.get("MONTHLY_PROPOSAL_QUOTA", 240))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        page = open_authenticated_page(browser)
        if page is None:
            browser.close()
            sys.exit(1)

        log.info("Bot iniciado. Ctrl+C para parar.")
        try:
            while True:
                try:
                    run_cycle(page, config, monthly_quota)
                except Exception as e:
                    # nunca deixar uma exceção de um ciclo derrubar o processo 24/7
                    log.exception("Erro não tratado durante o ciclo: %s", e)

                wait_s = random.uniform(interval_min, interval_max)
                log.info("Aguardando %.0fs até o próximo ciclo...", wait_s)
                time.sleep(wait_s)
        except KeyboardInterrupt:
            log.info("Interrompido pelo usuário.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
