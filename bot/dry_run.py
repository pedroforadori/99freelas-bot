"""
Modo de simulação: roda o mesmo loop do bot real (main.py) — login, scraping, filtro,
leitura da descrição completa, geração via IA, preenchimento dos campos — mas NUNCA clica
no botão final de envio. Notifica tudo via Telegram marcado como [SIMULAÇÃO]. Útil pra
validar o pipeline inteiro em tempo real enquanto o plano pago (necessário pro
PROPOSAL_LOWEST_BID e outros dados da página de proposta) ainda não está ativo.

Usa um arquivo de rastreio PRÓPRIO (data/dry_run_seen.json), separado de
data/applied_jobs.json — simular um projeto aqui NUNCA impede o bot real de
candidatar-se a ele de verdade depois.

Rode: python bot/dry_run.py
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from bot import scraper, submitter
from bot.filter import is_match
from bot.logger_setup import get_logger
from bot.main import load_config, open_authenticated_page

log = get_logger("dry_run")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SEEN_PATH = os.path.join(BASE_DIR, "data", "dry_run_seen.json")
MAX_PER_CYCLE = 10  # trava de segurança contra rajada de notificações


def _load_seen() -> set:
    if not os.path.exists(SEEN_PATH):
        return set()
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def _save_seen(seen: set) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    tmp_path = SEEN_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SEEN_PATH)


def run_cycle(page, config: dict, seen: set) -> None:
    projects = scraper.fetch_open_projects(page)
    new_projects = [p for p in projects if p["id"] not in seen]
    log.info("[DRY RUN] %d projetos novos (de %d na página).", len(new_projects), len(projects))

    simulated_count = 0
    for project in new_projects:
        seen.add(project["id"])

        match, reason = is_match(project, config)
        if not match:
            log.info("[DRY RUN] Ignorado: '%s' — %s", project["title"], reason)
            continue

        if simulated_count >= MAX_PER_CYCLE:
            log.info("[DRY RUN] Limite de %d simulações por ciclo atingido, pulando '%s'.", MAX_PER_CYCLE, project["title"])
            continue

        success, detail = submitter.submit_proposal(page, project, config, dry_run=True)
        log.info("[DRY RUN] %s: '%s' — %s", "OK" if success else "FALHOU", project["title"], detail)
        simulated_count += 1

        time.sleep(random.uniform(3, 8))

    _save_seen(seen)


def main() -> None:
    load_dotenv()
    config = load_config()
    headless = os.environ.get("HEADLESS", "true").lower() == "true"
    interval_min = int(os.environ.get("CHECK_INTERVAL_MIN_SECONDS", 180))
    interval_max = int(os.environ.get("CHECK_INTERVAL_MAX_SECONDS", 420))

    seen = _load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = open_authenticated_page(browser)
        if page is None:
            browser.close()
            sys.exit(1)

        log.info("[DRY RUN] Simulação iniciada — NENHUMA proposta real será enviada. Ctrl+C pra parar.")
        try:
            while True:
                try:
                    run_cycle(page, config, seen)
                except Exception as e:
                    log.exception("[DRY RUN] Erro não tratado durante o ciclo: %s", e)

                wait_s = random.uniform(interval_min, interval_max)
                log.info("[DRY RUN] Aguardando %.0fs até o próximo ciclo...", wait_s)
                time.sleep(wait_s)
        except KeyboardInterrupt:
            log.info("[DRY RUN] Interrompido pelo usuário.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
