import re

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from bot import notifier
from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.proposal import build_proposal
from bot.utils import format_currency_br, parse_currency

log = get_logger(__name__)

_AVG_PROPOSAL_VALUE_PATTERN = re.compile(r"Valor médio das propostas:?\s*R\$\s*([\d.,]+)", re.IGNORECASE)


def _finish(
    project: dict, proposal: dict | None, success: bool, detail: str, simulated: bool = False
) -> tuple[bool, str]:
    notifier.notify_proposal_result(project, proposal, "sent" if success else "failed", detail, simulated=simulated)
    return success, detail


def _read_lowest_bid(page: Page) -> float | None:
    """
    Lê o "Valor médio das propostas" da página de envio — usado como referência de preço
    concorrente. Não é o menor valor de verdade (isso não está disponível sem Premium),
    é uma aproximação pela média. Só existe em projetos com propostas suficientes pra
    calcular a média; em projetos recém-publicados (o alvo do filtro de idade) costuma
    estar ausente, retornando None — build_proposal cai pro fallback normal nesse caso.
    """
    el = page.query_selector(sel.PROPOSAL_LOWEST_BID)
    if not el:
        return None
    match = _AVG_PROPOSAL_VALUE_PATTERN.search(el.inner_text())
    if not match:
        return None
    return parse_currency(match.group(1))


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


def prepare_proposal(page: Page, project: dict, config: dict) -> tuple[dict | None, str]:
    """
    Abre a página do projeto, monta a proposta completa (oferta, prazo, texto) SEM
    preencher nem enviar nada. Usada tanto pelo fluxo de aprovação (main.run_cycle, que
    guarda o resultado em approvals.py pra revisão no Telegram) quanto por
    finalize_submission indiretamente via submit_proposal (ver abaixo).

    Retorna (proposal, "ok") em sucesso, ou (None, motivo) em qualquer falha — projeto já
    candidatado, sem plano Premium, botão não encontrado, ou build_proposal não conseguiu
    montar um preço (ver bot/proposal.py).
    """
    page.goto(project["url"], wait_until="networkidle")

    # Checagem por PRESENÇA, nunca clique: um dos marcadores é o link "Cancelar proposta",
    # que CANCELARIA a proposta já enviada se fosse clicado.
    if page.query_selector(sel.PROPOSAL_ALREADY_SENT_MARKER):
        return None, "proposta já havia sido enviada anteriormente"

    # Lida aqui (página do projeto) porque "Enviar proposta" navega pra outra página,
    # onde esse elemento não existe mais.
    full_description = _read_full_description(page)

    try:
        # "Enviar proposta" navega pra uma página separada (/project/bid/...), não é
        # um formulário na mesma página — precisa esperar a navegação terminar.
        with page.expect_navigation(wait_until="networkidle", timeout=8000):
            page.click(sel.PROPOSAL_BUTTON, timeout=8000)
    except PlaywrightTimeoutError:
        # Confirmado em produção (2026-09-18, projeto 785336): o marcador de Premium
        # ("Ver plano") pode estar presente MESMO quando o botão real também está —
        # não é um sinal confiável de bloqueio por si só, é um banner promocional
        # genérico (ver nota em site_selectors.py). Um timeout de clique por qualquer
        # outro motivo (rede lenta, elemento coberto, etc.) caía nesse ramo e era
        # erroneamente relatado como "requer Premium". Corrigido: só trata como
        # bloqueio de Premium quando o botão real (PROPOSAL_BUTTON) de fato NÃO existe
        # na página — checagem por presença, nunca clique, mesmo padrão de sempre.
        if page.query_selector(sel.PROPOSAL_BUTTON):
            return None, "clique em 'Enviar proposta' expirou mas o botão ainda existe na página (tentar de novo)"
        if page.query_selector(sel.PROPOSAL_PREMIUM_REQUIRED_MARKER):
            return None, "requer plano Freelancer Premium ativo pra propor nesse projeto"
        return None, "botão 'Enviar proposta' não encontrado (projeto pode ter fechado)"

    lowest_bid = _read_lowest_bid(page)
    proposal = build_proposal(project, config, lowest_bid=lowest_bid, full_description=full_description)
    if proposal is None:
        return None, "sem dado de preço concorrente/orçamento e a IA não sugeriu um valor coerente"

    # Guardado dentro do próprio proposal pra notifier.send_approval_request poder mostrar
    # a descrição completa na mensagem de aprovação, sem precisar de mais um parâmetro.
    proposal["full_description"] = full_description

    return proposal, "ok"


