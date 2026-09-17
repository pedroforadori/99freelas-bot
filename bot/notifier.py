"""
Notifica o resultado de tentativas de envio de proposta via Telegram — requer
TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env (ver README/CLAUDE.md pra como obter).

Falha sempre silenciosamente (só loga um warning): notificação nunca deve derrubar o
ciclo do bot nem impedir o registro da proposta em storage.py.
"""
import json
import os

import requests

from bot import approvals, connections, storage
from bot.logger_setup import get_logger
from bot.proposal import ORIGEM_LABELS
from bot.utils import daily_quota, format_currency_br

log = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_MSG_LIMIT = 4096

_OFFSET_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "telegram_offset.json")


def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não configurados — notificação não enviada.")
        return

    try:
        resp = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Falha ao enviar notificação Telegram (%s): %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Erro ao enviar notificação Telegram: %s", e)


def _telegram_call(method: str, payload: dict) -> dict | list | None:
    """
    Chamada genérica pra qualquer método da Bot API do Telegram (sendMessage com teclado,
    editMessageReplyMarkup, answerCallbackQuery, getUpdates). Retorna o campo "result" da
    resposta (dict ou list, depende do método), ou None se falhar — nunca levanta exceção,
    mesma filosofia do resto de notifier.py.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN não configurado — chamada Telegram (%s) ignorada.", method)
        return None
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            log.warning("Chamada Telegram %s falhou: %s", method, data)
            return None
        return data.get("result")
    except Exception as e:
        log.warning("Erro na chamada Telegram %s: %s", method, e)
        return None


_STATUS_EMOJI = {
    "started": "🟢",
    "stopped": "🟡",
    "stopped_error": "🔴",
    "auth_failed": "🔴",
    "cycle_error": "⚠️",
    "cycle_recovered": "✅",
}
_STATUS_TITLE = {
    "started": "Bot online",
    "stopped": "Bot parado",
    "stopped_error": "Bot caiu com erro fatal",
    "auth_failed": "Bot offline — falha ao autenticar",
    "cycle_error": "Problema num ciclo (bot continua rodando)",
    "cycle_recovered": "Ciclo voltou ao normal",
}


def notify_bot_status(event: str, detail: str = "", simulated: bool = False) -> None:
    """
    Notifica mudanças de ESTADO do bot (ciclo de vida: online/offline/erro), diferente de
    notify_proposal_result (que notifica cada tentativa de proposta). Só dispara em
    transições de estado — rodando normalmente não gera nenhuma mensagem, de propósito,
    pra não virar ruído numa operação 24/7 sem supervisão. `event` é uma das chaves de
    _STATUS_EMOJI/_STATUS_TITLE (ver main.py pra onde cada uma é chamada).

    simulated: True quando vem de bot/dry_run.py — prefixa a mensagem pra nunca ser
    confundida com o status do bot real rodando em produção.
    """
    emoji = _STATUS_EMOJI.get(event, "ℹ️")
    titulo = _STATUS_TITLE.get(event, event)
    if simulated:
        titulo = f"[DRY RUN] {titulo}"

    linhas = [f"{emoji} <b>{titulo}</b>"]
    if detail:
        linhas.append(detail)

    _send_telegram("\n".join(linhas))


def _propostas_hoje_line(status: str, simulated: bool) -> str:
    """
    Monta a linha "Hoje: X/Y", onde Y é `utils.daily_quota()` aplicado ao
    MONTHLY_PROPOSAL_QUOTA do .env (mesmo cálculo que `main.run_cycle` usa pra decidir se
    já bateu o limite do dia) e X é `storage.proposals_sent_today()`. Diferente de
    `_conexoes_usadas_line` (saldo REAL do site, resetado na data de renovação do plano),
    esse é o ritmo diário LOCAL que o bot se impõe pra não gastar a cota toda de uma vez —
    sempre disponível (não depende do cache de connections.py), por isso não retorna None.
    """
    monthly_quota = int(os.environ.get("MONTHLY_PROPOSAL_QUOTA", 240))
    max_per_day = daily_quota(monthly_quota)
    enviadas_hoje = storage.proposals_sent_today()
    if status == "sent" and not simulated:
        enviadas_hoje += 1
    return f"<b>Hoje:</b> {enviadas_hoje}/{max_per_day}"


def _conexoes_usadas_line(status: str, simulated: bool) -> str | None:
    """
    Monta a linha "Conexões usadas: X/Y" a partir do cache de connections.py (saldo real
    lido do /dashboard, atualizado uma vez por ciclo em main.py). Retorna None se ainda
    não há cache (ex: primeiro ciclo antes do primeiro refresh, ou refresh sempre falhou).

    Y é `plano_total` SOMADO às conexões não-expiráveis (`nao_expiraveis`) — o total real
    de conexões utilizáveis é maior que o do plano quando a conta tem esse saldo extra
    (confirmado numa conta real: 240 do plano + 6 não-expiráveis = 246 "disponíveis" no
    dashboard). Usar só `plano_total` como denominador super-representaria o quanto já foi
    "usado" e o contador passaria de Y antes de esgotar o saldo de verdade.

    notify_proposal_result é chamada de dentro de submitter._finish, ANTES de main.py
    registrar essa proposta em storage.py — por isso soma +1 aqui quando o envio foi real
    e bem-sucedido, senão o contador ficaria uma unidade atrasado.
    """
    cached = connections.read_cached()
    if not cached:
        return None

    ja_enviadas_hoje = storage.proposals_sent_today()
    if status == "sent" and not simulated:
        ja_enviadas_hoje += 1
    enviadas_desde_refresh = max(ja_enviadas_hoje - cached.get("baseline_sent_today", 0), 0)
    usadas = (cached["plano_total"] - cached["plano_restantes"]) + enviadas_desde_refresh
    total = cached["plano_total"] + (cached.get("nao_expiraveis") or 0)
    return f"<b>Conexões usadas:</b> {usadas}/{total}"


def _origem_line(proposal: dict) -> str | None:
    """
    Monta a linha mostrando de onde veio o valor/prazo da proposta (IA, média de
    propostas concorrentes, orçamento do cliente ou valor fixo — ver
    proposal.ORIGEM_LABELS). Retorna None se a proposta não tiver esse campo (propostas
    pendentes montadas antes dessa mudança, já persistidas em data/pending_approvals.json).
    Se o valor/prazo foi ajustado manualmente pelos botões ➖/➕ (ver notifier._handle_adjust),
    isso é sinalizado à parte, já que a origem original deixa de refletir o valor exibido.
    """
    origem = proposal.get("origem_valor")
    if not origem:
        return None
    label = ORIGEM_LABELS.get(origem, origem)
    if proposal.get("ajustado_manualmente"):
        label += " · ajustado manualmente"
    linha = f"<b>Origem do valor/prazo:</b> {label}"

    oferta_sugerida = proposal.get("oferta_sugerida_ia")
    if oferta_sugerida is not None:
        linha += f"\n<i>(IA sugeriu R$ {format_currency_br(oferta_sugerida)} — desconto competitivo aplicado)</i>"
    return linha


def notify_proposal_result(
    project: dict, proposal: dict | None, status: str, detail: str, simulated: bool = False
) -> None:
    """
    Notifica o resultado de uma tentativa de envio de proposta (sucesso ou falha).
    `proposal` pode ser None quando a falha ocorreu antes da proposta ser montada
    (ex: já havia proposta enviada, botão "Enviar proposta" não encontrado).
    Não é chamada para projetos ignorados pelo filtro (status "skipped_duplicate" em main.py).

    simulated: True quando vem de bot/dry_run.py (dry_run=True em submit_proposal) —
    nenhuma proposta real foi enviada, só preenchida. A mensagem sai claramente marcada
    como simulação pra nunca ser confundida com um envio de verdade.
    """
    if status == "sent":
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Proposta preenchida") if simulated else ("✅", "Proposta enviada")
    else:
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Falha simulada") if simulated else ("⚠️", "Falha ao enviar proposta")

    linhas = [
        f"{emoji} <b>{titulo}</b>",
        f"<b>Projeto:</b> {project.get('title', '')}",
        f"<b>Link:</b> {project.get('url', '')}",
    ]
    if proposal:
        linhas.append(f"<b>Oferta:</b> R$ {format_currency_br(proposal['oferta'])}")
        linhas.append(f"<b>Prazo:</b> {proposal['prazo_dias']} dias")
        origem_line = _origem_line(proposal)
        if origem_line:
            linhas.append(origem_line)

    linhas.append(_propostas_hoje_line(status, simulated))
    linha_conexoes = _conexoes_usadas_line(status, simulated)
    if linha_conexoes:
        linhas.append(linha_conexoes)

    linhas.append(f"<b>Detalhe:</b> {detail}")

    _send_telegram("\n".join(linhas))


# --- Fluxo de aprovação (substitui o envio 100% automático em main.py) ---
#
# send_approval_request manda a proposta completa com botões inline de ajuste (oferta/
# prazo) e "Aprovar"/"Rejeitar". poll_decisions faz short-poll (getUpdates timeout=0 —
# nunca long-poll, bloquearia a única thread do bot) e grava a decisão em approvals.py
# IMEDIATAMENTE ao receber o clique de aprovar/rejeitar, antes de qualquer coisa no
# Playwright — isso é o que garante que uma decisão nunca se perde mesmo se o processo
# cair logo depois. finalize_approval_message troca só o teclado da mensagem original
# (nunca o texto) pra mostrar o resultado FINAL, mantendo a descrição/proposta completas
# visíveis no histórico do chat — diferente de um ajuste de oferta/prazo (ver
# _handle_adjust), que edita o texto da mensagem (ainda não há decisão final) pra
# refletir o valor/prazo atualizado.


def _oferta_step_reais() -> float:
    return float(os.environ.get("APPROVAL_OFERTA_STEP_REAIS", 10))


def _prazo_step_dias() -> int:
    return int(os.environ.get("APPROVAL_PRAZO_STEP_DIAS", 1))


def _approval_text(project: dict, proposal: dict) -> str:
    cabecalho = (
        f"🆕 <b>Nova proposta pra aprovar</b>\n"
        f"<b>Projeto:</b> {project.get('title', '')}\n"
        f"<b>Link:</b> {project.get('url', '')}\n"
    )
    valores = f"<b>Oferta:</b> R$ {format_currency_br(proposal['oferta'])}\n<b>Prazo:</b> {proposal['prazo_dias']} dias\n"
    origem_line = _origem_line(proposal)
    if origem_line:
        valores += f"{origem_line}\n"
    texto_proposta = proposal["texto"]
    descricao = proposal.get("full_description") or project.get("description") or ""

    # Orçamento generoso pro texto da proposta (é o que importa pra decidir) — a descrição
    # é truncada se precisar, nunca o texto da proposta em si.
    moldura = "\n<b>Descrição do projeto:</b>\n\n\n<b>Proposta:</b>\n"
    overhead = len(cabecalho) + len(valores) + len(moldura) + len(texto_proposta) + 50
    max_desc_chars = max(_TELEGRAM_MSG_LIMIT - overhead, 200)
    if len(descricao) > max_desc_chars:
        descricao = descricao[:max_desc_chars] + "… (veja mais no link)"

    return f"{cabecalho}{valores}\n<b>Descrição do projeto:</b>\n{descricao}\n\n<b>Proposta:</b>\n{texto_proposta}"


def _approval_keyboard(project_id: str) -> dict:
    """
    Teclado da mensagem de aprovação: uma linha de ajuste de oferta, uma de prazo (botões
    de incremento/decremento em passos fixos — APPROVAL_OFERTA_STEP_REAIS/
    APPROVAL_PRAZO_STEP_DIAS no .env — em vez de texto livre, pra não precisar parsear
    resposta de usuário nem correlacionar reply) e a linha final de decisão.
    callback_data = "adj:<o|p>:<+|->:<project_id>" pros ajustes, formato compacto o
    bastante pro limite de 64 bytes do Telegram mesmo com o project_id.
    """
    step_oferta = format_currency_br(_oferta_step_reais())
    step_prazo = _prazo_step_dias()
    return {
        "inline_keyboard": [
            [
                {"text": f"➖ R$ {step_oferta}", "callback_data": f"adj:o:-:{project_id}"},
                {"text": f"➕ R$ {step_oferta}", "callback_data": f"adj:o:+:{project_id}"},
            ],
            [
                {"text": f"➖ {step_prazo}d prazo", "callback_data": f"adj:p:-:{project_id}"},
                {"text": f"➕ {step_prazo}d prazo", "callback_data": f"adj:p:+:{project_id}"},
            ],
            [
                {"text": "✅ Aprovar", "callback_data": f"approve:{project_id}"},
                {"text": "❌ Rejeitar", "callback_data": f"reject:{project_id}"},
            ],
        ]
    }


def send_approval_request(project: dict, proposal: dict) -> int | None:
    """
    Manda a proposta completa (descrição do projeto + texto gerado + valor + prazo) pro
    Telegram com botões de ajuste de oferta/prazo e "Aprovar"/"Rejeitar". Retorna o
    message_id (reaproveitado tanto por ajustes — editMessageText, ver _handle_adjust —
    quanto pelo resultado final via finalize_approval_message), ou None se o envio falhar.
    """
    project_id = project["id"]
    result = _telegram_call(
        "sendMessage",
        {
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
            "text": _approval_text(project, proposal),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _approval_keyboard(project_id),
        },
    )
    if not isinstance(result, dict):
        return None
    return result.get("message_id")


def finalize_approval_message(message_id: int | None, approved: bool, detail: str) -> None:
    """
    Troca o teclado da mensagem de aprovação original por um rótulo estático mostrando o
    resultado final — nunca mexe no TEXTO da mensagem, então a descrição/proposta
    completas continuam visíveis no histórico do chat pra referência futura.
    """
    if not message_id:
        return
    label = "✅ Aprovada e enviada" if approved else "❌ Rejeitada"
    if detail:
        label = f"{label} — {detail}"
    _telegram_call(
        "editMessageReplyMarkup",
        {
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": [[{"text": label[:64], "callback_data": "noop"}]]},
        },
    )


def _load_offset() -> int:
    if not os.path.exists(_OFFSET_PATH):
        return 0
    with open(_OFFSET_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("offset", 0)


def _save_offset(offset: int) -> None:
    os.makedirs(os.path.dirname(_OFFSET_PATH), exist_ok=True)
    tmp_path = _OFFSET_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)
    os.replace(tmp_path, _OFFSET_PATH)


def _answer_callback(callback_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _telegram_call("answerCallbackQuery", payload)


def _handle_adjust(callback_id: str, message_id: int | None, field: str, sign: str, project_id: str) -> None:
    """
    Aplica um passo de ajuste (oferta ou prazo) na proposta AINDA pendente. Lido/gravado
    via approvals.get_pending/update_proposal, que recusa o ajuste se a decisão já tiver
    sido tomada nesse meio-tempo (aprovar/rejeitar tem prioridade — evita reabrir uma
    proposta que já está em processamento). Reflete o novo valor editando o TEXTO da
    mensagem original (diferente de finalize_approval_message, que nunca edita o texto —
    aqui ainda não há decisão final, então mostrar o valor atualizado é o ponto).
    """
    entry = approvals.get_pending(project_id)
    if entry is None or entry["decision"] is not None:
        _answer_callback(callback_id, "Já decidido ou expirado — não é possível ajustar.")
        return

    proposal = dict(entry["proposal"])
    proposal["ajustado_manualmente"] = True
    delta = 1 if sign == "+" else -1
    if field == "o":
        proposal["oferta"] = max(0.0, round(proposal["oferta"] + delta * _oferta_step_reais(), 2))
        ack = f"Oferta: R$ {format_currency_br(proposal['oferta'])}"
    elif field == "p":
        proposal["prazo_dias"] = max(1, proposal["prazo_dias"] + delta * _prazo_step_dias())
        ack = f"Prazo: {proposal['prazo_dias']} dias"
    else:
        _answer_callback(callback_id)
        return

    if not approvals.update_proposal(project_id, proposal):
        _answer_callback(callback_id, "Já decidido ou expirado — não é possível ajustar.")
        return

    if message_id:
        _telegram_call(
            "editMessageText",
            {
                "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                "message_id": message_id,
                "text": _approval_text(entry["project"], proposal),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _approval_keyboard(project_id),
            },
        )
    _answer_callback(callback_id, ack)


def _handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    data_str = callback.get("data", "")
    message_id = callback.get("message", {}).get("message_id")

    parts = data_str.split(":")

    if parts[0] == "adj" and len(parts) == 4:
        _, field, sign, project_id = parts
        _handle_adjust(callback_id, message_id, field, sign, project_id)
        return

    if len(parts) != 2 or parts[0] not in ("approve", "reject"):
        # Clique no rótulo estático pós-decisão (callback_data="noop") ou algo
        # inesperado — só reconhece o clique pro Telegram parar de mostrar "carregando".
        _answer_callback(callback_id)
        return

    action, project_id = parts

    # Grava a decisão ANTES de qualquer outra coisa — é isso que dá segurança contra
    # crash entre o clique do usuário e o envio real (ver approvals.record_decision).
    known = approvals.record_decision(project_id, "approved" if action == "approve" else "rejected")

    # Tira os botões reais imediatamente, mesmo antes de process_pending_approvals
    # processar de verdade — evita duplo-clique/corrida durante o finalize_submission,
    # que pode levar alguns segundos (navegação real no Playwright).
    if message_id:
        _telegram_call(
            "editMessageReplyMarkup",
            {
                "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[{"text": "⏳ Processando...", "callback_data": "noop"}]]},
            },
        )

    if known:
        ack = "Aprovado ✅" if action == "approve" else "Rejeitado ❌"
    else:
        ack = "Não encontrado (já processado ou expirado)"
    _answer_callback(callback_id, ack)


def poll_decisions() -> None:
    """
    Short-poll (timeout=0 — nunca o long-poll nativo do Telegram, que bloquearia a única
    thread do bot) por cliques novos nos botões Aprovar/Rejeitar. Usa um offset persistido
    em data/telegram_offset.json pra nunca reprocessar o mesmo clique entre reinícios.
    Chamada com frequência própria (APPROVAL_POLL_INTERVAL_SECONDS) em main.py, separada
    do ciclo de scraping — ver loop em main.main().
    """
    offset = _load_offset()
    updates = _telegram_call("getUpdates", {"offset": offset, "timeout": 0})
    if not isinstance(updates, list):
        return

    max_update_id = offset - 1
    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        callback = update.get("callback_query")
        if callback:
            _handle_callback(callback)

    if max_update_id >= offset:
        _save_offset(max_update_id + 1)
