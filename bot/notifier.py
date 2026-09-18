"""
Notifica o resultado de tentativas de envio de proposta via Telegram — requer
TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env (ver README/CLAUDE.md pra como obter).

Falha sempre silenciosamente (só loga um warning): notificação nunca deve derrubar o
ciclo do bot nem impedir o registro da proposta em storage.py.
"""
import json
import os
import re

import requests

from bot import ai_writer, approvals, connections, storage
from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.proposal import ORIGEM_LABELS
from bot.utils import daily_quota, format_currency_br, parse_currency

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


def notify_new_messages(unread_count: int, previous_count: int) -> None:
    """
    Notifica quando o contador de mensagens não lidas do 99Freelas (badge do header, ver
    bot/messages.py) AUMENTA em relação ao último valor conhecido — alerta quase em tempo
    real de resposta de cliente, já que a notificação nativa do site é lenta/não confiável.
    Só chamada por messages.check_and_notify quando há aumento de verdade; uma queda ou
    igualdade nunca chega aqui, então esta função não precisa checar isso de novo.
    """
    linhas = [
        "📩 <b>Novas mensagens no 99Freelas</b>",
        f"Não lidas: {unread_count} (antes: {previous_count})",
        f'<a href="{sel.DASHBOARD_URL}">Ver no site</a>',
    ]
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
    Se o valor/prazo foi ajustado manualmente por texto livre (ver notifier._handle_edit_reply),
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
# send_approval_request manda a proposta completa com botões inline de edição (oferta/
# prazo, por texto livre — ver _handle_edit_request/_handle_edit_reply) e
# "Aprovar"/"Rejeitar". poll_decisions faz short-poll (getUpdates timeout=0 — nunca
# long-poll, bloquearia a única thread do bot) e grava a decisão em approvals.py
# IMEDIATAMENTE ao receber o clique de aprovar/rejeitar, antes de qualquer coisa no
# Playwright — isso é o que garante que uma decisão nunca se perde mesmo se o processo
# cair logo depois. finalize_approval_message troca só o teclado da mensagem original
# (nunca o texto) pra mostrar o resultado FINAL, mantendo a descrição/proposta completas
# visíveis no histórico do chat — diferente de uma edição de oferta/prazo (ver
# _handle_edit_reply), que edita o texto da mensagem (ainda não há decisão final) pra
# refletir o valor/prazo atualizado.


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
    if proposal.get("texto_ia_falhou"):
        valores += (
            "⚠️ <b>A IA falhou ao gerar o texto</b> — abaixo está o template fixo de "
            "config.yaml. Use o botão 🔄 pra tentar gerar via IA de novo, ou aprove assim mesmo.\n"
        )
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


def _approval_keyboard(project_id: str, texto_ia_falhou: bool = False) -> dict:
    """
    Teclado da mensagem de aprovação: uma linha com um botão de editar oferta e um de
    editar prazo (cada um dispara um prompt de resposta livre via force_reply — ver
    _handle_edit_request), opcionalmente uma linha com "🔄 Tentar gerar via IA novamente"
    (só quando proposal["texto_ia_falhou"] for True — ver proposal._build_texto) e a linha
    final de decisão. callback_data = "editf:<o|p>:<project_id>" pros pedidos de edição e
    "retryia:<project_id>" pro reenvio à IA, formato compacto o bastante pro limite de 64
    bytes do Telegram mesmo com o project_id.
    """
    keyboard = [
        [
            {"text": "✏️ Editar oferta", "callback_data": f"editf:o:{project_id}"},
            {"text": "✏️ Editar prazo", "callback_data": f"editf:p:{project_id}"},
        ],
    ]
    if texto_ia_falhou:
        keyboard.append([{"text": "🔄 Tentar gerar texto via IA novamente", "callback_data": f"retryia:{project_id}"}])
    keyboard.append([
        {"text": "✅ Aprovar", "callback_data": f"approve:{project_id}"},
        {"text": "❌ Rejeitar", "callback_data": f"reject:{project_id}"},
    ])
    return {"inline_keyboard": keyboard}


