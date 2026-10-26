"""
Importa cookies exportados manualmente do navegador comum (ex: extensão Cookie-Editor,
formato "Export as JSON") e converte pro formato de storage_state do Playwright, salvando
em data/auth_state.json — usado como alternativa ao login via Google dentro do bot, que o
próprio Google bloqueia por detectar navegador automatizado ("Esse navegador pode não ser
seguro").

Uso: python bot/import_cookies.py caminho/para/cookies_exportados.json
     ou cole o export em data/auth_state.json e rode python bot/import_cookies.py (sem argumento)
     Outro site: --site <nome> (ver SITES; padrão 99freelas), ex:
     python bot/import_cookies.py --site outro caminho/para/export.json
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright

from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.sources.workana import auth as workana_auth
from bot.submitter import is_logged_in

log = get_logger("import_cookies")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
AUTH_STATE_PATH = os.path.join(BASE_DIR, "data", "auth_state.json")

TARGET_DOMAIN_SUFFIX = "99freelas.com.br"


@dataclass(frozen=True)
class CookieSite:
    """Um site cuja sessão vem de cookies exportados (o login automatizado é barrado)."""
    domain_suffix: str                  # só cookies desse domínio entram
    auth_state_path: str                # storage_state do Playwright que a fonte lê
    validate: Callable                  # (playwright, auth_state_path) → logado?


# Um site por fonte que autentica por cookies. Nova fonte: uma entrada aqui, com o arquivo
# de sessão dela em data/ (gitignored) — nunca o auth_state.json do 99Freelas.
def _validate_99freelas(playwright, auth_state_path: str) -> bool:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_context(storage_state=auth_state_path).new_page()
        page.goto(sel.PROJECTS_LIST_URL, wait_until="networkidle")
        return is_logged_in(page)
    finally:
        browser.close()


SITES = {
    "99freelas": CookieSite(TARGET_DOMAIN_SUFFIX, AUTH_STATE_PATH, _validate_99freelas),
    # Navegador próprio (headless novo + User-Agent comum) por causa do Cloudflare — ver sources/workana/auth.py.
    "workana": CookieSite(workana_auth.DOMAIN_SUFFIX, workana_auth.AUTH_STATE_PATH, workana_auth.validate_session),
}

_SAME_SITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "no_restriction": "None",
    "none": "None",
    "unspecified": "Lax",
}


def _convert_cookie(raw: dict, domain_suffix: str = TARGET_DOMAIN_SUFFIX) -> dict | None:
    domain = raw.get("domain", "")
    if domain_suffix not in domain:
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


def _is_raw_export(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return isinstance(json.load(f), list)
    except (OSError, ValueError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa cookies exportados do navegador (Cookie-Editor, JSON).")
    parser.add_argument("--site", choices=sorted(SITES), default="99freelas")
    parser.add_argument("export", nargs="?", help="arquivo exportado (sem ele, lê o export colado no arquivo de sessão)")
    args = parser.parse_args()
    site = SITES[args.site]

    if args.export:
        source_path = args.export
    elif _is_raw_export(site.auth_state_path):
        # Export do Cookie-Editor colado direto no arquivo de sessão (o jeito que o
        # usuário faz na prática): converte ele mesmo.
        source_path = site.auth_state_path
    else:
        log.error(
            "Uso: python bot/import_cookies.py [--site %s] caminho/para/cookies_exportados.json "
            "(ou cole o export em %s e rode sem argumento)",
            args.site, site.auth_state_path,
        )
        sys.exit(1)
    if not os.path.exists(source_path):
        log.error("Arquivo não encontrado: %s", source_path)
        sys.exit(1)

    with open(source_path, "r", encoding="utf-8") as f:
        raw_cookies = json.load(f)

    if not isinstance(raw_cookies, list):
        log.error("Formato inesperado — esperava uma lista de cookies (export JSON do Cookie-Editor).")
        sys.exit(1)

    cookies = [c for c in (_convert_cookie(rc, site.domain_suffix) for rc in raw_cookies) if c is not None]
    if not cookies:
        log.error(
            "Nenhum cookie de %s encontrado no arquivo. Confirme que você exportou "
            "estando na aba do %s (e não de outro site).",
            site.domain_suffix, site.domain_suffix,
        )
        sys.exit(1)

    log.info("%d cookie(s) de %s encontrados, convertendo...", len(cookies), site.domain_suffix)

    storage_state = {"cookies": cookies, "origins": []}
    os.makedirs(os.path.dirname(site.auth_state_path), exist_ok=True)
    with open(site.auth_state_path, "w", encoding="utf-8") as f:
        json.dump(storage_state, f, ensure_ascii=False, indent=2)
    log.info("Sessão salva em %s", site.auth_state_path)

    log.info("Validando a sessão importada...")
    with sync_playwright() as p:
        if site.validate(p, site.auth_state_path):
            log.info("Sessão válida — login confirmado via cookies importados.")
        else:
            log.warning(
                "Não consegui confirmar o login com o marcador de login atual "
                "(pode ser que esse seletor ainda não esteja validado contra o site real, "
                "não necessariamente que os cookies estejam errados)."
            )


if __name__ == "__main__":
    main()
