"""
Fonte Workana (2026-10-23). Varre a listagem de TI e Programação, filtra com o mesmo
filter.is_match do 99Freelas (seção `workana.filtro` do config.yaml sobrescreve as chaves
globais), prepara a proposta (texto pela IA com as mesmas travas; valor pela faixa do
cliente, nunca abaixo do lance mínimo do site) e pede aprovação no Telegram. SEMPRE com
aprovação — decisão do usuário: "✅ Enviar proposta" / "❌ Recusar".

Navegador próprio (ver auth.py — Cloudflare), aberto em start() a partir do browser
compartilhado (browser.browser_type). Sessão de data/workana_auth_state.json; se cair,
espera o arquivo mudar (import_cookies.py --site workana), como o 99Freelas.

Ainda NÃO validado ao vivo: o que acontece depois do clique em "Enviar orçamento". O
sucesso é "saiu da página do formulário sem erro"; qualquer outra coisa vira falha com o
HTML salvo em data/workana_debug/. Um modal "Continuar"/"Cancelar" existe escondido na
página — se aparecer, o bot NÃO confirma sozinho.
"""
import os
import random
import time

from bot import ai_writer, proposal as proposal_mod
from bot.filter import excluded_keyword, is_match
from bot.logger_setup import get_logger
from bot.sources.base import EditableField, JobSource
from bot.sources.workana import auth, client, views
from bot.utils import format_currency_br, parse_currency

log = get_logger(__name__)

_FILTER_KEYS = ("max_project_age_minutes", "keywords_include", "keywords_exclude", "budget_min", "budget_max")
_PLATAFORMA = "Workana"


# --- Campos editáveis na aprovação -------------------------------------------------------------


def _parse_oferta(raw: str) -> float | None:
    valor = parse_currency(raw)
    return round(valor, 2) if valor and valor > 0 else None


def _parse_prazo(raw: str) -> int | None:
    digitos = "".join(c for c in (raw or "") if c.isdigit())
    return int(digitos) if digitos and int(digitos) >= 1 else None


def _apply_oferta(proposal: dict, valor: float) -> str:
    proposal["oferta"] = valor
    proposal["ajustado_manualmente"] = True
    ack = f"Oferta atualizada: R$ {format_currency_br(valor)}"
    minimo = proposal.get("lance_minimo")
    if minimo and valor < minimo:
        ack += f" ⚠️ abaixo do lance mínimo do site (R$ {format_currency_br(minimo)}) — o envio vai falhar."
    return ack


def _apply_prazo(proposal: dict, valor: int) -> str:
    proposal["prazo_dias"] = valor
    proposal["ajustado_manualmente"] = True
    return f"Prazo atualizado: {valor} dias"


OFERTA = EditableField(
    code="o",
    button="✏️ Editar oferta",
    prompt="Digite a nova oferta em R$ (o valor que o cliente vê, ex: 1500 ou 1.500,00):",
    invalid_msg="Não entendi o valor. Responda de novo à mensagem anterior com a nova oferta em R$.",
    parse=_parse_oferta,
    apply=_apply_oferta,
)
PRAZO = EditableField(
    code="p",
    button="✏️ Editar prazo",
    prompt="Digite o novo prazo em dias (ex: 10):",
    invalid_msg="Não entendi o prazo. Responda de novo à mensagem anterior com o novo prazo em dias.",
    parse=_parse_prazo,
    apply=_apply_prazo,
)


def _cfg(config: dict) -> dict:
    return config.get("workana") or {}


def filter_config(config: dict) -> dict:
    """Filtro global do 99Freelas (sem `categories`, que são nomes do 99Freelas) + workana.filtro."""
    base = {k: config.get(k) for k in _FILTER_KEYS if config.get(k) is not None}
    return {**base, **(_cfg(config).get("filtro") or {})}


