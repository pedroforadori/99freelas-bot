"""Sessão autenticada do 99Freelas no Playwright (usada pelo bot real e pelo dry_run)."""
import os

from bot import notifier, submitter
from bot import site_selectors as sel
from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")


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
        detail = (
            f"Sessão salva em {AUTH_STATE_PATH} expirou ou não é mais válida. "
            "Rode `python bot/import_cookies.py` de novo (exporte os cookies do 99freelas.com.br "
            "do seu navegador comum, já logado) pra renovar a sessão."
        )
        log.error(detail)
        notifier.notify_bot_status("auth_failed", detail)
        return None

    email = os.environ.get("NINETY_NINE_EMAIL")
    password = os.environ.get("NINETY_NINE_PASSWORD")
    if not email or not password:
        detail = (
            "Nenhuma sessão salva encontrada e NINETY_NINE_EMAIL/NINETY_NINE_PASSWORD não "
            "configurados no .env. Rode `python bot/import_cookies.py` (recomendado, ver "
            "CLAUDE.md) pra reaproveitar a sessão do seu navegador comum."
        )
        log.error(detail)
        notifier.notify_bot_status("auth_failed", detail)
        return None

    context = browser.new_context()
    page = context.new_page()
    if not submitter.login(page, email, password):
        detail = "Login por email/senha falhou (provavelmente bloqueado pelo Cloudflare Turnstile — ver CLAUDE.md)."
        log.error(detail + " Encerrando.")
        notifier.notify_bot_status("auth_failed", detail)
        return None
    return page
