"""Sessão autenticada do 99Freelas no Playwright (usada pelo bot real e pelo dry_run)."""
import json
import os

from bot import notifier, submitter
from bot import site_selectors as sel
from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")


def save_session(page) -> bool:
    """
    Grava os cookies ATUAIS do navegador em data/auth_state.json. O site renova a sessão
    enquanto o bot navega, mas só na memória do Chromium — o arquivo continuava com os
    cookies da última importação. Confirmado em produção (2026-10-20): um hot reload
    reiniciou o processo, ele leu o auth_state.json de 18/09 e parou com "sessão expirou",
    embora o processo antigo estivesse logado. Chamado a cada varredura bem-sucedida; só
    grava se a página ainda está logada (nunca troca uma sessão boa por uma deslogada).
    Escrita atômica (.tmp + os.replace). Falha só loga — nunca derruba o ciclo.
    """
    try:
        if not submitter.is_logged_in(page):
            log.warning("Sessão do 99Freelas parece deslogada — auth_state.json NÃO foi atualizado.")
            return False
        estado = page.context.storage_state()
        os.makedirs(os.path.dirname(AUTH_STATE_PATH), exist_ok=True)
        tmp_path = AUTH_STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, AUTH_STATE_PATH)
        return True
    except Exception as e:
        log.info("Não consegui salvar a sessão do 99Freelas: %s", e)
        return False


def session_file_mtime() -> float | None:
    """mtime do auth_state.json (None se não existe) — pra perceber uma importação nova."""
    try:
        return os.path.getmtime(AUTH_STATE_PATH)
    except OSError:
        return None


def _load_storage_state():
    """
    Conteúdo do auth_state.json pro Playwright. Aceita também o export cru do Cookie-Editor
    (lista de cookies) colado direto no arquivo — já aconteceu (2026-10-20), e o Playwright
    não entende esse formato; convertido com o mesmo _convert_cookie do import_cookies.py.
    """
    with open(AUTH_STATE_PATH, encoding="utf-8") as f:
        estado = json.load(f)
    if isinstance(estado, list):
        from bot.import_cookies import _convert_cookie

        log.info("auth_state.json está no formato do Cookie-Editor — convertendo.")
        estado = {"cookies": [c for c in map(_convert_cookie, estado) if c], "origins": []}
    return estado


def reopen_session(page):
    """
    Troca o contexto do navegador por um novo, a partir do auth_state.json atual (usado
    quando a sessão caiu com o bot rodando e o usuário importou cookies novos). Retorna
    (page, logado) — a Page é sempre válida (a antiga é fechada), mesmo se não logou.
    """
    browser = page.context.browser
    try:
        page.context.close()
    except Exception:
        pass
    context = browser.new_context(storage_state=_load_storage_state())
    nova = context.new_page()
    nova.goto(sel.PROJECTS_LIST_URL, wait_until="networkidle")
    return nova, submitter.is_logged_in(nova)


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
        context = browser.new_context(storage_state=_load_storage_state())
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
