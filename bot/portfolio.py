"""
Captura as imagens de itens de portfólio (sites e apps), gera título/descrição e manda pro
Telegram pra você aprovar — o upload no 99Freelas é feito à mão (decisão explícita: não
automatizar o envio).

    python bot/portfolio.py https://meusite.com.br https://outro.com
    python bot/portfolio.py --app-store https://apps.apple.com/br/app/x/id123 --play-store "https://play.google.com/store/apps/details?id=com.x"
    python bot/portfolio.py --file portfolio.yaml   # lista de itens (ver abaixo)
    python bot/portfolio.py https://meusite.com.br --variant 1   # outros pontos de rolagem

Pra cada item: captura as 5 imagens (bot/portfolio_capture.py — limite do 99Freelas), gera
título (≤50) e descrição (≤400) via IA (ai_writer.generate_portfolio_text), deixa tudo em
data/portfolio/.pendentes/<Nome da empresa>/ (imagens + texto.txt) e manda pro Telegram as
imagens como DOCUMENTO (preserva o arquivo original) + título/descrição pra copiar
(bot/portfolio_review.py).

Com PORTFOLIO_TELEGRAM_BOT_TOKEN (segundo bot, ver portfolio_review.py) a mensagem tem
✅ Aprovar / 🔁 Refazer capturas / ❌ Reprovar e este comando fica esperando os cliques:
aprovar move a pasta pra data/portfolio/<Nome da empresa>/, reprovar apaga, refazer captura
de novo em outros pontos da página (só sites). Sem o token, não há botões: vai direto pra
data/portfolio/<Nome>/. Não usa a sessão do 99Freelas nem depende do bot principal.

Formato de --file (YAML): lista de itens, cada um com `url` (site), OU `app_store` e/ou
`play_store` (app); `titulo` e `descricao` opcionais (senão a IA gera).
"""
import argparse
import os
import shutil
import sys

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # permite `python bot/portfolio.py`

from bot import ai_writer, portfolio_capture, portfolio_review
from bot.logger_setup import get_logger

log = get_logger("portfolio")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "portfolio")
PENDING_DIR = os.path.join(OUTPUT_DIR, ".pendentes")  # capturado, aguardando aprovação


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalize(entry) -> dict:
    """Aceita string (URL) ou dict do YAML e devolve {"site"|"app_store"/"play_store", titulo?, descricao?}."""
    if isinstance(entry, str):
        entry = {"url": entry}
    spec = {"titulo": entry.get("titulo"), "descricao": entry.get("descricao")}
    url = entry.get("url")
    if url:
        kind = portfolio_capture.classify_url(url)
        spec["app_store" if kind == "ios" else "play_store" if kind == "android" else "site"] = url
    spec["app_store"] = entry.get("app_store") or spec.get("app_store")
    spec["play_store"] = entry.get("play_store") or spec.get("play_store")
    return spec


def _source_key(spec: dict) -> str:
    return spec.get("site") or "|".join(filter(None, [spec.get("app_store"), spec.get("play_store")]))


def _fallback_text(context: dict) -> tuple[str, str]:
    """Texto do próprio site/app, cortado nos limites do formulário — só quando a IA falha."""
    titulo = (context.get("title") or "Projeto").strip()[: ai_writer.PORTFOLIO_TITULO_MAX]
    descricao = (context.get("meta_description") or "").strip()[: ai_writer.PORTFOLIO_DESCRICAO_MAX]
    return titulo, descricao


def _capture(spec: dict, browser, variant: int, dest_dir: str) -> dict:
    if spec.get("site"):
        return portfolio_capture.capture_site(browser, spec["site"], dest_dir, variant=variant)
    return portfolio_capture.capture_app(spec.get("app_store"), spec.get("play_store"), dest_dir)


