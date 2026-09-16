"""
Importa cookies exportados manualmente do navegador comum (ex: extensão Cookie-Editor,
formato "Export as JSON") e converte pro formato de storage_state do Playwright, salvando
em data/auth_state.json — usado como alternativa ao login via Google dentro do bot, que o
próprio Google bloqueia por detectar navegador automatizado ("Esse navegador pode não ser
seguro").

Uso: python bot/import_cookies.py caminho/para/cookies_exportados.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright

from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.submitter import is_logged_in

log = get_logger("import_cookies")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")

TARGET_DOMAIN_SUFFIX = "99freelas.com.br"

_SAME_SITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "no_restriction": "None",
    "none": "None",
    "unspecified": "Lax",
}


def _convert_cookie(raw: dict) -> dict | None:
    domain = raw.get("domain", "")
    if TARGET_DOMAIN_SUFFIX not in domain:
        return None  # ignora cookies de outros sites (ex: google.com) que possam ter vindo junto

    is_session = raw.get("session", False)
    expiration = raw.get("expirationDate")
    expires = -1 if is_session or expiration is None else int(expiration)

    same_site_raw = str(raw.get("sameSite", "lax")).lower()
    same_site = _SAME_SITE_MAP.get(same_site_raw, "Lax")

    return {
        "name": raw["name"],
        "value": raw["value"],
        "domain": domain,
        "path": raw.get("path", "/"),
        "expires": expires,
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", False)),
        "sameSite": same_site,
    }


def main() -> None:
    if len(sys.argv) != 2:
        log.error("Uso: python bot/import_cookies.py caminho/para/cookies_exportados.json")
        sys.exit(1)

    source_path = sys.argv[1]
    if not os.path.exists(source_path):
        log.error("Arquivo não encontrado: %s", source_path)
        sys.exit(1)

    with open(source_path, "r", encoding="utf-8") as f:
        raw_cookies = json.load(f)

    if not isinstance(raw_cookies, list):
        log.error("Formato inesperado — esperava uma lista de cookies (export JSON do Cookie-Editor).")
        sys.exit(1)

    cookies = [c for c in (_convert_cookie(rc) for rc in raw_cookies) if c is not None]
    if not cookies:
        log.error(
            "Nenhum cookie de %s encontrado no arquivo. Confirme que você exportou "
            "estando na aba do 99freelas.com.br (e não de outro site).",
            TARGET_DOMAIN_SUFFIX,
        )
        sys.exit(1)

    log.info("%d cookie(s) de %s encontrados, convertendo...", len(cookies), TARGET_DOMAIN_SUFFIX)

    storage_state = {"cookies": cookies, "origins": []}
    os.makedirs(os.path.dirname(AUTH_STATE_PATH), exist_ok=True)
    with open(AUTH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(storage_state, f, ensure_ascii=False, indent=2)
    log.info("Sessão salva em %s", AUTH_STATE_PATH)

    log.info("Validando a sessão importada...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_STATE_PATH)
        page = context.new_page()
        page.goto(sel.PROJECTS_LIST_URL, wait_until="networkidle")
        if is_logged_in(page):
            log.info("Sessão válida — login confirmado via cookies importados.")
        else:
            log.warning(
                "Não consegui confirmar o login com o LOGIN_SUCCESS_MARKER atual "
                "(pode ser que esse seletor ainda não esteja validado contra o site real, "
                "não necessariamente que os cookies estejam errados)."
            )
        browser.close()


if __name__ == "__main__":
    main()