def send_approval_request(project: dict, proposal: dict) -> int | None:
    """
    Manda a proposta completa (descrição do projeto + texto gerado + valor + prazo) pro
    Telegram com botões de editar oferta/prazo e "Aprovar"/"Rejeitar". Retorna o
    message_id (reaproveitado tanto por edições — editMessageText, ver
    _handle_edit_reply — quanto pelo resultado final via finalize_approval_message), ou
    None se o envio falhar.
    """
    project_id = project["id"]
    result = _telegram_call(
        "sendMessage",
        {
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
            "text": _approval_text(project, proposal),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _approval_keyboard(project_id, proposal.get("texto_ia_falhou", False)),
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


_EDIT_PROMPTS = {
    "o": "Digite a nova oferta em R$ (ex: 150 ou 150,00):",
    "p": "Digite o novo prazo em dias (ex: 5):",
}


def _handle_edit_request(callback_id: str, field: str, project_id: str) -> None:
    """
    Clique em "✏️ Editar oferta/prazo": manda uma mensagem NOVA (não edita a original)
    com force_reply pedindo o novo valor em texto livre. O message_id dessa pergunta é
    gravado em approvals.set_pending_edit — quando a resposta chegar (reply_to_message
    aponta pra ela), _handle_edit_reply correlaciona de volta ao project_id/field sem
    precisar adivinhar a qual proposta pendente o texto se refere (não dá pra assumir só
    uma edição em voo por vez — pode haver várias propostas pendentes ao mesmo tempo).
    """
    entry = approvals.get_pending(project_id)
    if entry is None or entry["decision"] is not None:
        _answer_callback(callback_id, "Já decidido ou expirado — não é possível editar.")
        return

    prompt = _EDIT_PROMPTS.get(field)
    if not prompt:
        _answer_callback(callback_id)
        return

    titulo = entry["project"].get("title", "")
    result = _telegram_call(
        "sendMessage",
        {
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
            "text": f"{prompt}\n<i>{titulo}</i>",
            "parse_mode": "HTML",
            "reply_markup": {"force_reply": True, "selective": True},
        },
    )
    prompt_message_id = result.get("message_id") if isinstance(result, dict) else None
    if not prompt_message_id or not approvals.set_pending_edit(project_id, field, prompt_message_id):
        _answer_callback(callback_id, "Não foi possível iniciar a edição — tente de novo.")
        return

    _answer_callback(callback_id)


def _parse_oferta_reply(raw: str) -> float | None:
    valor = parse_currency(raw)
    if valor is None or valor < 0:
        return None
    return round(valor, 2)


def _parse_prazo_reply(raw: str) -> int | None:
    match = re.search(r"\d+", raw or "")
    if not match:
        return None
    valor = int(match.group())
    return valor if valor >= 1 else None


def _handle_edit_reply(message: dict) -> None:
    """
    Trata uma resposta de texto livre a um prompt de edição (force_reply, ver
    _handle_edit_request). Só age se reply_to_message apontar pro message_id de algum
    pending_edit ainda em aberto (approvals.find_by_prompt_message_id) — qualquer outra
    mensagem no chat (conversa normal, reply a outra coisa) é ignorada sem nenhum aviso.
    Em caso de valor inválido, avisa e mantém o pending_edit ativo pra deixar o usuário
    tentar de novo respondendo à mesma mensagem.
    """
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return
    found = approvals.find_by_prompt_message_id(reply_to["message_id"])
    if not found:
        return
    project_id, field = found

    entry = approvals.get_pending(project_id)
    if entry is None or entry["decision"] is not None:
        _send_telegram("Essa proposta já foi decidida (ou não existe mais) — edição ignorada.")
        return

    raw_text = message.get("text", "")
    proposal = dict(entry["proposal"])
    if field == "o":
        novo = _parse_oferta_reply(raw_text)
        if novo is None:
            _send_telegram("Não entendi o valor. Responda de novo à mensagem anterior com a nova oferta em R$.")
            return
        proposal["oferta"] = novo
        ack = f"Oferta atualizada: R$ {format_currency_br(novo)}"
    elif field == "p":
        novo = _parse_prazo_reply(raw_text)
        if novo is None:
            _send_telegram("Não entendi o prazo. Responda de novo à mensagem anterior com o novo prazo em dias.")
            return
        proposal["prazo_dias"] = novo
        ack = f"Prazo atualizado: {novo} dias"
    else:
        return

    proposal["ajustado_manualmente"] = True
    if not approvals.update_proposal(project_id, proposal):
        _send_telegram("Essa proposta já foi decidida (ou não existe mais) — edição ignorada.")
        return
    approvals.clear_pending_edit(project_id)

    message_id = entry.get("telegram_message_id")
    if message_id:
        _telegram_call(
            "editMessageText",
            {
                "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                "message_id": message_id,
                "text": _approval_text(entry["project"], proposal),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _approval_keyboard(project_id, proposal.get("texto_ia_falhou", False)),
            },
        )
    _send_telegram(ack)


def _handle_retry_ia_text(callback_id: str, project_id: str, config: dict) -> None:
    """
    Clique em "🔄 Tentar gerar texto via IA novamente" (só aparece quando
    proposal["texto_ia_falhou"] é True — ver proposal._build_texto/_approval_keyboard).
    Chama ai_writer.generate_proposal_text de novo com a MESMA full_description já salva
    na proposta pendente (lida da página do projeto no momento do preparo — não navega de
    novo no Playwright, só chama a IA), e, em caso de sucesso, substitui o texto e edita a
    mensagem de aprovação pra refletir o novo texto e sumir com o botão de retry. Em caso
    de falha de novo, só avisa por cima do próprio clique (toast do Telegram via
    answerCallbackQuery) — a mensagem de aprovação continua igual, ainda com o botão pra
    tentar de novo depois.
    """
    entry = approvals.get_pending(project_id)
    if entry is None or entry["decision"] is not None:
        _answer_callback(callback_id, "Já decidido ou expirado — não é possível gerar de novo.")
        return

    proposal = dict(entry["proposal"])
    full_description = proposal.get("full_description")
    if not full_description:
        _answer_callback(callback_id, "Sem descrição completa salva desse projeto — não é possível gerar via IA.")
        return

    texto = ai_writer.generate_proposal_text(entry["project"], full_description, config)
    if not texto:
        _answer_callback(callback_id, "IA falhou de novo. Tente mais tarde ou aprove com o texto atual.")
        return

    proposal["texto"] = texto
    proposal["texto_ia_falhou"] = False
    if not approvals.update_proposal(project_id, proposal):
        _answer_callback(callback_id, "Já decidido ou expirado — não é possível gerar de novo.")
        return

    message_id = entry.get("telegram_message_id")
    if message_id:
        _telegram_call(
            "editMessageText",
            {
                "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                "message_id": message_id,
                "text": _approval_text(entry["project"], proposal),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _approval_keyboard(project_id, False),
            },
        )
    _answer_callback(callback_id, "Novo texto gerado ✅")


def _handle_callback(callback: dict, config: dict) -> None:
    callback_id = callback["id"]
    data_str = callback.get("data", "")
    message_id = callback.get("message", {}).get("message_id")

    parts = data_str.split(":")

    if parts[0] == "editf" and len(parts) == 3:
        _, field, project_id = parts
        _handle_edit_request(callback_id, field, project_id)
        return

    if parts[0] == "retryia" and len(parts) == 2:
        _, project_id = parts
        _handle_retry_ia_text(callback_id, project_id, config)
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


def poll_decisions(config: dict) -> None:
    """
    Short-poll (timeout=0 — nunca o long-poll nativo do Telegram, que bloquearia a única
    thread do bot) por cliques novos nos botões Aprovar/Rejeitar/Editar/Tentar via IA de
    novo e por respostas de texto livre a um prompt de edição (ver
    _handle_edit_request/_handle_edit_reply). Usa um offset persistido em
    data/telegram_offset.json pra nunca reprocessar o mesmo update entre reinícios.
    Chamada com frequência própria (APPROVAL_POLL_INTERVAL_SECONDS) em main.py, separada
    do ciclo de scraping — ver loop em main.main(). `config` é repassado pra
    _handle_retry_ia_text poder chamar ai_writer.generate_proposal_text de novo.
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
            _handle_callback(callback, config)
            continue
        message = update.get("message")
        if message:
            _handle_edit_reply(message)

    if max_update_id >= offset:
        _save_offset(max_update_id + 1)
