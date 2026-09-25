"""
Polling do Telegram (cliques em botões e respostas de texto) e roteamento pro lugar certo.

Os fluxos genéricos — aprovar/rejeitar, editar um campo por texto livre, "🔄 Tentar de
novo" — vivem aqui e servem qualquer fonte: o que muda por fonte (texto da mensagem,
campos editáveis, como preparar de novo) vem da interface JobSource. Botões exclusivos de
uma fonte são registrados por ela (JobSource.callback_routes), nunca por um if aqui.

Nunca toca o Playwright: só grava decisões/edições (approvals.py, manual_queue.py) — o
envio de verdade é feito depois, no loop principal (JobSource.resolve_approval).
"""
from bot import approvals, telegram_api
from bot.logger_setup import get_logger
from bot.sources import registry
from bot.sources.base import Callback, CallbackRoute, JobSource

log = get_logger(__name__)

_JA_DECIDIDA = "Essa proposta já foi decidida (ou não existe mais) — edição ignorada."


class TelegramDispatcher:
    def __init__(self, sources: list[JobSource], config: dict):
        self.sources = sources
        self.config = config
        self.routes: dict[str, CallbackRoute] = {
            "approve": CallbackRoute(1, lambda cb: self._on_decision(cb, approve=True)),
            "reject": CallbackRoute(1, lambda cb: self._on_decision(cb, approve=False)),
            "editf": CallbackRoute(2, self._on_edit_request),
            "retry": CallbackRoute(1, self._on_retry),
        }
        for source in sources:
            for prefix, route in source.callback_routes().items():
                if prefix in self.routes:
                    raise ValueError(f"prefixo de callback '{prefix}' duplicado (fonte {source.name})")
                self.routes[prefix] = route

    def poll(self) -> None:
        """
        Short-poll (timeout=0 — nunca o long-poll nativo do Telegram, que bloquearia a única
        thread do bot) por cliques e respostas novas. Offset persistido em
        data/telegram_offset.json pra nunca reprocessar o mesmo update entre reinícios.
        Chamado com frequência própria (APPROVAL_POLL_INTERVAL_SECONDS) em main.py.
        """
        offset = telegram_api.load_offset()
        updates = telegram_api.call("getUpdates", {"offset": offset, "timeout": 0})
        if not isinstance(updates, list):
            return

        max_update_id = offset - 1
        for update in updates:
            max_update_id = max(max_update_id, update["update_id"])
            callback = update.get("callback_query")
            if callback:
                self._handle_callback(callback)
                continue
            message = update.get("message")
            if message:
                if message.get("reply_to_message"):
                    self._on_edit_reply(message)
                else:
                    self._on_message(message)

        if max_update_id >= offset:
            telegram_api.save_offset(max_update_id + 1)

    def _on_message(self, message: dict) -> None:
        """Mensagem solta (não é reply): a primeira fonte que reconhecer, trata."""
        for source in self.sources:
            if source.handle_message(message):
                return

    def _handle_callback(self, callback: dict) -> None:
        prefix, *args = callback.get("data", "").split(":")
        cb = Callback(id=callback["id"], args=tuple(args), message=callback.get("message", {}), config=self.config)
        route = self.routes.get(prefix)
        if route is None or len(args) != route.arity:
            # Clique no rótulo estático pós-decisão (callback_data="noop") ou algo
            # inesperado — só reconhece o clique pro Telegram parar de mostrar "carregando".
            cb.answer()
            return
        route.handler(cb)

    # --- Aprovar / rejeitar ---------------------------------------------------------------

    def _on_decision(self, cb: Callback, approve: bool) -> None:
        (project_id,) = cb.args
        # Grava a decisão ANTES de qualquer outra coisa — é isso que dá segurança contra
        # crash entre o clique do usuário e o envio real (ver approvals.record_decision).
        known = approvals.record_decision(project_id, "approved" if approve else "rejected")

        # Tira os botões reais imediatamente, antes do envio de verdade (que pode levar
        # alguns segundos) — evita duplo-clique/corrida.
        message_id = cb.message.get("message_id")
        if message_id:
            telegram_api.edit_reply_markup(message_id, telegram_api.static_label_keyboard("⏳ Processando..."))

        if known:
            cb.answer("Aprovado ✅" if approve else "Rejeitado ❌")
        else:
            cb.answer("Não encontrado (já processado ou expirado)")

    # --- Edição por texto livre -----------------------------------------------------------

    def _on_edit_request(self, cb: Callback) -> None:
        """
        Clique em "✏️ Editar ...": manda uma mensagem NOVA (não edita a original) com
        force_reply pedindo o valor. O message_id dessa pergunta é gravado em
        approvals.set_pending_edit — quando a resposta chegar (reply_to_message aponta pra
        ela), _on_edit_reply correlaciona de volta ao item/campo sem adivinhar (pode haver
        vários pedidos de aprovação em aberto ao mesmo tempo).
        """
        code, project_id = cb.args
        entry = approvals.get_pending(project_id)
        if entry is None or entry["decision"] is not None:
            cb.answer("Já decidido ou expirado — não é possível editar.")
            return

        source = registry.source_of(entry["project"])
        field = source.field(code)
        if field is None:
            cb.answer()
            return

        titulo = telegram_api.esc(entry["project"].get("title", ""))
        result = telegram_api.call(
            "sendMessage",
            {
                "chat_id": telegram_api.chat_id(),
                "text": f"{source.tag} {field.prompt}\n<i>{titulo}</i>",
                "parse_mode": "HTML",
                "reply_markup": {"force_reply": True, "selective": True},
            },
        )
        prompt_message_id = result.get("message_id") if isinstance(result, dict) else None
        if not prompt_message_id or not approvals.set_pending_edit(project_id, code, prompt_message_id):
            cb.answer("Não foi possível iniciar a edição — tente de novo.")
            return
        cb.answer()

    def _on_edit_reply(self, message: dict) -> None:
        """
        Resposta de texto a um prompt de edição. Só age se reply_to_message apontar pra um
        pending_edit em aberto — qualquer outra mensagem no chat é ignorada sem aviso. Valor
        inválido: avisa e mantém o pending_edit, pro usuário responder de novo à mesma
        pergunta.
        """
        found = approvals.find_by_prompt_message_id(message["reply_to_message"]["message_id"])
        if not found:
            return
        project_id, code = found

        entry = approvals.get_pending(project_id)
        if entry is None or entry["decision"] is not None:
            telegram_api.send_message(_JA_DECIDIDA)
            return

        source = registry.source_of(entry["project"])
        field = source.field(code)
        if field is None:
            return
        valor = field.parse(message.get("text", ""))
        if valor is None:
            telegram_api.send_message(field.invalid_msg)
            return

        proposal = dict(entry["proposal"])
        ack = field.apply(proposal, valor)
        if not approvals.update_proposal(project_id, proposal):
            telegram_api.send_message(_JA_DECIDIDA)
            return
        approvals.clear_pending_edit(project_id)

        message_id = entry.get("telegram_message_id")
        if message_id:
            telegram_api.edit_text(message_id, *source.render_approval(entry["project"], proposal))
        telegram_api.send_message(f"{source.tag} {ack}")

    # --- Tentar de novo ----------------------------------------------------------------------

    def _on_retry(self, cb: Callback) -> None:
        """
        "🔄 Tentar de novo" numa notificação de falha:
        - falha no ENVIO de algo já aprovado (entrada "failed" em approvals): volta pra
          "approved" e o próximo tick reenvia a MESMA proposta (com edições) — sem pedir
          aprovação de novo;
        - senão, falha no PREPARO: a fonte decide como preparar de novo
          (JobSource.retry_preparation).
        """
        (project_id,) = cb.args
        if approvals.retry_failed(project_id):
            ack, label = "Reenviando a proposta aprovada...", "⏳ Reenviando..."
        else:
            entry = approvals.get_pending(project_id)
            if entry is not None and entry["decision"] is None:
                cb.answer("Esse projeto já está aguardando sua aprovação.")
                return
            if entry is not None and entry["decision"] == "approved":
                cb.answer("Esse projeto já está sendo enviado.")
                return
            result = registry.source_for_id(project_id).retry_preparation(cb)
            if result is None:
                return
            ack, label = result

        # Troca o botão pra evitar duplo-clique enquanto o retry roda.
        if cb.message.get("message_id"):
            telegram_api.edit_reply_markup(cb.message["message_id"], telegram_api.static_label_keyboard(label))
        cb.answer(ack)
