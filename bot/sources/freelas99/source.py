"""
Fonte 99Freelas: varredura da listagem de projetos (Playwright), preparo da proposta
(submitter/proposal/IA) e envio de verdade depois da aprovação no Telegram. Também é dona
do trabalho de fundo específico do site — links colados no chat (manual_queue), badge de
mensagens não lidas (messages.py) — e dos botões exclusivos dela (texto via IA de novo,
menu de link).
"""
import os
import random
import re
import time
from datetime import datetime, timedelta

from bot import ai_writer, approvals, connections, manual_queue, messages, scraper, storage, submitter, telegram_api
from bot.filter import is_match
from bot.logger_setup import get_logger
from bot.sources.base import Callback, CallbackRoute, EditableField, JobSource
from bot.sources.freelas99 import auth, views
from bot.storage import already_applied, register_application
from bot.telegram_api import esc
from bot.utils import format_currency_br, parse_currency

log = get_logger(__name__)


# --- Campos editáveis na aprovação ------------------------------------------------------


def _parse_oferta(raw: str) -> float | None:
    valor = parse_currency(raw)
    if valor is None or valor < 0:
        return None
    return round(valor, 2)


def _parse_prazo(raw: str) -> int | None:
    match = re.search(r"\d+", raw or "")
    if not match:
        return None
    valor = int(match.group())
    return valor if valor >= 1 else None


def _apply_oferta(proposal: dict, valor: float) -> str:
    proposal["oferta"] = valor
    proposal["ajustado_manualmente"] = True
    return f"Oferta atualizada: R$ {format_currency_br(valor)}"


def _apply_prazo(proposal: dict, valor: int) -> str:
    proposal["prazo_dias"] = valor
    proposal["ajustado_manualmente"] = True
    return f"Prazo atualizado: {valor} dias"


OFERTA = EditableField(
    code="o",
    button="✏️ Editar oferta",
    prompt="Digite a nova oferta em R$ (ex: 150 ou 150,00):",
    invalid_msg="Não entendi o valor. Responda de novo à mensagem anterior com a nova oferta em R$.",
    parse=_parse_oferta,
    apply=_apply_oferta,
)
PRAZO = EditableField(
    code="p",
    button="✏️ Editar prazo",
    prompt="Digite o novo prazo em dias (ex: 5):",
    invalid_msg="Não entendi o prazo. Responda de novo à mensagem anterior com o novo prazo em dias.",
    parse=_parse_prazo,
    apply=_apply_prazo,
)


def _approval_extra(proposal: dict) -> dict:
    """Estratégia usada na proposta, gravada em applied_jobs.json pra comparar os estilos depois."""
    campos = (
        "oferta", "prazo_dias", "origem_valor", "texto_variante",
        "media_concorrentes", "media_prazo", "ajustado_manualmente",
    )
    return {c: proposal[c] for c in campos if proposal.get(c) is not None}