def finalize_submission(page: Page, project: dict, proposal: dict, dry_run: bool = False) -> tuple[bool, str]:
    """
    Envia de fato a proposta JÁ MONTADA (oferta/prazo/texto vindos de prepare_proposal,
    possivelmente há muito tempo — a aprovação no Telegram pode demorar indefinidamente).
    Por isso RE-checa já-enviado/Premium do zero, defensivamente, em vez de confiar no
    resultado antigo de prepare_proposal.

    dry_run: se True, preenche os campos mas NUNCA clica no botão final de envio — usado
    por bot/dry_run.py (via submit_proposal) pra validar o pipeline sem gastar conexão.
    """
    page.goto(project["url"], wait_until="networkidle")

    if page.query_selector(sel.PROPOSAL_ALREADY_SENT_MARKER):
        return _finish(project, None, False, "proposta já havia sido enviada anteriormente", simulated=dry_run)

    try:
        with page.expect_navigation(wait_until="networkidle", timeout=8000):
            page.click(sel.PROPOSAL_BUTTON, timeout=8000)
    except PlaywrightTimeoutError:
        # Ver comentário equivalente em prepare_proposal: o marcador de Premium pode
        # coexistir com o botão real, então só é sinal confiável de bloqueio quando o
        # botão real de fato não está mais na página.
        if page.query_selector(sel.PROPOSAL_BUTTON):
            return _finish(
                project,
                None,
                False,
                "clique em 'Enviar proposta' expirou mas o botão ainda existe na página (tentar de novo)",
                simulated=dry_run,
            )
        if page.query_selector(sel.PROPOSAL_PREMIUM_REQUIRED_MARKER):
            return _finish(
                project, None, False, "requer plano Freelancer Premium ativo pra propor nesse projeto", simulated=dry_run
            )
        return _finish(
            project,
            None,
            False,
            "botão 'Enviar proposta' não encontrado ao confirmar (projeto fechou, ou sessão "
            "pode ter expirado — rode import_cookies.py de novo se isso persistir)",
            simulated=dry_run,
        )

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
        # A navegação pós-envio pode ter completado de fato (dom/load disparados) mesmo
        # sem atingir "networkidle" a tempo (ex: script de analytics/chat mantendo
        # requisição em aberto) — nesse caso o clique já confirmou o envio no servidor e
        # declarar falha aqui seria um falso negativo (confirmado em produção: proposta
        # enviada de verdade no site, bot reportou erro). Só checagem de presença
        # (query_selector), nunca clique, mesmo padrão do marcador de Premium acima.
        if page.query_selector(sel.PROPOSAL_SUCCESS_MARKER):
            return _finish(
                project, proposal, True, "proposta enviada com sucesso (confirmado após timeout de networkidle)"
            )
        return _finish(
            project, proposal, False, f"timeout ao preencher/enviar formulário de proposta: {e}", simulated=dry_run
        )
    except Exception as e:
        return _finish(project, proposal, False, f"erro inesperado ao enviar proposta: {e}", simulated=dry_run)


def submit_proposal(page: Page, project: dict, config: dict, dry_run: bool = False) -> tuple[bool, str]:
    """
    Compõe prepare_proposal + finalize_submission num único passo — usada por
    bot/dry_run.py (dry_run=True) e como referência do fluxo completo. O bot real
    (main.py) usa prepare_proposal e finalize_submission separadamente, com o portão de
    aprovação no Telegram entre os dois (ver bot/approvals.py).

    dry_run repassado pra finalize_submission (não tratado aqui) — assim o modo de
    simulação continua exercitando a navegação/preenchimento real dos campos, só sem
    clicar no botão final, igual sempre foi.
    """
    proposal, reason = prepare_proposal(page, project, config)
    if proposal is None:
        return _finish(project, None, False, reason, simulated=dry_run)
    return finalize_submission(page, project, proposal, dry_run=dry_run)
