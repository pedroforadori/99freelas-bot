"""
Script auxiliar pra logar manualmente no 99Freelas (inclusive via "Entrar com Google")
e salvar a sessão (cookies) num arquivo local, que o bot reaproveita em toda execução.

Necessário porque automatizar o clique no login do Google via Playwright é frágil e
sujeito a bloqueio do Google por comportamento automatizado — mais seguro logar uma
vez manualmente e reusar a sessão.

Rode: python bot/manual_login.py
Repita sempre que o bot avisar que a sessão salva expirou.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright

from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.submitter import is_logged_in

log = get_logger("manual_login")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")


def main() -> None:
    os.makedirs(os.path.dirname(AUTH_STATE_PATH), exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(sel.LOGIN_URL, wait_until="networkidle")

        log.info("Faça login manualmente na janela do navegador (email/senha ou Google, como preferir).")
        input("Depois de logar com sucesso, volte aqui e pressione ENTER... ")

        if not is_logged_in(page):
            log.error(
                "Não detectei login bem-sucedido (LOGIN_SUCCESS_MARKER não encontrado em site_selectors.py). "
                "Sessão NÃO foi salva — confirme que o login realmente completou e rode de novo."
            )
            browser.close()
            sys.exit(1)

        context.storage_state(path=AUTH_STATE_PATH)
        log.info("Sessão salva em %s", AUTH_STATE_PATH)
        browser.close()


if __name__ == "__main__":
    main()