class Freelas99Source(JobSource):
    name = "99freelas"
    tag = views.TAG
    needs_browser = True
    editable_fields = (OFERTA, PRAZO)

    def __init__(self):
        self.page = None
        self._messages_poll_interval = 60
        self._next_messages_check_at = 0.0  # força checar mensagens já no primeiro tick

    # --- ciclo de vida -------------------------------------------------------------------

    def start(self, browser) -> bool:
        self._messages_poll_interval = int(os.environ.get("MESSAGES_POLL_INTERVAL_SECONDS", 60))
        self.page = auth.open_authenticated_page(browser)
        return self.page is not None

    def owns_id(self, project_id: str) -> bool:
        return project_id.isdigit()

    def run_cycle(self, config: dict) -> None:
        """
        Varredura de projetos novos. NÃO envia proposta nenhuma — pra cada match, monta a
        proposta completa (submitter.prepare_proposal) e pede aprovação no Telegram. O
        envio de verdade só acontece em resolve_approval, depois do clique do usuário.

        A cota diária (utils.daily_quota) NÃO bloqueia a varredura nem o envio — decisão do
        usuário: mesmo passando do limite do dia, os projetos continuam chegando no
        Telegram e ele decide se gasta conexões extras (a mensagem de aprovação mostra
        "Hoje: X/Y" com aviso quando X >= Y).
        """
        # Saldo real de conexões (lido de /dashboard) uma vez por ciclo — usado pelo
        # contador "Conexões usadas: X/Y" das notificações (views._conexoes_usadas_line).
        connections.refresh(self.page)

        aguardar_media = config.get("proposal", {}).get("aguardar_media", False)
        if aguardar_media:
            self._recheck_awaiting_average(config)

        projects = scraper.fetch_open_projects(self.page)
        new_projects = [p for p in projects if not already_applied(p["id"])]
        log.info("%d projetos novos (de %d na página) ainda não avaliados.", len(new_projects), len(projects))
        if new_projects:
            self.notify(f"🔎 Ciclo: {len(new_projects)} projeto(s) novo(s) de {len(projects)} na página")

        queued_count = 0
        for project in new_projects:
            match, reason = is_match(project, config)
            if not match:
                log.info("Ignorado: '%s' — %s", project["title"], reason)
                self.notify(f"🚫 Ignorado: {esc(project['title'])}\n{esc(reason)}")
                register_application(project["id"], project["title"], status="skipped_duplicate", detail=reason)
                continue

            if queued_count >= self.MAX_QUEUED_PER_CYCLE:
                log.info(
                    "Limite de %d pedidos de aprovação por ciclo atingido, '%s' fica pro próximo ciclo.",
                    self.MAX_QUEUED_PER_CYCLE,
                    project["title"],
                )
                continue

            proposal, reason = submitter.prepare_proposal(self.page, project, config, require_average=aguardar_media)
            if reason == submitter.AGUARDANDO_MEDIA:
                # Ainda sem média de concorrentes — espera juntar propostas antes de montar
                # (preço competitivo). _recheck_awaiting_average checa de novo a cada ciclo.
                log.info("Aguardando média de propostas: '%s'", project["title"])
                self.notify(f"⏳ Aguardando média de propostas: {esc(project['title'])}")
                register_application(
                    project["id"], project["title"], status="awaiting_average", detail=reason,
                    extra={"project": project, "aguardando_desde": datetime.utcnow().isoformat()},
                )
                continue
            if proposal is None:
                log.info("Não foi possível preparar proposta: '%s' — %s", project["title"], reason)
                register_application(project["id"], project["title"], status="failed", detail=reason)
                views.notify_proposal_result(project, None, "failed", reason)
                continue

            self._queue(project, proposal, detail="aguardando aprovação no Telegram")
            queued_count += 1

            # delay curto entre preparos dentro do mesmo ciclo, pra não parecer um robô disparando em rajada
            time.sleep(random.uniform(5, 15))

    def _queue(self, project: dict, proposal: dict, detail: str) -> None:
        self.queue_for_approval(project, proposal)
        register_application(project["id"], project["title"], status="pending_approval", detail=detail)
        log.info(
            "Aguardando aprovação: '%s' — oferta=R$%s, prazo=%sd, texto=%s",
            project["title"], proposal["oferta"], proposal["prazo_dias"], proposal.get("texto_variante"),
        )

    def _recheck_awaiting_average(self, config: dict) -> None:
        """
        Projetos aderentes que ainda não tinham a média de propostas concorrentes (status
        "awaiting_average" em applied_jobs.json, com o dict do projeto guardado junto).
        Checados de novo a cada varredura: quando a média aparece (o site mostra a partir de
        ~5 propostas), monta a proposta e pede aprovação. Trava de segurança: desiste depois
        de proposal.aguardar_media_max_horas.
        """
        max_horas = config.get("proposal", {}).get("aguardar_media_max_horas", 48)
        for project_id, rec in storage.list_by_status("awaiting_average").items():
            project = rec.get("project")
            if not project:
                continue
            desde = datetime.fromisoformat(rec.get("aguardando_desde") or rec["timestamp"])
            if datetime.utcnow() - desde > timedelta(hours=max_horas):
                log.info("Média não apareceu em %sh, desistindo: '%s'", max_horas, project["title"])
                register_application(project_id, project["title"], status="failed", detail=f"média não apareceu em {max_horas}h")
                continue

            try:
                proposal, reason = submitter.prepare_proposal(self.page, project, config, require_average=True)
            except Exception as e:
                log.exception("Erro ao checar média de '%s': %s", project["title"], e)
                continue
            if reason == submitter.AGUARDANDO_MEDIA:
                log.info("Ainda sem média: '%s'", project["title"])
                continue
            if proposal is None:
                log.info("Desistindo de '%s' enquanto aguardava a média — %s", project["title"], reason)
                self.notify(f"🚫 Parou de aguardar: {esc(project['title'])}\n{esc(reason)}")
                register_application(project_id, project["title"], status="failed", detail=reason)
                continue

            self._queue(project, proposal, detail="aguardando aprovação no Telegram")
            time.sleep(random.uniform(3, 8))

    def tick(self, config: dict) -> None:
        self.process_manual_projects(config)
        # Mensagens não lidas numa cadência própria (MESSAGES_POLL_INTERVAL_SECONDS), mais
        # espaçada que o tick — cada checagem navega até /dashboard (messages.refresh).
        if time.time() >= self._next_messages_check_at:
            messages.check_and_notify(self.page)
            self._next_messages_check_at = time.time() + self._messages_poll_interval

    def process_manual_projects(self, config: dict) -> None:
        """
        Projetos cujo link o usuário colou no chat do Telegram (enfileirados em
        manual_queue pelo botão "📝 Preparar proposta"). Mesmo caminho de um match de
        run_cycle — prepare_proposal → pedido de aprovação — mas SEM passar por is_match
        (decisão do usuário: se mandou o link, quer propor) e mesmo que o projeto já tenha
        sido ignorado/falhado/rejeitado antes. As checagens de "já enviada", projeto fechado
        e Premium continuam valendo (vivem em prepare_proposal).
        """
        for item in manual_queue.peek_all():
            project_id, url = item["id"], item["url"]

            pending = approvals.get_pending(project_id)
            if pending is not None and pending["decision"] is None:
                views.notify_manual_project_failed(
                    url, "esse projeto já está aguardando sua aprovação no Telegram", retry=False
                )
                manual_queue.remove(project_id)
                continue

            project = {
                "id": project_id,
                "title": "",  # preenchido por prepare_proposal a partir da página
                "url": url,
                "category": "",
                "budget": None,
                "description": "",
                "posted_minutes_ago": None,
            }
            try:
                proposal, reason = submitter.prepare_proposal(self.page, project, config)
            except Exception as e:
                # Tira da fila mesmo assim — senão um link problemático seria retentado a
                # cada tick pra sempre. O usuário pode colar de novo.
                log.exception("Erro ao preparar proposta (link manual) %s: %s", url, e)
                proposal, reason = None, f"erro inesperado: {e}"
            if proposal is None:
                log.info("Não foi possível preparar proposta (link manual): %s — %s", url, reason)
                # Não sobrescreve um registro anterior (ex: "sent" de uma proposta já
                # enviada, que é justamente um dos motivos de falha aqui).
                if not already_applied(project_id):
                    register_application(project_id, project["title"] or url, status="failed", detail=reason)
                views.notify_manual_project_failed(url, reason)
                manual_queue.remove(project_id)
                continue

            self._queue(project, proposal, detail="link enviado manualmente")
            manual_queue.remove(project_id)

    # --- aprovação -------------------------------------------------------------------------

    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        keyboard = views.approval_keyboard(
            project["id"], self.edit_buttons(project["id"]), proposal.get("texto_ia_falhou", False)
        )
        return views.approval_text(project, proposal), keyboard

    def deliver(self, entry: dict) -> tuple[bool, str]:
        project = entry["project"]
        self.notify(f"🚀 Enviando proposta aprovada: {esc(project.get('title'))}")
        return submitter.finalize_submission(self.page, project, entry["proposal"])

    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        project = entry["project"]
        log.info("%s (aprovada): '%s' — %s", "ENVIADA" if success else "FALHOU", project.get("title"), detail)
        register_application(
            entry["project_id"], project["title"], status="sent" if success else "failed",
            detail=detail, extra=_approval_extra(entry["proposal"]),
        )

    def on_rejected(self, entry: dict) -> None:
        project = entry["project"]
        log.info("Rejeitada pelo usuário: '%s'", project.get("title"))
        self.notify(f"❌ Rejeitada por você: {esc(project.get('title'))}")
        register_application(
            entry["project_id"], project["title"], status="rejected_by_user", detail="rejeitada pelo usuário via Telegram"
        )

    # --- Telegram: botões e mensagens próprios --------------------------------------------

    def callback_routes(self) -> dict[str, CallbackRoute]:
        return {
            "retryia": CallbackRoute(arity=1, handler=self._on_retry_ia_text),
            "link": CallbackRoute(arity=2, handler=self._on_link_action),
        }

    def _on_retry_ia_text(self, cb: Callback) -> None:
        """
        "🔄 Tentar gerar texto via IA novamente" (só aparece quando
        proposal["texto_ia_falhou"] — ver proposal._build_texto). Chama a IA de novo com a
        MESMA full_description já salva na proposta pendente (não navega no Playwright) e,
        em caso de sucesso, troca o texto e edita a mensagem de aprovação (some o botão).
        Em caso de falha de novo, só avisa no toast do clique.
        """
        (project_id,) = cb.args
        entry = approvals.get_pending(project_id)
        if entry is None or entry["decision"] is not None:
            cb.answer("Já decidido ou expirado — não é possível gerar de novo.")
            return

        proposal = dict(entry["proposal"])
        full_description = proposal.get("full_description")
        if not full_description:
            cb.answer("Sem descrição completa salva desse projeto — não é possível gerar via IA.")
            return

        # "template" marca que o texto caiu pro template fixo — não é um estilo da IA
        # (passar pra ela gerava um warning encaminhado pro Telegram); volta pro padrão.
        variante = proposal.get("texto_variante")
        if variante in (None, "template"):
            variante = "padrao"
        texto = ai_writer.generate_proposal_text(entry["project"], full_description, cb.config, variante=variante)
        if not texto:
            cb.answer("IA falhou de novo. Tente mais tarde ou aprove com o texto atual.")
            return

        proposal["texto"] = texto
        proposal["texto_ia_falhou"] = False
        proposal["texto_variante"] = variante
        if not approvals.update_proposal(project_id, proposal):
            cb.answer("Já decidido ou expirado — não é possível gerar de novo.")
            return

        message_id = entry.get("telegram_message_id")
        if message_id:
            telegram_api.edit_text(message_id, *self.render_approval(entry["project"], proposal))
        cb.answer("Novo texto gerado ✅")

    def handle_message(self, message: dict) -> bool:
        """
        Mensagem solta com link de projeto do 99Freelas: responde com um menu
        (views.send_link_menu) — "📝 Preparar proposta", "💬 Respondeu" ou "🏆 Fechou".
        Nada acontece até o clique. Só aceita mensagens do próprio TELEGRAM_CHAT_ID —
        qualquer um pode escrever pro bot, e preparar proposta gasta conexão se aprovada.
        """
        chat_id = telegram_api.chat_id()
        if not chat_id or str(message.get("chat", {}).get("id")) != str(chat_id):
            return False

        textos = [message.get("text") or message.get("caption") or ""]
        textos += [e["url"] for e in message.get("entities", []) + message.get("caption_entities", []) if e.get("url")]
        matches = {m.group(2): m.group(1) for texto in textos for m in views.PROJECT_LINK_REGEX.finditer(texto)}
        if not matches:
            return False

        for project_id, slug in matches.items():
            views.send_link_menu(project_id, views.project_url(slug))
        return True

    def _on_link_action(self, cb: Callback) -> None:
        """Clique num botão do menu de link (ver views.link_menu_keyboard)."""
        kind, project_id = cb.args
        if kind == "p":
            result = self._enqueue_from_message(cb, project_id, "Não achei o link nessa mensagem — cole o link de novo.")
            if result is None:
                return
            ack, label = "Preparando a proposta...", "⏳ Preparando proposta..."
        elif kind in ("r", "f"):
            ok, ack = self._record_outcome(project_id, "fechou" if kind == "f" else "respondeu")
            if not ok:
                cb.answer(ack)
                return
            label = ack
        else:
            cb.answer()
            return

        if cb.message.get("message_id"):
            telegram_api.edit_reply_markup(cb.message["message_id"], telegram_api.static_label_keyboard(label))
        cb.answer(ack)

    @staticmethod
    def _record_outcome(project_id: str, resultado: str) -> tuple[bool, str]:
        """
        Grava "respondeu"/"fechou" no registro do projeto em applied_jobs.json — base do
        bot/report.py pra comparar os estilos de texto. O bot não consegue saber sozinho
        qual cliente respondeu (o site só mostra o total de não lidas).
        """
        rec = storage.get_application(project_id)
        if rec is None or rec.get("status") != "sent":
            return False, "Esse projeto não consta como proposta enviada — nada marcado."
        gravado = storage.record_outcome(project_id, resultado)
        return True, "🏆 Marcado: projeto fechado" if gravado == "fechou" else "💬 Marcado: cliente respondeu"

    def _enqueue_from_message(self, cb: Callback, project_id: str, no_link_msg: str) -> bool | None:
        """
        Enfileira em manual_queue o link que está no texto da mensagem clicada
        (callback_data só comporta o id). None = já respondeu o clique com o motivo.
        """
        match = views.PROJECT_LINK_REGEX.search(cb.message.get("text", ""))
        if not match:
            cb.answer(no_link_msg)
            return None
        pending = approvals.get_pending(project_id)
        if pending is not None and pending["decision"] is None:
            cb.answer("Esse projeto já está aguardando sua aprovação.")
            return None
        if not manual_queue.add(project_id, views.project_url(match.group(1))):
            cb.answer("Esse projeto já está na fila, aguarde.")
            return None
        return True

    def retry_preparation(self, cb: Callback) -> tuple[str, str] | None:
        """
        "🔄 Tentar de novo" de uma falha no PREPARO (não chegou a existir proposta):
        enfileira o link da própria mensagem de falha em manual_queue, que prepara do zero
        e manda um novo pedido de aprovação.
        """
        (project_id,) = cb.args
        if self._enqueue_from_message(cb, project_id, "Não achei o link do projeto nessa mensagem — cole o link no chat.") is None:
            return None
        return "Preparando a proposta de novo...", "⏳ Preparando de novo..."
