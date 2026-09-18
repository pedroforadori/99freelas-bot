import os
import random
import signal
import sys
import time

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # permite `python bot/main.py`

from bot import approvals, connections, notifier, scraper, submitter
from bot import site_selectors as sel
from bot.filter import is_match
from bot.logger_setup import get_logger
from bot.storage import already_applied, proposals_sent_today, register_application
from bot.utils import daily_quota

log = get_logger("main")

MAX_QUEUED_PER_CYCLE = 10  # trava de segurança contra rajada de pedidos de aprovação


def _handle_sigterm(signum, frame):
    # docker compose stop / restart / down mandam SIGTERM, não SIGINT — convertendo pra
    # KeyboardInterrupt reaproveita o mesmo caminho de parada "limpa" (com notificação
    # via Telegram) que já existe pro Ctrl+C, em vez do processo simplesmente morrer.
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, _handle_sigterm)

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


def run_cycle(page, config: dict, monthly_quota: int) -> None:
    """
    Varredura de projetos novos. NÃO envia proposta nenhuma diretamente — pra cada match,
    monta a proposta completa (submitter.prepare_proposal) e manda pro Telegram pra
    aprovação (approvals.add_pending + notifier.send_approval_request). O envio de
    verdade só acontece depois, em process_pending_approvals, quando o usuário aprova.
    """
    max_per_day = daily_quota(monthly_quota)
    sent_today = proposals_sent_today()
    if sent_today >= max_per_day:
        log.info("Limite diário de %d propostas atingido (%d enviadas). Aguardando o próximo dia.", max_per_day, sent_today)
        return

    # Atualiza o saldo real de conexões (lido de /dashboard) uma vez por ciclo — usado
    # por notifier.py pra montar o contador "Conexões usadas: X/Y" nas notificações.
    connections.refresh(page)

    projects = scraper.fetch_open_projects(page)
    new_projects = [p for p in projects if not already_applied(p["id"])]
    log.info("%d projetos novos (de %d na página) ainda não avaliados.", len(new_projects), len(projects))

    queued_count = 0
    for project in new_projects:
        if proposals_sent_today() >= max_per_day:
            log.info("Limite diário atingido no meio do ciclo, parando por hoje.")
            break

        match, reason = is_match(project, config)
        if not match:
            log.info("Ignorado: '%s' — %s", project["title"], reason)
            register_application(project["id"], project["title"], status="skipped_duplicate", detail=reason)
            continue

        if queued_count >= MAX_QUEUED_PER_CYCLE:
            log.info(
                "Limite de %d pedidos de aprovação por ciclo atingido, '%s' fica pro próximo ciclo.",
                MAX_QUEUED_PER_CYCLE,
                project["title"],
            )
            continue

        proposal, reason = submitter.prepare_proposal(page, project, config)
        if proposal is None:
            log.info("Não foi possível preparar proposta: '%s' — %s", project["title"], reason)
            register_application(project["id"], project["title"], status="failed", detail=reason)
            notifier.notify_proposal_result(project, None, "failed", reason)
            continue

        message_id = notifier.send_approval_request(project, proposal)
        approvals.add_pending(project, proposal, message_id)
        register_application(project["id"], project["title"], status="pending_approval", detail="aguardando aprovação no Telegram")
        log.info(
            "Aguardando aprovação: '%s' — oferta=R$%s, prazo=%sd",
            project["title"],
            proposal["oferta"],
            proposal["prazo_dias"],
        )
        queued_count += 1

        # delay curto entre preparos dentro do mesmo ciclo, pra não parecer um robô disparando em rajada
        time.sleep(random.uniform(5, 15))