def build_offer(budget: dict, lance_minimo: float | None, sugestao_ia: float | None, modo: str) -> tuple[float | None, str]:
    """
    (oferta, origem). modo "minimo_faixa" (default): piso da faixa do cliente. "ia": sugestão
    da IA presa dentro da faixa. Sem faixa: sugestão da IA. Nunca abaixo do lance mínimo do
    site (o formulário recusa). None = sem base nenhuma pra um valor.
    """
    piso, teto = budget.get("min"), budget.get("max")
    if modo == "ia" and sugestao_ia is not None and (piso or teto):
        oferta, origem = sugestao_ia, "ia"
        if piso is not None:
            oferta = max(oferta, piso)
        if teto is not None:
            oferta = min(oferta, teto)
    elif piso is not None:
        oferta, origem = piso, "minimo_faixa"
    elif sugestao_ia is not None:
        oferta, origem = (min(sugestao_ia, teto) if teto else sugestao_ia), "ia_sem_faixa"
    else:
        oferta, origem = None, ""
    if lance_minimo and (oferta is None or oferta < lance_minimo):
        oferta, origem = lance_minimo, "lance_minimo"
    return (round(oferta, 2) if oferta is not None else None), origem


class WorkanaSource(JobSource):
    name = "workana"
    tag = views.TAG
    needs_browser = True  # usa o browser compartilhado só pra abrir o PRÓPRIO (auth.launch_from)
    editable_fields = (OFERTA, PRAZO)
    require_message_id = True  # sem mensagem não há botão — não registra, tenta no próximo ciclo

    def __init__(self):
        self.page = None
        self._browser = None
        self._sem_sessao = False
        self._session_mtime = None

    # --- ciclo de vida -------------------------------------------------------------------

    def is_enabled(self, config: dict) -> bool:
        return bool(_cfg(config).get("enabled"))

    def owns_id(self, project_id: str) -> bool:
        return project_id.startswith("wk-")

    def start(self, browser) -> bool:
        self._browser = auth.launch_from(browser)
        self._open_session()
        return True  # sem sessão não derruba nada: espera cookies novos (_restore_session)

    def _open_session(self) -> bool:
        if self.page is not None:
            try:
                self.page.context.close()
            except Exception:
                pass
        state = auth.AUTH_STATE_PATH if os.path.exists(auth.AUTH_STATE_PATH) else None
        self.page = auth.new_context(self._browser, state).new_page()
        self._session_mtime = auth.session_file_mtime()
        logado = auth.open_listing(self.page) and auth.is_logged_in(self.page)
        if not logado:
            self._on_session_lost()
        return logado

    def _on_session_lost(self) -> None:
        if self._sem_sessao:
            return
        self._sem_sessao = True
        log.warning("Sessão do Workana inválida — aguardando cookies novos em %s.", auth.AUTH_STATE_PATH)
        views.notify_session(
            "🔴 <b>Sem sessão no Workana</b> (as outras fontes continuam). Exporte os cookies do "
            "workana.com do seu navegador (já logado) e rode "
            "<code>python bot/import_cookies.py --site workana caminho/do/export.json</code> — "
            "o bot percebe o arquivo novo sozinho, sem reiniciar."
        )

    def _restore_session(self) -> bool:
        mtime = auth.session_file_mtime()
        if mtime is None or mtime == self._session_mtime:
            return False
        if not self._open_session():
            return False
        self._sem_sessao = False
        log.info("Sessão do Workana restaurada.")
        views.notify_session("✅ Sessão do Workana de volta.")
        return True

    # --- varredura ---------------------------------------------------------------------------

    def run_cycle(self, config: dict) -> None:
        if self._sem_sessao and not self._restore_session():
            return
        if not auth.open_listing(self.page):
            raise RuntimeError("o Cloudflare não liberou a listagem do Workana")
        self._wait_for(client.CARD)
        if not auth.is_logged_in(self.page):
            self._on_session_lost()
            return

        filtro = filter_config(config)
        limite = int(_cfg(config).get("max_por_ciclo") or self.MAX_QUEUED_PER_CYCLE)
        novos = [p for p in client.parse_listing(self.page.content()) if client.get_record(p["id"]) is None]
        log.info("Workana: %d projetos novos na listagem.", len(novos))

        queued = 0
        for project in novos:
            if queued >= limite:
                break  # não registra — fica pro próximo ciclo
            if project["por_hora"]:
                client.register(project["id"], project["title"], "skipped", "projeto por hora")
                continue
            ok, motivo = is_match(project, filtro)
            if not ok:
                log.info("Workana ignorado: '%s' — %s", project["title"], motivo)
                client.register(project["id"], project["title"], "skipped", motivo)
                continue

            proposal, motivo = self.prepare(project, config)
            if proposal is None:
                if motivo.startswith("ignorado"):
                    client.register(project["id"], project["title"], "skipped", motivo)
                else:
                    log.info("Workana: falha ao preparar '%s' — %s", project["title"], motivo)
                    client.register(project["id"], project["title"], "failed", motivo, {"url": project["url"]})
                    views.notify_prepare_failed(project, motivo)
                continue
            if not self.queue_for_approval(project, proposal):
                log.warning("Pedido de aprovação do Workana não chegou ao Telegram: '%s'", project["title"])
                continue
            client.register(project["id"], project["title"], "pending_approval", "aguardando aprovação",
                            {"url": project["url"]})
            log.info("Workana aguardando aprovação: '%s' — R$%s em %sd", project["title"],
                     proposal["oferta"], proposal["prazo_dias"])
            queued += 1
            time.sleep(random.uniform(4, 10))  # não martelar o site (Cloudflare)

    def _wait_for(self, selector: str) -> bool:
        """Partes da página são montadas por JS ~2s depois do carregamento (ex: cards da listagem)."""
        try:
            self.page.wait_for_selector(selector, state="attached", timeout=15000)
            return True
        except Exception:
            return False

    def _goto(self, url: str, esperar: str) -> str:
        if not auth.goto(self.page, url):
            raise RuntimeError(f"o Cloudflare não liberou {url}")
        self._wait_for(esperar)  # ausente (ex: projeto fechado) → segue e o parse decide
        return self.page.content()

    def prepare(self, project: dict, config: dict) -> tuple[dict | None, str]:
        """Monta a proposta (sem preencher nada no site). (None, motivo) se não der."""
        info = client.parse_project_page(self._goto(project["url"], client.PROJECT_DESCRIPTION))
        if not info["pode_propor"]:
            return None, "projeto sem botão de proposta (fechado ou já proposto)"
        full_description = info["full_description"] or project.get("description") or ""

        kw = excluded_keyword(f"{project['title']} {full_description}", filter_config(config))
        if kw:
            return None, f"ignorado: descrição completa contém '{kw}'"

        time.sleep(random.uniform(2, 4))
        form = client.parse_bid_form(self._goto(client.BID_URL.format(slug=project["slug"]), client.BID_AMOUNT))
        if not form["tem_formulario"]:
            return None, "formulário de proposta indisponível (proposta já enviada?)"

        budget_text = info["budget_text"] or project.get("budget_text") or ""
        budget = client.parse_budget(budget_text)
        sugestao = ai_writer.suggest_price_and_deadline(
            project, full_description, config, orcamento=budget_text or None, plataforma=_PLATAFORMA
        )
        modo = _cfg(config).get("oferta", "minimo_faixa")
        oferta, origem = build_offer(budget, form["lance_minimo"], sugestao[0] if sugestao else None, modo)
        if oferta is None:
            return None, "sem orçamento do cliente e a IA não sugeriu valor"
        prazo = sugestao[1] if sugestao else int(_cfg(config).get("prazo_padrao_dias", 15))

        texto, texto_ia_falhou, variante = self._texto(project, full_description, config)
        return {
            "oferta": oferta,
            "prazo_dias": prazo,
            "texto": texto,
            "texto_ia_falhou": texto_ia_falhou,
            "texto_variante": variante,
            "origem_valor": origem,
            "lance_minimo": form["lance_minimo"],
            "orcamento_texto": budget_text,
            "full_description": full_description,
        }, ""

    def _texto(self, project: dict, full_description: str, config: dict) -> tuple[str, bool, str]:
        """Mesma IA/estilos/travas do 99Freelas (proposal.py); falhou → template fixo, sinalizado."""
        proposal_cfg = config.get("proposal") or {}
        variante = proposal_mod._sortear_variante_texto(proposal_cfg)
        texto = ai_writer.generate_proposal_text(project, full_description, config, variante=variante, plataforma=_PLATAFORMA)
        falhou = not texto
        if falhou:
            variante = "template"
            template = proposal_cfg.get("texto", "Olá! Tenho interesse em {titulo}.")
            texto = template.format(titulo=project.get("title", ""), oferta="", prazo_dias="")
        texto, _ = proposal_mod._finalizar_texto(texto, project, proposal_cfg)
        return texto, falhou, variante

    # --- aprovação ---------------------------------------------------------------------------

    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        return views.approval_text(project, proposal), views.approval_keyboard(project["id"], self.edit_buttons(project["id"]))

    def already_delivered(self, entry: dict) -> bool:
        rec = client.get_record(entry["project_id"])
        return bool(rec and rec.get("status") == "sent")

    def deliver(self, entry: dict) -> tuple[bool, str]:
        project, proposal = entry["project"], entry["proposal"]
        if self._sem_sessao:
            return False, "a sessão do Workana caiu — importe os cookies de novo e tente outra vez"
        oferta, prazo = proposal.get("oferta"), proposal.get("prazo_dias")
        if not oferta or not prazo:
            return False, "proposta sem valor/prazo"

        html = self._goto(client.BID_URL.format(slug=project["slug"]), client.BID_AMOUNT)
        if not auth.is_logged_in(self.page):
            self._on_session_lost()
            return False, "a sessão do Workana caiu — importe os cookies de novo e tente outra vez"
        form = client.parse_bid_form(html)
        if not form["tem_formulario"]:
            client.save_debug("sem-formulario", html)
            return False, "formulário de proposta não encontrado (projeto fechado ou proposta já enviada?)"
        if form["lance_minimo"] and oferta < form["lance_minimo"]:
            return False, f"oferta abaixo do lance mínimo do site (R$ {format_currency_br(form['lance_minimo'])})"

        self.page.fill(client.BID_AMOUNT, client.format_amount(oferta))
        self.page.fill(client.BID_CONTENT, proposal["texto"])
        self.page.fill(client.BID_DELIVERY, f"{prazo} dias")
        aceite = self.page.query_selector(client.BID_ACKNOWLEDGED)
        if aceite is not None and not aceite.is_checked():
            aceite.check()
        self.page.click(client.BID_SUBMIT)
        return self._resultado_do_envio()

    def _resultado_do_envio(self) -> tuple[bool, str]:
        """Sucesso = saiu da página do formulário. Erro visível, modal ou nada acontecer = falha."""
        for _ in range(20):
            time.sleep(1)
            if "/messages/bid/" not in self.page.url:
                client.save_debug("enviada", self.page.content())  # 1º envio real: conferir a página
                return True, "proposta enviada"
        html = self.page.content()
        debug = client.save_debug("falha-envio", html)
        erro = self.page.query_selector(".has-error .help-block, .alert-danger, .error-message")
        if erro is not None and erro.inner_text().strip():
            return False, f"o site recusou: {erro.inner_text().strip()[:200]}"
        modal = self.page.query_selector(".modal.in button.btn-primary, .modal.show button.btn-primary")
        if modal is not None:
            return False, "o site abriu uma confirmação que o bot não conhece — não confirmei sozinho; confira no site"
        return False, f"não consegui confirmar o envio (continuou no formulário){' — HTML em ' + debug if debug else ''}"

    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        project, proposal = entry["project"], entry["proposal"]
        log.info("%s (Workana): '%s' — %s", "ENVIADA" if success else "FALHOU", project["title"], detail)
        extra = {"url": project["url"], "oferta": proposal.get("oferta"), "prazo_dias": proposal.get("prazo_dias"),
                 "origem_valor": proposal.get("origem_valor"), "texto_variante": proposal.get("texto_variante")}
        client.register(entry["project_id"], project["title"], "sent" if success else "failed", detail, extra)
        views.notify_result(project, proposal, success, detail)

    def on_rejected(self, entry: dict) -> None:
        project = entry["project"]
        log.info("Workana recusado pelo usuário: '%s'", project["title"])
        client.register(entry["project_id"], project["title"], "rejected_by_user", "recusada via Telegram", {"url": project["url"]})
