"""
Captura das imagens (e do contexto textual) de um item de portfólio, a partir de:
- um site: screenshots via Playwright em viewport desktop (3) e mobile emulado (2);
- um app: screenshots oficiais das lojas (App Store via API pública do iTunes; Play Store
  via google-play-scraper, que lê a página da loja — não há API oficial).

Limite do 99Freelas: 5 imagens por trabalho. Ordem de saída pensada pra capa: a PRIMEIRA
imagem é a que aparece como capa no perfil, então sempre é o topo (hero) da versão desktop.
Nada aqui usa a sessão logada do 99Freelas — só sites/lojas públicos.
"""
import hashlib
import os
import re

import requests

from bot.logger_setup import get_logger

log = get_logger(__name__)

MAX_IMAGES = 5
DESKTOP_SHOTS = 3
MOBILE_SHOTS = 2

# 3:2, mesma proporção do 250x167 que o 99Freelas recomenda pras imagens do portfólio.
DESKTOP_VIEWPORT = {"width": 1440, "height": 960}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Cada "variante" é um conjunto de posições verticais (fração da rolagem total da página)
# pros screenshots. "Refazer capturas" no Telegram avança pra próxima variante — mesma
# página, pontos diferentes — pra escapar de popup/seção vazia/animação no meio.
_DESKTOP_VARIANTS = [(0.0, 0.33, 0.66), (0.0, 0.45, 0.85), (0.0, 0.2, 0.55), (0.0, 0.6, 0.95)]
_MOBILE_VARIANTS = [(0.0, 0.4), (0.0, 0.7), (0.0, 0.25), (0.0, 0.9)]

# Esconde banners de cookies/chat/popups comuns antes de capturar — melhor esforço, o
# usuário revisa as imagens no Telegram de qualquer jeito.
_HIDE_OVERLAYS_CSS = """
[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i],
[id*="lgpd" i], [class*="lgpd" i], [class*="cc-window" i], #onetrust-consent-sdk,
[id*="chat" i][style*="fixed"], [class*="whatsapp" i][style*="fixed"],
iframe[src*="intercom"], iframe[src*="tawk"], iframe[src*="crisp"], iframe[src*="hubspot"]
{ display: none !important; }
"""
_CONSENT_BUTTON_TEXT = re.compile(r"^(aceitar|aceito|concordo|entendi|ok|accept|accept all|allow all)\b", re.I)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "item"


def item_id_from_key(source_key: str) -> str:
    """
    Id curto e estável do item: slug legível + hash. Tem que caber (com o prefixo
    "pf:xx:") nos 64 bytes de callback_data do Telegram — por isso ≤ 37 chars.
    """
    digest = hashlib.sha1(source_key.encode()).hexdigest()[:6]
    legivel = re.sub(r"^https?://(www\.)?", "", source_key)
    return f"{_slugify(legivel)[:30]}-{digest}"


def company_name(context: dict) -> str:
    """
    Nome da empresa/app pra nomear a pasta do item. Site: og:site_name se existir, senão o
    domínio (www.trouw.com.br → "Trouw"; o <title> costuma ter slogan junto, ruim de nome de
    pasta). App: nome na loja, sem o subtítulo depois de " - "/" – "/":".
    """
    if context.get("kind") == "app":
        name = re.split(r"\s[-–—]\s|:", context.get("title", ""))[0]
    else:
        name = (context.get("site_name") or "").strip()
        if not name:
            host = re.sub(r"^https?://", "", context.get("url", "")).split("/")[0].split(":")[0]
            labels = [l for l in host.split(".") if l and l != "www"]
            name = labels[0].capitalize() if labels else ""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    return name[:60] or "Sem nome"


def classify_url(url: str) -> str:
    if "apps.apple.com" in url or "itunes.apple.com" in url:
        return "ios"
    if "play.google.com" in url:
        return "android"
    return "site"


def _dismiss_overlays(page) -> None:
    for button in page.query_selector_all("button, a[role='button']"):
        try:
            if button.is_visible() and _CONSENT_BUTTON_TEXT.match((button.inner_text() or "").strip()):
                button.click(timeout=1000)
                break
        except Exception:
            continue
    page.add_style_tag(content=_HIDE_OVERLAYS_CSS)


def _scroll_through(page) -> int:
    """Rola a página inteira aos poucos (dispara lazy-load) e volta pro topo. Devolve a altura total."""
    height = page.evaluate("document.documentElement.scrollHeight")
    step = max(page.viewport_size["height"] // 2, 300)
    y = 0
    while y < height and y < 30000:  # trava contra scroll infinito
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(150)
        y += step
        height = page.evaluate("document.documentElement.scrollHeight")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    return height


def _shoot(browser, url: str, out_dir: str, prefix: str, positions: tuple, *, mobile: bool) -> tuple[list[str], dict]:
    if mobile:
        context = browser.new_context(
            viewport=MOBILE_VIEWPORT, device_scale_factor=2, is_mobile=True, has_touch=True, user_agent=MOBILE_USER_AGENT
        )
    else:
        context = browser.new_context(viewport=DESKTOP_VIEWPORT)
    paths: list[str] = []
    info: dict = {}
    try:
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=45000)
        page.wait_for_timeout(2000)  # deixa animações de entrada/hero terminarem
        _dismiss_overlays(page)
        if not mobile:
            info = _read_page_info(page)
        total = _scroll_through(page)
        viewport_h = page.viewport_size["height"]
        scrollable = max(total - viewport_h, 0)
        for i, frac in enumerate(positions, start=1):
            page.evaluate(f"window.scrollTo(0, {int(scrollable * frac)})")
            page.wait_for_timeout(700)
            path = os.path.join(out_dir, f"{prefix}-{i}.jpg")
            page.screenshot(path=path, type="jpeg", quality=85)
            paths.append(path)
    finally:
        context.close()
    return paths, info


