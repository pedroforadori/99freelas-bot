"""
Mensagens do Telegram específicas do 99Freelas: pedido de aprovação, resultado de envio,
mensagens não lidas, menu de link colado. Só monta/manda texto — as decisões ficam em
source.py. Importado também por submitter.py (resultado de envio) e messages.py (badge),
por isso não importa source.py (evita ciclo de import).
"""
import os
import re
from datetime import datetime

from bot import connections, storage, telegram_api
from bot import site_selectors as sel
from bot.proposal import ORIGEM_LABELS, TEXTO_VARIANTE_LABELS
from bot.telegram_api import esc
from bot.utils import daily_quota, format_currency_br

TAG = "<b>[99Freelas]</b>"
_PREFIX = f"{TAG} "

# Aceita a página do projeto e a de envio (/project/bid/...), com ou sem www/query string.
PROJECT_LINK_REGEX = re.compile(r"https?://(?:www\.)?99freelas\.com\.br/project/(?:bid/)?([a-z0-9-]+-(\d+))", re.I)


def project_url(slug: str) -> str:
    return f"https://www.99freelas.com.br/project/{slug.lower()}"


def notify_new_messages(unread_count: int, previous_count: int) -> None:
    """
    Notifica quando o contador de mensagens não lidas do 99Freelas (badge do header, ver
    bot/messages.py) AUMENTA em relação ao último valor conhecido — alerta quase em tempo
    real de resposta de cliente, já que a notificação nativa do site é lenta/não confiável.
    Só chamada por messages.check_and_notify quando há aumento de verdade.
    """
    linhas = [
        f"{_PREFIX}📩 <b>Novas mensagens no 99Freelas</b>",
        f"Não lidas: {unread_count} (antes: {previous_count})",
        f'<a href="{sel.DASHBOARD_URL}">Ver no site</a>',
    ]
    telegram_api.send_message("\n".join(linhas))


_REPLY_HINT = "↩️ <i>Responda a esta mensagem pra enviar a resposta pro cliente.</i>"
_REPLY_HINT_OUTRA = "↩️ <i>Responda a esta mensagem pra mandar outra (escrita por você).</i>"


def _conversa_header(conversa: dict) -> str:
    projeto = f"\nProjeto: <i>{esc(conversa['projeto'])}</i>" if conversa.get("projeto") else ""
    return f"<b>{esc(conversa['cliente'])}</b>{projeto}"


def _conversa_link(conversa: dict) -> str:
    return f'<a href="{sel.CONVERSATION_URL.format(id=conversa["id"])}">Abrir conversa no site</a>'


def _cliente_bloco(mensagens: list[dict], max_chars: int) -> str:
    corpo = "\n\n".join(
        esc(m["texto"] or "(sem texto)") + ("\n📎 <i>tem anexo — veja no site</i>" if m.get("tem_arquivos") else "")
        for m in mensagens
    )
    return telegram_api.truncate_escaped(corpo, max_chars, suffix="… (continua no site)")


def delete_keyboard(mensagem_id) -> dict:
    return telegram_api.single_button_keyboard("🗑️ Apagar do site", f"msgdel:{mensagem_id}")


def notify_client_messages(conversa: dict, mensagens: list[dict], motivo_sem_ia: str | None = None) -> int | None:
    """
    Mensagem(ns) nova(s) de um cliente sem resposta automática (ver messages.check_and_notify).
    `motivo_sem_ia`: por que a IA não respondeu (projeto já contratado/fechado etc.), quando
    a resposta automática está ligada. Devolve o message_id — vira thread (reply = resposta).
    """
    cabecalho = f"{_PREFIX}💬 Nova mensagem de {_conversa_header(conversa)}\n\n"
    aviso = f"\n\nℹ️ <i>A IA não respondeu: {esc(motivo_sem_ia)}.</i>" if motivo_sem_ia else ""
    rodape = f"{aviso}\n\n{_conversa_link(conversa)}\n{_REPLY_HINT}"
    corpo = _cliente_bloco(mensagens, telegram_api.MSG_LIMIT - len(cabecalho) - len(rodape) - 40)
    return telegram_api.send_with_keyboard(cabecalho + corpo + rodape, None)