def build_item(spec: dict, browser, config: dict, variant: int) -> dict | None:
    """Captura, gera título/descrição e deixa tudo em data/portfolio/.pendentes/<Nome>/."""
    key = _source_key(spec)
    if not key:
        log.error("Item sem url/app_store/play_store: %s", spec)
        return None

    item_id = portfolio_capture.item_id_from_key(key)
    # Captura numa pasta temporária: o nome da empresa (nome da pasta) só é conhecido
    # depois de ler a página/loja.
    tmp_dir = os.path.join(PENDING_DIR, f".tmp-{item_id}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log.info("Capturando %s ...", key)
    try:
        captured = _capture(spec, browser, variant, tmp_dir)
    except Exception as e:
        log.error("Falha ao capturar %s: %s", key, e)
        return None
    if not captured["images"]:
        log.error("Nenhuma imagem capturada de %s.", key)
        return None

    context = captured["context"]
    name = portfolio_capture.company_name(context)
    pending_dir = os.path.join(PENDING_DIR, name)
    shutil.rmtree(pending_dir, ignore_errors=True)  # recaptura do mesmo item substitui a anterior
    shutil.move(tmp_dir, pending_dir)

    titulo, descricao, aviso = spec.get("titulo"), spec.get("descricao"), ""
    if not (titulo and descricao):
        generated = ai_writer.generate_portfolio_text(context, config)
        if generated:
            titulo, descricao = titulo or generated[0], descricao or generated[1]
        else:
            fb_titulo, fb_descricao = _fallback_text(context)
            titulo, descricao = titulo or fb_titulo, descricao or fb_descricao
            aviso = "⚠️ A IA falhou — texto abaixo veio do próprio site/app, revise."
    # Só avisa (o usuário copia à mão, não há o que bloquear).
    violation = ai_writer.check_portfolio_text(titulo, descricao)
    if violation:
        aviso = f"{aviso}\n⚠️ Ajuste antes de usar: {violation}.".strip()

    with open(os.path.join(pending_dir, "texto.txt"), "w", encoding="utf-8") as f:
        f.write(f"{titulo}\n\n{descricao}\n")

    return {
        "id": item_id, "spec": spec, "name": name, "label": key.replace("|", " + "), "dir": pending_dir,
        "images": [os.path.join(pending_dir, os.path.basename(p)) for p in captured["images"]],
        "titulo": titulo, "descricao": descricao, "aviso": aviso, "variant": variant, "control_id": None,
    }


def approve(item: dict) -> str:
    """Move a pasta pendente pra data/portfolio/<Nome>/ (substituindo uma aprovada antes)."""
    final_dir = os.path.join(OUTPUT_DIR, item["name"])
    shutil.rmtree(final_dir, ignore_errors=True)
    shutil.move(item["dir"], final_dir)
    return final_dir


def recapture(item: dict, browser) -> bool:
    """Refaz as imagens com o próximo conjunto de posições de rolagem (só sites); mantém o texto."""
    if not item["spec"].get("site"):
        return False
    variant = item["variant"] + 1
    tmp_dir = os.path.join(PENDING_DIR, f".tmp-{item['id']}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        captured = _capture(item["spec"], browser, variant, tmp_dir)
    except Exception as e:
        log.error("Falha ao refazer capturas de %s: %s", item["label"], e)
        return False
    for old in item["images"]:
        os.remove(old)
    images = []
    for path in captured["images"]:
        dest = os.path.join(item["dir"], os.path.basename(path))
        shutil.move(path, dest)
        images.append(dest)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    item.update(images=images, variant=variant)
    return True


def wait_for_decisions(items: list[dict], browser) -> None:
    """Fica lendo os cliques do Telegram até todos os itens serem aprovados/reprovados (Ctrl+C sai)."""
    pending = {it["id"]: it for it in items}
    offset = portfolio_review.drain()
    print(f"Aguardando {len(pending)} decisão(ões) no Telegram (Ctrl+C sai; o que ficar pendente fica em {PENDING_DIR}).")
    while pending:
        callbacks, offset = portfolio_review.poll(offset)
        for cb in callbacks:
            parts = cb.get("data", "").split(":")
            item = pending.get(parts[2]) if len(parts) == 3 and parts[0] == "pf" else None
            if item is None:
                portfolio_review.answer(cb["id"], "Já decidido ou não encontrado.")
                continue
            action, control_id = parts[1], item["control_id"]
            if action == "ok":
                portfolio_review.answer(cb["id"], "Aprovado ✅")
                final_dir = approve(item)
                portfolio_review.set_label(control_id, "✅ Aprovado")
                log.info("Aprovado: %s", final_dir)
                pending.pop(item["id"])
            elif action == "no":
                portfolio_review.answer(cb["id"], "Reprovado ❌")
                shutil.rmtree(item["dir"], ignore_errors=True)
                portfolio_review.set_label(control_id, "❌ Reprovado")
                log.info("Reprovado, pasta removida: %s", item["name"])
                pending.pop(item["id"])
            elif action == "re":
                if not item["spec"].get("site"):
                    portfolio_review.answer(cb["id"], "App usa os screenshots oficiais da loja, não dá pra refazer.")
                    continue
                portfolio_review.answer(cb["id"], "Refazendo capturas... (~40s)")
                portfolio_review.set_label(control_id, "⏳ Refazendo capturas...")
                if recapture(item, browser):
                    portfolio_review.set_label(control_id, "🔁 Capturas refeitas (mensagem nova abaixo)")
                    item["control_id"] = portfolio_review.send_review(item, with_buttons=True)
                else:
                    portfolio_review.set_label(control_id, "⚠️ Falhou ao refazer — imagens antigas mantidas")
                    item["control_id"] = portfolio_review.send_review(item, with_buttons=True)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Captura imagens de portfólio e manda pro Telegram.")
    parser.add_argument("urls", nargs="*", help="links de sites (um item por link)")
    parser.add_argument("--app-store", help="link da App Store (item de app)")
    parser.add_argument("--play-store", help="link da Play Store (item de app; junto com --app-store vira um item só)")
    parser.add_argument("--file", help="YAML com a lista de itens")
    parser.add_argument("--variant", type=int, default=0, help="outro conjunto de pontos de rolagem (0-3), só sites")
    parser.add_argument("--manual-pass", action="store_true",
                        help="abre o navegador visível e pausa pra você passar do antibot (Cloudflare) antes de cada captura")
    args = parser.parse_args()
    portfolio_capture.MANUAL_PASS = args.manual_pass

    specs = [_normalize(u) for u in args.urls]
    if args.app_store or args.play_store:
        specs.append({"app_store": args.app_store, "play_store": args.play_store})
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            specs += [_normalize(e) for e in (yaml.safe_load(f) or [])]
    if not specs:
        parser.error("informe ao menos um link, --app-store/--play-store ou --file")

    config = _load_config()
    buttons = portfolio_review.buttons_enabled()
    if not buttons:
        log.warning("PORTFOLIO_TELEGRAM_BOT_TOKEN não configurado — sem botões: as imagens vão direto pra %s.", OUTPUT_DIR)
    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.manual_pass)
        try:
            for spec in specs:
                item = build_item(spec, browser, config, args.variant)
                if item is None:
                    continue
                item["control_id"] = portfolio_review.send_review(item, with_buttons=buttons)
                if buttons:
                    items.append(item)
                else:
                    log.info("Pronto: %s", approve(item))
            if items:
                wait_for_decisions(items, browser)
        except KeyboardInterrupt:
            print("\nInterrompido — itens ainda pendentes ficam em", PENDING_DIR)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