def _read_page_info(page) -> dict:
    """Título, meta description e um trecho do texto visível — contexto pra IA escrever título/descrição."""
    return page.evaluate(
        """() => ({
            title: document.title || '',
            site_name: (document.querySelector('meta[property="og:site_name"]') || {}).content || '',
            meta_description: (document.querySelector('meta[name="description"], meta[property="og:description"]') || {}).content || '',
            text: (document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 3000),
        })"""
    )


def capture_site(browser, url: str, out_dir: str, variant: int = 0) -> dict:
    """
    Captura 3 screenshots desktop + 2 mobile de `url` em `out_dir`. `variant` escolhe o
    conjunto de posições de rolagem (cicla) — usado por "Refazer capturas". Devolve
    {"images": [...5 caminhos, capa primeiro], "context": {title, meta_description, text}}.
    """
    os.makedirs(out_dir, exist_ok=True)
    desktop, info = _shoot(
        browser, url, out_dir, "desktop", _DESKTOP_VARIANTS[variant % len(_DESKTOP_VARIANTS)], mobile=False
    )
    mobile, _ = _shoot(browser, url, out_dir, "mobile", _MOBILE_VARIANTS[variant % len(_MOBILE_VARIANTS)], mobile=True)
    # Capa = desktop hero; depois alterna pra o portfólio mostrar as duas versões cedo.
    images = [desktop[0], mobile[0], desktop[1], desktop[2], mobile[1]]
    return {"images": images, "context": {"kind": "site", "url": url, **info}}


def _download(url: str, path: str) -> bool:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        log.warning("Falha ao baixar %s: %s", url, e)
        return False


def _download_all(urls: list[str], out_dir: str, prefix: str, limit: int) -> list[str]:
    paths = []
    for i, url in enumerate(urls, start=1):
        if len(paths) >= limit:
            break
        path = os.path.join(out_dir, f"{prefix}-{i}.jpg")
        if _download(url, path):
            paths.append(path)
    return paths


def fetch_appstore(url: str, out_dir: str, limit: int = MAX_IMAGES) -> dict:
    """Screenshots oficiais + textos da App Store, via API pública do iTunes (sem chave)."""
    match = re.search(r"/id(\d+)", url)
    if not match:
        raise ValueError(f"não achei o id do app na URL da App Store: {url}")
    country = (re.search(r"apple\.com/([a-z]{2})/", url) or [None, "br"])[1]
    resp = requests.get(
        "https://itunes.apple.com/lookup", params={"id": match.group(1), "country": country}, timeout=20
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise ValueError(f"app não encontrado na App Store: {url}")
    app = results[0]
    os.makedirs(out_dir, exist_ok=True)
    shots = app.get("screenshotUrls") or app.get("ipadScreenshotUrls") or []
    images = _download_all(shots, out_dir, "ios", limit)
    return {
        "images": images,
        "context": {"kind": "app", "store": "App Store", "url": url, "title": app.get("trackName", ""),
                    "meta_description": app.get("description", "")[:3000]},
    }


def fetch_playstore(url: str, out_dir: str, limit: int = MAX_IMAGES) -> dict:
    """Screenshots oficiais + textos da Play Store, via google-play-scraper."""
    match = re.search(r"[?&]id=([\w.]+)", url)
    if not match:
        raise ValueError(f"não achei o id do app na URL da Play Store: {url}")
    from google_play_scraper import app as gp_app

    data = gp_app(match.group(1), lang="pt", country="br")
    os.makedirs(out_dir, exist_ok=True)
    images = _download_all(data.get("screenshots") or [], out_dir, "android", limit)
    return {
        "images": images,
        "context": {"kind": "app", "store": "Google Play", "url": url, "title": data.get("title", ""),
                    "meta_description": (data.get("description") or "")[:3000]},
    }


def capture_app(app_store_url: str | None, play_store_url: str | None, out_dir: str) -> dict:
    """App em uma ou nas duas lojas: 5 imagens de uma loja só, ou 3 iOS + 2 Android se tiver as duas."""
    if app_store_url and play_store_url:
        ios = fetch_appstore(app_store_url, out_dir, limit=3)
        android = fetch_playstore(play_store_url, out_dir, limit=MAX_IMAGES - len(ios["images"]))
        return {"images": ios["images"] + android["images"], "context": ios["context"]}
    if app_store_url:
        return fetch_appstore(app_store_url, out_dir)
    if play_store_url:
        return fetch_playstore(play_store_url, out_dir)
    raise ValueError("informe ao menos um link de loja")
