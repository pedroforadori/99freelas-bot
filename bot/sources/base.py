"""
Contrato de uma fonte de vagas (99Freelas, GitHub, ...). O núcleo do bot — loop de
main.py, portão de aprovação (approvals.py) e roteamento do Telegram
(telegram_dispatcher.py) — só conversa com fontes por esta interface e nunca pergunta
"qual fonte é essa?". Adicionar uma fonte nova = subclasse de JobSource + registrar em
bot/sources/registry.py (ver CLAUDE.md, "Como adicionar uma nova fonte de vagas").

O ciclo de vida de um item é sempre o mesmo, e as partes comuns vivem aqui como template
method: a fonte acha itens (run_cycle) → monta a "proposta" e pede aprovação
(queue_for_approval) → o usuário decide no Telegram → resolve_approval entrega de verdade
(deliver) ou descarta, e registra o resultado.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

from bot import approvals, notifier, telegram_api


@dataclass(frozen=True)
class EditableField:
    """
    Campo da proposta editável por texto livre no Telegram (botão "✏️ Editar ..." →
    prompt com force_reply → resposta). O fluxo em si é genérico (telegram_dispatcher);
    a fonte só descreve o campo.
    """
    code: str                            # 1 caractere no callback_data "editf:<code>:<id>" (limite de 64 bytes)
    button: str                          # rótulo do botão na mensagem de aprovação
    prompt: str                          # pergunta mandada com force_reply
    invalid_msg: str                     # resposta quando `parse` não entende o texto
    parse: Callable[[str], Any]          # texto do usuário → valor, ou None se inválido
    apply: Callable[[dict, Any], str]    # grava o valor na proposta (dict mutável) e devolve o ack


@dataclass(frozen=True)
class Callback:
    """Clique num botão, já separado do prefixo: "link:p:123" → args=("p", "123")."""
    id: str
    args: tuple[str, ...]
    message: dict
    config: dict

    def answer(self, text: str = "") -> None:
        telegram_api.answer_callback(self.id, text)


@dataclass(frozen=True)
class CallbackRoute:
    """Handler de um prefixo de callback_data próprio da fonte, com o nº exato de argumentos."""
    arity: int
    handler: Callable[[Callback], None]


class JobSource(ABC):
    name: ClassVar[str]           # gravado em project["source"] (ex: "99freelas")
    tag: ClassVar[str]            # prefixo das mensagens no Telegram (ex: "<b>[99Freelas]</b>")
    needs_browser: ClassVar[bool] = False
    editable_fields: ClassVar[tuple[EditableField, ...]] = ()
    # Sem message_id não há botão pra aprovar: True = não enfileira (tenta no próximo ciclo).
    require_message_id: ClassVar[bool] = False
    MAX_QUEUED_PER_CYCLE: ClassVar[int] = 10  # trava de segurança contra rajada de pedidos

    # --- ciclo de vida ---------------------------------------------------------------------

    def is_enabled(self, config: dict) -> bool:
        return True

    def start(self, browser) -> bool:
        """Preparação antes do loop (ex: autenticar). False = bot não pode rodar."""
        return True

    @abstractmethod
    def run_cycle(self, config: dict) -> None:
        """Varredura (ritmo CHECK_INTERVAL_*): acha itens novos e pede aprovação. Nunca envia nada."""

    def tick(self, config: dict) -> None:
        """Trabalho do loop interno (ritmo APPROVAL_POLL_INTERVAL_SECONDS), entre varreduras."""

    def owns_id(self, project_id: str) -> bool:
        """Se o id é desta fonte — usado quando não há entrada em approvals pra olhar o "source"."""
        return False

    def notify(self, text: str) -> None:
        notifier.notify_activity(text, tag=self.tag)

    # --- pedido de aprovação -----------------------------------------------------------------

    @abstractmethod
    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        """(texto HTML, teclado inline) da mensagem de aprovação."""

    def field(self, code: str) -> EditableField | None:
        return next((f for f in self.editable_fields if f.code == code), None)

    def edit_buttons(self, project_id: str) -> list[dict]:
        """Linha de botões "✏️ Editar ..." pra por no teclado de render_approval."""
        return [{"text": f.button, "callback_data": f"editf:{f.code}:{project_id}"} for f in self.editable_fields]

    def queue_for_approval(self, project: dict, proposal: dict) -> bool:
        """Manda o pedido pro Telegram e enfileira em approvals. False = não enfileirou."""
        project["source"] = self.name
        text, keyboard = self.render_approval(project, proposal)
        message_id = notifier.send_approval_message(text, keyboard)
        if message_id is None and self.require_message_id:
            return False
        approvals.add_pending(project, proposal, message_id)
        return True

    # --- resolução da decisão (template method) ------------------------------------------

    def resolve_approval(self, entry: dict) -> None:
        """
        Decisão já gravada em approvals ("approved"/"rejected"). Aprovado → deliver();
        falha fica guardada (approvals.mark_failed) pro botão "🔄 Tentar de novo" reenviar a
        MESMA proposta, sem pedir aprovação de novo.
        """
        project_id, message_id = entry["project_id"], entry.get("telegram_message_id")

        if entry["decision"] == "rejected":
            self.on_rejected(entry)
            notifier.finalize_approval_message(message_id, approved=False, detail="rejeitada por você via Telegram")
            approvals.resolve(project_id)
            return

        if self.already_delivered(entry):
            approvals.resolve(project_id)
            return

        success, detail = self.deliver(entry)
        notifier.finalize_approval_message(message_id, approved=success, detail="" if success else detail)
        self.on_delivered(entry, success, detail)
        if success:
            approvals.resolve(project_id)
        else:
            approvals.mark_failed(project_id)

    def already_delivered(self, entry: dict) -> bool:
        """Proteção contra envio duplicado se o processo caiu entre o envio e o resolve."""
        return False

    @abstractmethod
    def deliver(self, entry: dict) -> tuple[bool, str]:
        """Envia de verdade a proposta aprovada. Retorna (sucesso, detalhe)."""

    @abstractmethod
    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        """Registra/notifica o resultado de deliver()."""

    @abstractmethod
    def on_rejected(self, entry: dict) -> None:
        """Registra a rejeição do usuário."""

    # --- extensões do Telegram (opcionais) ----------------------------------------------------

    def callback_routes(self) -> dict[str, CallbackRoute]:
        """Prefixos de callback_data próprios da fonte (os genéricos ficam no dispatcher)."""
        return {}

    def handle_message(self, message: dict) -> bool:
        """Mensagem solta no chat (não é reply). True = tratou (as outras fontes não veem)."""
        return False

    def retry_preparation(self, cb: Callback) -> tuple[str, str] | None:
        """
        "🔄 Tentar de novo" sem envio aprovado pra repetir (falha antes de existir proposta).
        Retorna (ack, rótulo do botão) se enfileirou um novo preparo, ou None se já
        respondeu o clique sozinha.
        """
        cb.answer("Nada pra tentar de novo — não há envio pendente desse item.")
        return None