_PARTE_STATUS = {
    "sent": "✅ enviada às {hora}",
    "pending": "🕒 sai por volta das {hora}",
    "sending": "⏳ enviando...",
    "cancelled": "✋ cancelada — não enviada",
    "failed": "❌ recusada pelo site",
    "uncertain": "⚠️ não sei se chegou — confira no site",
    "superseded": "🔁 não enviada — o cliente escreveu de novo",
}


def _hora(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M") if epoch else "?"


def render_auto_reply(grupo: dict) -> tuple[str, dict | None]:
    """
    Resposta da IA enviada em partes (messages.py, "como uma pessoa digitando"): mostra o
    cliente, cada parte com o horário (enviada / previsto) e os botões — "✋ Cancelar o que
    falta" enquanto houver parte agendada, "🗑️ Apagar do site" quando alguma já saiu. A
    mesma mensagem é editada a cada parte enviada.
    """
    conversa, partes = grupo["conversa"], grupo["partes"]
    status = [p["status"] for p in partes]
    abertas = any(st in ("pending", "sending") for st in status)
    enviadas = any(st == "sent" for st in status)
    if abertas:
        titulo = f"🕒 A IA está respondendo {_conversa_header(conversa)}"
    elif all(st == "sent" for st in status):
        titulo = f"🤖 A IA respondeu {_conversa_header(conversa)}"
    elif any(st == "superseded" for st in status):
        titulo = f"🔁 Resposta da IA substituída (o cliente escreveu de novo) — {_conversa_header(conversa)}"
    elif any(st in ("failed", "uncertain") for st in status):
        titulo = f"⚠️ Resposta da IA interrompida — {_conversa_header(conversa)}"
    else:
        titulo = f"✋ Resposta da IA cancelada — {_conversa_header(conversa)}"

    limite_parte = max(300, 2000 // max(1, len(partes)))
    linhas_partes = []
    for parte in partes:
        hora = _hora(parte.get("sent_at") if parte["status"] == "sent" else parte.get("send_at"))
        linhas_partes.append(
            f"<i>{_PARTE_STATUS.get(parte['status'], parte['status']).format(hora=hora)}</i>\n"
            f"{telegram_api.truncate_escaped(esc(parte['texto']), limite_parte, suffix='…')}"
        )
    cabecalho = f"{_PREFIX}{titulo}\n\n💬 <b>Cliente:</b>\n"
    rodape = (
        "\n\n🤖 <b>Resposta (em partes, como alguém digitando):</b>\n"
        + "\n\n".join(linhas_partes)
        + f"\n\n{_conversa_link(conversa)}\n{_REPLY_HINT_OUTRA}"
    )
    corpo = _cliente_bloco([{"texto": t} for t in grupo["cliente"]],
                           telegram_api.MSG_LIMIT - len(cabecalho) - len(rodape) - 40)

    botoes = []
    if abertas:
        botoes.append([{"text": "✋ Cancelar o que falta", "callback_data": f"msgcancel:{grupo['id']}"}])
    if enviadas:
        botoes.append([{"text": "🗑️ Apagar do site", "callback_data": f"msgdel:{grupo['id']}"}])
    return cabecalho + corpo + rodape, ({"inline_keyboard": botoes} if botoes else None)


def notify_escalation(
    conversa: dict, mensagens: list[dict], motivo: str | None, rascunho: str, draft_id: str | None
) -> int | None:
    """
    A IA não respondeu sozinha: mensagem do cliente, motivo e sugestão (não enviada).
    motivo=None = modo aprovação (mensagens.aprovar_antes_de_enviar): a resposta passou em
    todas as travas e só espera o seu clique — sai em partes, no ritmo de digitação.
    """
    if motivo is None:
        cabecalho = f"{_PREFIX}📝 Resposta da IA pra aprovar — {_conversa_header(conversa)}\n\n💬 <b>Cliente:</b>\n"
        titulo_sugestao, aviso, botao = "🤖 <b>Resposta da IA (NÃO enviada):</b>", "", "✅ Enviar pro cliente"
        dica = "↩️ <i>Aprove pelo botão (sai em partes, como alguém digitando), ou responda a esta mensagem com o seu texto.</i>"
    else:
        cabecalho = f"{_PREFIX}✋ Precisa de você — {_conversa_header(conversa)}\n\n💬 <b>Cliente:</b>\n"
        titulo_sugestao, botao = "✍️ <b>Sugestão da IA (NÃO enviada):</b>", "✅ Enviar sugestão"
        aviso = f"\n\n⚠️ <i>A IA não respondeu sozinha: {esc(motivo)}</i>"
        dica = "↩️ <i>Envie a sugestão pelo botão, ou responda a esta mensagem com o seu texto.</i>"
    sugestao = ""
    if rascunho:
        sugestao = (
            f"\n\n{titulo_sugestao}\n"
            f"<code>{telegram_api.truncate_escaped(esc(rascunho), 1600, suffix='…')}</code>"
        )
    rodape = f"{aviso}{sugestao}\n\n{_conversa_link(conversa)}\n" + (dica if draft_id else _REPLY_HINT)
    corpo = _cliente_bloco(mensagens, telegram_api.MSG_LIMIT - len(cabecalho) - len(rodape) - 40)
    teclado = telegram_api.single_button_keyboard(botao, f"msgsend:{draft_id}") if draft_id else None
    return telegram_api.send_with_keyboard(cabecalho + corpo + rodape, teclado)


def notify_reply_result(conversa: dict, texto: str, success: bool, detail: str, mensagem_id=None) -> int | None:
    if success:
        linhas = [
            f"{_PREFIX}✅ Resposta enviada pra {_conversa_header(conversa)}",
            "",
            f"<i>{telegram_api.truncate_escaped(esc(texto), 1500, suffix='…')}</i>",
            "",
            _conversa_link(conversa),
            "↩️ <i>Responda a esta mensagem pra mandar outra.</i>",
        ]
    else:
        linhas = [
            f"{_PREFIX}❌ Resposta NÃO enviada pra {_conversa_header(conversa)}",
            f"Motivo: {esc(detail)}",
            "",
            "Seu texto:",
            f"<code>{telegram_api.truncate_escaped(esc(texto), 3000, suffix='…')}</code>",
            "",
            _conversa_link(conversa),
            "↩️ <i>Responda a esta mensagem pra tentar de novo.</i>",
        ]
    teclado = delete_keyboard(mensagem_id) if success and mensagem_id else None
    return telegram_api.send_with_keyboard("\n".join(linhas), teclado)


def notify_reply_uncertain(conversa: dict, texto: str) -> int | None:
    """O bot caiu no meio do envio de uma resposta: não reenvia sozinho (poderia duplicar)."""
    linhas = [
        f"{_PREFIX}⚠️ Não sei se a resposta pra {_conversa_header(conversa)} foi enviada",
        "O bot parou no meio do envio. Confira no site antes de mandar de novo:",
        "",
        f"<code>{telegram_api.truncate_escaped(esc(texto), 3000, suffix='…')}</code>",
        "",
        _conversa_link(conversa),
        "↩️ <i>Se não chegou, responda a esta mensagem com o texto de novo.</i>",
    ]
    return telegram_api.send_with_keyboard("\n".join(linhas), None)


def _propostas_hoje_line(status: str, simulated: bool) -> str:
    """
    Monta a linha "Hoje: X/Y", onde Y é `utils.daily_quota()` aplicado ao
    MONTHLY_PROPOSAL_QUOTA do .env e X é `storage.proposals_sent_today()`. Diferente de
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


def _limite_diario_atingido() -> bool:
    monthly_quota = int(os.environ.get("MONTHLY_PROPOSAL_QUOTA", 240))
    return storage.proposals_sent_today() >= daily_quota(monthly_quota)


def _conexoes_usadas_line(status: str, simulated: bool) -> str | None:
    """
    Monta a linha "Conexões usadas: X/Y" a partir do cache de connections.py (saldo real
    lido do /dashboard, atualizado uma vez por ciclo). Retorna None se ainda não há cache
    (ex: primeiro ciclo antes do primeiro refresh, ou refresh sempre falhou).

    Y é `plano_total` SOMADO às conexões não-expiráveis (`nao_expiraveis`) — o total real
    de conexões utilizáveis é maior que o do plano quando a conta tem esse saldo extra
    (confirmado numa conta real: 240 do plano + 6 não-expiráveis = 246 "disponíveis" no
    dashboard). Usar só `plano_total` como denominador super-representaria o quanto já foi
    "usado" e o contador passaria de Y antes de esgotar o saldo de verdade.

    notify_proposal_result é chamada de dentro de submitter._finish, ANTES de a proposta
    ser registrada em storage.py — por isso soma +1 aqui quando o envio foi real e
    bem-sucedido, senão o contador ficaria uma unidade atrasado.
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
    Se o valor/prazo foi ajustado manualmente por texto livre, isso é sinalizado à parte,
    já que a origem original deixa de refletir o valor exibido.
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
    media = proposal.get("media_concorrentes")
    if media:
        media_prazo = proposal.get("media_prazo")
        prazo_txt = f" · {media_prazo} dias" if media_prazo else ""
        linha += f"\n<i>(média das propostas concorrentes: R$ {format_currency_br(media)}{prazo_txt})</i>"
    return linha


def _texto_variante_line(proposal: dict) -> str | None:
    """Estilo do texto (ver proposal.TEXTO_VARIANTE_LABELS); None em propostas antigas."""
    variante = proposal.get("texto_variante")
    if not variante:
        return None
    return f"<b>Estilo do texto:</b> {TEXTO_VARIANTE_LABELS.get(variante, variante)}"


def notify_proposal_result(
    project: dict, proposal: dict | None, status: str, detail: str, simulated: bool = False
) -> None:
    """
    Notifica o resultado de uma tentativa de envio de proposta (sucesso ou falha).
    `proposal` pode ser None quando a falha ocorreu antes da proposta ser montada
    (ex: já havia proposta enviada, botão "Enviar proposta" não encontrado).
    Não é chamada para projetos ignorados pelo filtro.

    simulated: True quando vem de bot/dry_run.py (dry_run=True em submit_proposal) —
    nenhuma proposta real foi enviada, só preenchida. A mensagem sai claramente marcada
    como simulação pra nunca ser confundida com um envio de verdade.
    """
    if status == "sent":
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Proposta preenchida") if simulated else ("✅", "Proposta enviada")
    else:
        emoji, titulo = ("🧪", "[SIMULAÇÃO] Falha simulada") if simulated else ("⚠️", "Falha ao enviar proposta")

    linhas = [
        f"{_PREFIX}{emoji} <b>{titulo}</b>",
        f"<b>Projeto:</b> {esc(project.get('title', ''))}",
        f"<b>Link:</b> {esc(project.get('url', ''))}",
    ]
    if proposal:
        linhas.append(f"<b>Oferta:</b> R$ {format_currency_br(proposal['oferta'])}")
        linhas.append(f"<b>Prazo:</b> {proposal['prazo_dias']} dias")
        if proposal.get("promovida"):
            linhas.append("⭐ <b>Promovida</b>")
        origem_line = _origem_line(proposal)
        if origem_line:
            linhas.append(origem_line)
        variante_line = _texto_variante_line(proposal)
        if variante_line:
            linhas.append(variante_line)

    linhas.append(_propostas_hoje_line(status, simulated))
    linha_conexoes = _conexoes_usadas_line(status, simulated)
    if linha_conexoes:
        linhas.append(linha_conexoes)

    linhas.append(f"<b>Detalhe:</b> {esc(detail)}")

    # Falha real ganha botão de retry (ver telegram_dispatcher). Nunca em simulação.
    reply_markup = None
    if status != "sent" and not simulated and project.get("id"):
        reply_markup = telegram_api.single_button_keyboard("🔄 Tentar de novo", f"retry:{project['id']}")

    telegram_api.send_message("\n".join(linhas), reply_markup)


def approval_text(project: dict, proposal: dict) -> str:
    cabecalho = (
        f"{_PREFIX}🆕 <b>Nova proposta pra aprovar</b>\n"
        f"<b>Projeto:</b> {esc(project.get('title', ''))}\n"
        f"<b>Link:</b> {esc(project.get('url', ''))}\n"
    )
    valores = f"<b>Oferta:</b> R$ {format_currency_br(proposal['oferta'])}\n<b>Prazo:</b> {proposal['prazo_dias']} dias\n"
    origem_line = _origem_line(proposal)
    if origem_line:
        valores += f"{origem_line}\n"
    variante_line = _texto_variante_line(proposal)
    if variante_line:
        valores += f"{variante_line}\n"
    # A cota diária não bloqueia a fila — mostra o ritmo do dia aqui pro usuário decidir
    # se vale gastar uma conexão extra. Pode passar de Y (ex: 11/8).
    valores += f"{_propostas_hoje_line('pending', simulated=False)}\n"
    if _limite_diario_atingido():
        valores += "⚠️ <b>Limite diário já atingido</b> — aprovar gasta conexão extra além do ritmo do dia.\n"
    if proposal.get("promovida"):
        valores += "⭐ <b>Proposta promovida</b> — será enviada com destaque (só um freelancer por projeto consegue; aprove rápido).\n"
        if proposal.get("promocao_sem_media"):
            valores += (
                "<i>Sem média de concorrentes ainda — valor/prazo sugeridos pela IA. Se outro "
                "freelancer promover antes do envio, não envia e volta a aguardar a média.</i>\n"
            )
    if proposal.get("texto_ia_falhou"):
        valores += (
            "⚠️ <b>A IA falhou ao gerar o texto</b> — abaixo está o template fixo de "
            "config.yaml. Use o botão 🔄 pra tentar gerar via IA de novo, ou aprove assim mesmo.\n"
        )
    # Tudo que vem do site/da IA é escapado — um "<" solto faz o Telegram rejeitar a
    # mensagem inteira e o pedido de aprovação se perde.
    texto_proposta = esc(proposal["texto"])
    descricao = esc(proposal.get("full_description") or project.get("description") or "")

    # Orçamento generoso pro texto da proposta (é o que importa pra decidir) — a descrição
    # é truncada se precisar, nunca o texto da proposta em si.
    moldura = "\n<b>Descrição do projeto:</b>\n\n\n<b>Proposta:</b>\n"
    overhead = len(cabecalho) + len(valores) + len(moldura) + len(texto_proposta) + 50
    max_desc_chars = max(telegram_api.MSG_LIMIT - overhead, 200)
    descricao = telegram_api.truncate_escaped(descricao, max_desc_chars)

    return f"{cabecalho}{valores}\n<b>Descrição do projeto:</b>\n{descricao}\n\n<b>Proposta:</b>\n{texto_proposta}"


def approval_keyboard(
    project_id: str, edit_buttons: list[dict], texto_ia_falhou: bool = False, promovida: bool = False
) -> dict:
    """
    Teclado da mensagem de aprovação: a linha de edição (JobSource.edit_buttons),
    opcionalmente "🔄 Tentar gerar via IA novamente" (só quando proposal["texto_ia_falhou"]
    — ver proposal._build_texto), o botão que liga/desliga a proposta promovida
    (checkbox #highlight-bid do site, marcado no envio) e a linha final de decisão.
    """
    keyboard = [edit_buttons]
    if texto_ia_falhou:
        keyboard.append([{"text": "🔄 Tentar gerar texto via IA novamente", "callback_data": f"retryia:{project_id}"}])
    promo_label = "⭐ Promovida: SIM (clique pra desligar)" if promovida else "☆ Enviar como promovida"
    keyboard.append([{"text": promo_label, "callback_data": f"promo:{project_id}"}])
    keyboard.append([
        {"text": "✅ Aprovar", "callback_data": f"approve:{project_id}"},
        {"text": "❌ Rejeitar", "callback_data": f"reject:{project_id}"},
    ])
    return {"inline_keyboard": keyboard}


LINK_STATUS_LABELS = {
    "sent": "proposta enviada",
    "pending_approval": "aguardando sua aprovação",
    "awaiting_average": "aguardando a média de propostas",
    "rejected_by_user": "rejeitada por você",
    "failed": "falhou",
    "skipped_duplicate": "ignorado pelo filtro",
    "cancelled_by_user": "proposta cancelada por você",
}


def send_link_menu(project_id: str, url: str) -> None:
    """Resposta a um link colado no chat: situação do projeto + menu do que fazer."""
    rec = storage.get_application(project_id)
    linhas = [f"{_PREFIX}🔗 <b>{esc(rec['title']) if rec and rec.get('title') else 'Projeto ' + project_id}</b>", url]
    if rec:
        linhas.append(f"<b>Situação:</b> {esc(LINK_STATUS_LABELS.get(rec.get('status'), rec.get('status', '')))}")
        if rec.get("texto_variante"):
            estilo = TEXTO_VARIANTE_LABELS.get(rec["texto_variante"], rec["texto_variante"])
            linhas.append(f"<b>Estilo do texto:</b> {esc(estilo)}")
        if rec.get("resultado"):
            linhas.append(f"<b>Resultado marcado:</b> {rec['resultado']}")
    linhas.append("O que fazer com esse projeto?")
    telegram_api.send_message("\n".join(linhas), link_menu_keyboard(project_id))


def link_menu_keyboard(project_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📝 Preparar proposta", "callback_data": f"link:p:{project_id}"}],
            [
                {"text": "💬 Respondeu", "callback_data": f"link:r:{project_id}"},
                {"text": "🏆 Fechou", "callback_data": f"link:f:{project_id}"},
            ],
            [{"text": "🗑️ Cancelar proposta", "callback_data": f"link:c:{project_id}"}],
        ]
    }


def cancel_confirm_keyboard(project_id: str) -> dict:
    """Cancelar não tem volta — o menu de link pede uma segunda confirmação."""
    return {
        "inline_keyboard": [
            [{"text": "🗑️ Sim, cancelar a proposta", "callback_data": f"link:x:{project_id}"}],
            [{"text": "↩️ Voltar", "callback_data": f"link:m:{project_id}"}],
        ]
    }


def notify_cancel_result(url: str, title: str, success: bool, detail: str) -> None:
    titulo = esc(title) if title else esc(url)
    if success:
        texto = f"{_PREFIX}🗑️ <b>Proposta cancelada</b>\n{titulo}\n{esc(url)}"
    else:
        texto = f"{_PREFIX}⚠️ <b>Não consegui cancelar a proposta</b>\n{titulo}\n{esc(url)}\n<b>Motivo:</b> {esc(detail)}"
    telegram_api.send_message(texto)


def notify_promocao_perdida(project: dict) -> None:
    """Proposta promovida sem média não foi enviada: outro freelancer promoveu antes."""
    telegram_api.send_message(
        f"{_PREFIX}⏸️ <b>Promoção perdida — proposta não enviada</b>\n"
        f"<b>Projeto:</b> {esc(project.get('title', ''))}\n"
        f"<b>Link:</b> {esc(project.get('url', ''))}\n"
        "Outro freelancer promoveu antes. Voltou a aguardar a média de propostas — "
        "quando ela aparecer, chega um novo pedido de aprovação."
    )


def notify_manual_project_failed(url: str, reason: str, retry: bool = True) -> None:
    """
    Falha ao preparar um projeto enviado manualmente (link colado no chat).
    `retry=True` adiciona o botão "🔄 Tentar de novo".
    """
    reply_markup = None
    match = PROJECT_LINK_REGEX.search(url)
    if retry and match:
        reply_markup = telegram_api.single_button_keyboard("🔄 Tentar de novo", f"retry:{match.group(2)}")
    telegram_api.send_message(
        f"{_PREFIX}⚠️ <b>Não consegui preparar a proposta</b>\n<b>Link:</b> {esc(url)}\n<b>Motivo:</b> {esc(reason)}",
        reply_markup,
    )
