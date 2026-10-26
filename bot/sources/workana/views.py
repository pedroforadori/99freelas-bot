"""
Mensagens do Telegram do Workana. Tudo que vem do site é escapado (esc). Toda proposta
passa pelo portão de aprovação — decisão do usuário (2026-10-23): "✅ Enviar proposta" ou
"❌ Recusar", nunca envio automático.
"""
from bot import telegram_api
from bot.telegram_api import esc
from bot.utils import format_currency_br

TAG = "<b>[Workana]</b>"
_PREFIX = f"{TAG} "

ORIGEM_LABELS = {
    "minimo_faixa": "mínimo da faixa do cliente",
    "ia": "sugestão da IA dentro da faixa do cliente",
    "ia_sem_faixa": "sugestão da IA (cliente sem faixa de orçamento)",
    "lance_minimo": "lance mínimo do site",
}


def _reais(valor) -> str:
    return f"R$ {format_currency_br(valor)}" if valor is not None else "—"


def approval_text(project: dict, proposal: dict) -> str:
    cabecalho = (
        f"{_PREFIX}📝 <b>Proposta pra aprovar</b>\n"
        f"<b>Projeto:</b> {esc(project.get('title', ''))}\n"
        f"<b>Link:</b> {esc(project.get('url', ''))}\n"
    )
    detalhes = []
    if proposal.get("orcamento_texto"):
        detalhes.append(f"<b>Orçamento do cliente:</b> {esc(proposal['orcamento_texto'])}")
    if project.get("bids") is not None:
        detalhes.append(f"<b>Propostas até agora:</b> {project['bids']}")
    info = "\n".join(detalhes) + ("\n" if detalhes else "")

    origem = ORIGEM_LABELS.get(proposal.get("origem_valor"), "")
    if proposal.get("ajustado_manualmente"):
        origem = (origem + " · " if origem else "") + "ajustado manualmente"
    info += f"<b>Oferta (valor que o cliente vê; a comissão do Workana sai dele):</b> {_reais(proposal.get('oferta'))}"
    info += f" ({esc(origem)})\n" if origem else "\n"
    if proposal.get("lance_minimo"):
        info += f"<b>Lance mínimo do site:</b> {_reais(proposal['lance_minimo'])}\n"
        if proposal.get("oferta") is not None and proposal["oferta"] < proposal["lance_minimo"]:
            info += "⚠️ <b>Oferta abaixo do lance mínimo — o site vai recusar.</b>\n"
    info += f"<b>Prazo:</b> {proposal.get('prazo_dias')} dias\n"
    if proposal.get("texto_ia_falhou"):
        info += "⚠️ A IA falhou ao gerar o texto — abaixo está o template fixo. Confira antes de enviar.\n"

    texto = esc(proposal.get("texto", ""))
    descricao = esc(proposal.get("full_description") or project.get("description") or "")
    moldura = "\n<b>Descrição:</b>\n\n\n<b>Proposta:</b>\n"
    overhead = len(cabecalho) + len(info) + len(moldura) + len(texto) + 50
    descricao = telegram_api.truncate_escaped(descricao, max(telegram_api.MSG_LIMIT - overhead, 200))
    return f"{cabecalho}{info}\n<b>Descrição:</b>\n{descricao}\n\n<b>Proposta:</b>\n{texto}"


def approval_keyboard(project_id: str, edit_buttons: list[dict]) -> dict:
    return {
        "inline_keyboard": [
            edit_buttons,
            [
                {"text": "✅ Enviar proposta", "callback_data": f"approve:{project_id}"},
                {"text": "❌ Recusar", "callback_data": f"reject:{project_id}"},
            ],
        ]
    }


def notify_result(project: dict, proposal: dict, success: bool, detail: str) -> None:
    """Resultado do envio aprovado. Falha ganha "🔄 Tentar de novo" (reenvia a mesma proposta)."""
    titulo = "✅ Proposta enviada" if success else "⚠️ Falha ao enviar proposta"
    linhas = [
        f"{_PREFIX}{titulo}",
        f"<b>Projeto:</b> {esc(project.get('title', ''))}",
        f"<b>Link:</b> {esc(project.get('url', ''))}",
        f"<b>Oferta:</b> {_reais(proposal.get('oferta'))} · <b>Prazo:</b> {proposal.get('prazo_dias')} dias",
    ]
    if not success:
        linhas.append(f"<b>Detalhe:</b> {esc(detail)}")
    reply_markup = None if success else telegram_api.single_button_keyboard("🔄 Tentar de novo", f"retry:{project['id']}")
    telegram_api.send_message("\n".join(linhas), reply_markup)


def notify_prepare_failed(project: dict, motivo: str) -> None:
    telegram_api.send_message(
        f"{_PREFIX}🚫 <b>Não consegui preparar a proposta</b>\n"
        f"<b>Projeto:</b> {esc(project.get('title', ''))}\n"
        f"<b>Link:</b> {esc(project.get('url', ''))}\n"
        f"<b>Motivo:</b> {esc(motivo)}"
    )


def notify_session(text: str) -> None:
    telegram_api.send_message(f"{_PREFIX}{text}")
