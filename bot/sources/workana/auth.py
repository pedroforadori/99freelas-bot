"""
Sessão do Workana. Igual ao 99Freelas, vem de cookies exportados do navegador comum
(`python bot/import_cookies.py --site workana export.json`), gravados em
data/workana_auth_state.json — nunca o auth_state.json do 99Freelas.

Cloudflare (testado em 2026-10-23, IP residencial): o Chromium headless padrão
("headless shell") leva 403 "Just a moment..." em tudo; o headless novo
(channel="chromium") também, enquanto o User-Agent anuncia "HeadlessChrome". Com o
headless novo + User-Agent de um Chrome comum, o desafio passa sozinho, sem janela. Por
isso o Workana abre o PRÓPRIO navegador (launch), em vez do compartilhado do main.py.
O /dashboard tem um desafio mais duro (não passou em 25s) — não depender dele.
"""
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "workana_auth_state.json")

DOMAIN_SUFFIX = "workana.com"
LISTING_URL = "https://www.workana.com/jobs?language=pt&category=it-programming"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Validado contra o HTML real da listagem logada (2026-10-23): o menu do usuário tem o
# link de sair; deslogado, não existe.
LOGGED_IN_MARKER = "a[href$='/logout']"


def launch(playwright):
    return playwright.chromium.launch(headless=True, channel="chromium")


def launch_from(browser):
    """Navegador próprio a partir do compartilhado do main.py (mesmo Playwright, outro modo)."""
    return browser.browser_type.launch(headless=True, channel="chromium")


def session_file_mtime() -> float | None:
    """mtime do arquivo de sessão (None se não existe) — pra perceber uma importação nova."""
    try:
        return os.path.getmtime(AUTH_STATE_PATH)
    except OSError:
        return None


def new_context(browser, storage_state=None):
    """
    Contexto com o script de desafio "de fundo" do Cloudflare bloqueado. Testado em
    2026-10-23: a 1ª página de um contexto abre em ~1s, mas esse script grava um
    cf_clearance que, no headless, faz TODA navegação seguinte cair num desafio que não se
    resolve (403 "Um momento..." por 40s) — inclusive o POST do formulário de proposta.
    Sem o script, listagem → projeto → formulário no mesmo contexto passam todas.
    """
    context = browser.new_context(locale="pt-BR", user_agent=USER_AGENT, storage_state=storage_state)
    context.route("**/cdn-cgi/challenge-platform/**", lambda route: route.abort())
    return context


def wait_challenge(page, timeout_seconds: int = 25) -> bool:
    """Espera o desafio do Cloudflare ("Just a moment..."/"Um momento...") sair. True = passou."""
    for _ in range(timeout_seconds):
        if "oment" not in page.title():
            return True
        time.sleep(1)
    return "oment" not in page.title()


def is_logged_in(page) -> bool:
    # Presença no DOM (nunca visibilidade, nem clique — é o link de SAIR).
    return page.query_selector(LOGGED_IN_MARKER) is not None


def goto(page, url: str) -> bool:
    """
    Navega e espera o desafio do Cloudflare sair. True = página carregou. Espera só o
    "commit" no goto: com o desafio no meio, esperar "domcontentloaded" estourou 45s numa
    página de projeto (2026-10-23) — quem espera o desafio é wait_challenge (título).
    """
    page.goto(url, wait_until="commit", timeout=30000)
    if not wait_challenge(page, timeout_seconds=40):
        return False
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    return True


def open_listing(page) -> bool:
    """Abre a listagem e espera o Cloudflare. True = página carregou (logado ou não)."""
    return goto(page, LISTING_URL)


def validate_session(playwright, auth_state_path: str) -> bool:
    """Usada pelo import_cookies.py: abre a listagem com a sessão gravada e checa o login."""
    browser = launch(playwright)
    try:
        page = new_context(browser, auth_state_path).new_page()
        return open_listing(page) and is_logged_in(page)
    finally:
        browser.close()