def process_pending_approvals(page, config: dict, monthly_quota: int) -> None:
    """
    Resolve aprovações/rejeições já decididas no Telegram (decision != None, gravado por
    notifier.poll_decisions ANTES desta função rodar — ver bot/approvals.py). Só aqui o
    Playwright é usado de verdade pra enviar; poll_decisions em si nunca toca a Page.
    """
    max_per_day = daily_quota(monthly_quota)

    for entry in approvals.get_decided_unresolved():
        project_id = entry["project_id"]
        project = entry["project"]
        proposal = entry["proposal"]
        message_id = entry.get("telegram_message_id")

        if entry["decision"] == "rejected":
            log.info("Rejeitada pelo usuário: '%s'", project.get("title"))
            register_application(
                project_id, project["title"], status="rejected_by_user", detail="rejeitada pelo usuário via Telegram"
            )
            notifier.finalize_approval_message(message_id, approved=False, detail="rejeitada por você via Telegram")
            approvals.resolve(project_id)
            continue

        # decision == "approved" — mas a cota pode ter enchido entre a fila e agora
        # (aprovações podem ficar pendentes por tempo indefinido). Não resolve nesse
        # caso: tenta de novo no próximo ciclo/dia, mantendo o teclado como
        # "Processando..." (já trocado por notifier._handle_callback no clique).
        if proposals_sent_today() >= max_per_day:
            log.info(
                "Cota diária atingida — aprovação de '%s' fica pendente pro próximo ciclo/dia.", project.get("title")
            )
            continue

        success, detail = submitter.finalize_submission(page, project, proposal)
        status = "sent" if success else "failed"
        log.info("%s (aprovada): '%s' — %s", "ENVIADA" if success else "FALHOU", project.get("title"), detail)
        register_application(project_id, project["title"], status=status, detail=detail)
        notifier.finalize_approval_message(message_id, approved=success, detail="" if success else detail)
        approvals.resolve(project_id)


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


def main() -> None:
    load_dotenv()
    config = load_config()

    headless = os.environ.get("HEADLESS", "true").lower() == "true"
    interval_min = int(os.environ.get("CHECK_INTERVAL_MIN_SECONDS", 180))
    interval_max = int(os.environ.get("CHECK_INTERVAL_MAX_SECONDS", 420))
    monthly_quota = int(os.environ.get("MONTHLY_PROPOSAL_QUOTA", 240))
    approval_poll_interval = int(os.environ.get("APPROVAL_POLL_INTERVAL_SECONDS", 20))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        page = open_authenticated_page(browser)
        if page is None:
            browser.close()
            sys.exit(1)

        log.info("Bot iniciado. Ctrl+C para parar.")
        notifier.notify_bot_status("started")
        consecutive_errors = 0
        try:
            while True:
                try:
                    run_cycle(page, config, monthly_quota)
                    if consecutive_errors > 0:
                        log.info("Ciclo voltou ao normal após %d falha(s) consecutiva(s).", consecutive_errors)
                        notifier.notify_bot_status(
                            "cycle_recovered", f"Voltou ao normal após {consecutive_errors} ciclo(s) com erro."
                        )
                    consecutive_errors = 0
                except Exception as e:
                    # nunca deixar uma exceção de um ciclo derrubar o processo 24/7
                    consecutive_errors += 1
                    log.exception("Erro não tratado durante o ciclo (%dª consecutiva): %s", consecutive_errors, e)
                    if consecutive_errors == 1:
                        # notifica só no INÍCIO do problema, não a cada repetição — se o
                        # mesmo erro persistir por horas (ex: seletor quebrou), o bot já
                        # avisou uma vez; repetir a cada poucos minutos seria spam.
                        notifier.notify_bot_status("cycle_error", str(e))

                # Entre ciclos de varredura (scraping), fica de olho nas aprovações/
                # rejeições respondidas no Telegram numa cadência bem mais curta
                # (APPROVAL_POLL_INTERVAL_SECONDS) — sem isso, uma aprovação só seria
                # processada no próximo ciclo completo (até CHECK_INTERVAL_MAX_SECONDS,
                # ~5min), o que atrasa demais o envio de projetos sensíveis a velocidade.
                next_scrape_at = time.time() + random.uniform(interval_min, interval_max)
                log.info("Aguardando até %.0fs pro próximo ciclo (checando aprovações a cada %ds)...",
                         next_scrape_at - time.time(), approval_poll_interval)
                while time.time() < next_scrape_at:
                    try:
                        notifier.poll_decisions(config)
                        process_pending_approvals(page, config, monthly_quota)
                    except Exception as e:
                        # Erros aqui ficam só no log — categoria de falha diferente da do
                        # ciclo de scraping (polling do Telegram), não entra no contador
                        # consecutive_errors nem gera notify_bot_status próprio, pra não
                        # duplicar/confundir com os eventos de ciclo de vida já existentes.
                        log.exception("Erro ao processar aprovações pendentes: %s", e)
                    time.sleep(approval_poll_interval)
        except KeyboardInterrupt:
            log.info("Interrompido (Ctrl+C ou parada do container).")
            notifier.notify_bot_status("stopped")
        except Exception as e:
            log.exception("Erro fatal fora do ciclo, bot encerrando: %s", e)
            notifier.notify_bot_status("stopped_error", str(e))
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
