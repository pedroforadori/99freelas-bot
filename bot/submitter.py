from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from bot import notifier
from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.proposal import build_proposal
from bot.utils import format_currency_br, parse_currency

log = get_logger(__name__)


def _finish(
    project: dict, proposal: dict | None, success: bool, detail: str, simulated: bool = False
) -> tuple[bool, str]:
    notifier.notify_proposal_result(project, proposal, "sent" if success else "failed", detail, simulated=simulated)
    return success, detail


def _read_lowest_bid(page: Page) -> float | None:
    """Lê o menor valor já proposto por outro freelancer, se o campo existir na página."""
    el = page.query_selector(sel.PROPOSAL_LOWEST_BID)
    if not el:
        return None
    return parse_currency(el.inner_text())


def _read_full_description(page: Page) -> str | None:
    """Lê a descrição completa (sem truncar) da página do projeto, se o elemento existir."""
    el = page.query_selector(sel.PROJECT_PAGE_DESCRIPTION)
    if not el:
        return None
    return el.inner_text().strip()


def is_logged_in(page: Page) -> bool:
    try:
        # state="attached": só confirma presença no DOM, não exige visibilidade — o
        # primeiro elemento que bate com o seletor pode estar numa versão de menu
        # (ex: mobile) escondida via CSS, o que faria o padrão ("visible") sempre
        # dar timeout mesmo com a sessão válida (confirmado: bug real, reproduzido
        # de forma consistente).
        page.wait_for_selector(sel.LOGIN_SUCCESS_MARKER, timeout=3000, state="attached")
        return True
    except PlaywrightTimeoutError:
        return False


def login(page: Page, email: str, password: str) -> bool:
    log.info("Fazendo login...")
    page.goto(sel.LOGIN_URL, wait_until="networkidle")
    page.fill(sel.LOGIN_EMAIL_INPUT, email)
    page.fill(sel.LOGIN_PASSWORD_INPUT, password)
    page.click(sel.LOGIN_SUBMIT_BUTTON)
    page.wait_for_load_state("networkidle")

    if is_logged_in(page):
        log.info("Login OK.")
        return True

    log.error("Login falhou — confira credenciais no .env e os seletores de LOGIN_* em site_selectors.py")
    return False


def submit_proposal(page: Page, project: dict, config: dict, dry_run: bool = False) -> tuple[bool, str]:
    """
    Abre a página do projeto, preenche e envia a proposta.
    Retorna (sucesso: bool, mensagem: str).

    dry_run: se True, faz tudo igual (navega, lê dados reais, preenche os campos) MAS
    NUNCA clica no botão final de envio — usado por bot/dry_run.py pra validar o
    pipeline inteiro sem gastar conexão nem enviar proposta de verdade. As notificações
    Telegram saem marcadas como simulação nesse modo.
    """
    page.goto(project["url"], wait_until="networkidle")

    # Checagem por PRESENÇA, nunca clique: um dos marcadores é o link "Cancelar proposta",
    # que CANCELARIA a proposta já enviada se fosse clicado.
    if page.query_selector(sel.PROPOSAL_ALREADY_SENT_MARKER):
        return _finish(project, None, False, "proposta já havia sido enviada anteriormente", simulated=dry_run)

    # Lida aqui (página do projeto) porque "Enviar proposta" navega pra outra página,
    # onde esse elemento não existe mais.
    full_description = _read_full_description(page)

    try:
        # "Enviar proposta" navega pra uma página separada (/project/bid/...), não é
        # um formulário na mesma página — precisa esperar a navegação terminar.
        with page.expect_navigation(wait_until="networkidle", timeout=8000):
            page.click(sel.PROPOSAL_BUTTON, timeout=8000)
    except PlaywrightTimeoutError:
        return _finish(
            project, None, False, "botão 'Enviar proposta' não encontrado (projeto pode ter fechado)", simulated=dry_run
        )

    lowest_bid = _read_lowest_bid(page)
    proposal = build_proposal(project, config, lowest_bid=lowest_bid, full_description=full_description)

    try:
        page.fill(sel.PROPOSAL_OFERTA_INPUT, format_currency_br(proposal["oferta"]))
        page.fill(sel.PROPOSAL_PRAZO_INPUT, str(proposal["prazo_dias"]))
        page.fill(sel.PROPOSAL_DETALHES_TEXTAREA, proposal["texto"])

        if dry_run:
            log.info("[DRY RUN] Campos preenchidos, envio NÃO confirmado (simulação): '%s'", project["title"])
            return _finish(project, proposal, True, "[SIMULAÇÃO] proposta NÃO enviada de verdade", simulated=True)

        # Ao confirmar, o site redireciona de volta pra página do projeto — espera a
        # navegação terminar antes de checar o marcador de sucesso.
        with page.expect_navigation(wait_until="networkidle", timeout=8000):
            page.click(sel.PROPOSAL_SUBMIT_BUTTON)
        page.wait_for_selector(sel.PROPOSAL_SUCCESS_MARKER, timeout=8000)
        return _finish(project, proposal, True, "proposta enviada com sucesso")
    except PlaywrightTimeoutError as e:
        return _finish(
            project, proposal, False, f"timeout ao preencher/enviar formulário de proposta: {e}", simulated=dry_run
        )
    except Exception as e:
        return _finish(project, proposal, False, f"erro inesperado ao enviar proposta: {e}", simulated=dry_run)
