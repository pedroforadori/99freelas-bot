"""
Mensagens de clientes do 99Freelas ↔ Telegram, com resposta automática por IA.

Leitura (check_and_notify, cadência MESSAGES_POLL_INTERVAL_SECONDS): consulta pela API
JSON da caixa de entrada (a mesma que a página /messages usa — ver MESSAGES_API_* em
site_selectors.py) as conversas não lidas e pega as mensagens novas de cliente. Lidas com
visualizar=False: continuam não lidas no site. Chamadas via page.context.request — usa os
cookies da sessão sem navegar a Page, então dá pra rodar no meio da varredura de projetos.
Se a API falhar (rota mudou, sessão caiu), cai pro jeito antigo: badge do header em
/dashboard, avisando só quando o contador AUMENTA.

Resposta automática (mensagens.resposta_automatica no config.yaml — decisão do usuário,
2026-09-30: responder rápido converte mais): em conversa de projeto AINDA EM NEGOCIAÇÃO
(aberto, sem freelancer contratado, proposta não rejeitada), a IA (ai_writer.
generate_chat_reply) responde sozinha "como uma pessoa digitando" (ver split_human_messages/
schedule_parts: cumprimento na hora, cada parágrafo depois, no tempo de ler + pensar +
digitar) e o Telegram mostra projeto + mensagem do cliente + cada parte com o horário, com
"✋ Cancelar o que falta" e "🗑️ Apagar do site" (a mesma mensagem é atualizada a cada parte;
as partes vivem em "groups"). Não responde sozinha — manda a mensagem do
cliente e uma SUGESTÃO com "✅ Enviar sugestão" — quando a IA pede ("escalar": desconto,
prazo novo, reunião...), quando a resposta cita valor/prazo que o usuário ainda não tinha
passado ou contato/link (ai_writer.check_chat_reply_safety), ou quando a conversa passou de
mensagens.max_respostas_ia_por_conversa. Projeto já contratado/fechado: só notifica.

Resposta manual: reply no Telegram a qualquer mensagem ligada a uma conversa (notificação,
resposta da IA, confirmação) → Freelas99Source.handle_reply → queue_reply. O polling do
Telegram nunca toca o Playwright: botões e replies só gravam na fila ("outbox") e o tick
(process_outbox) envia/apaga. Tudo em data/message_threads.json: "threads" (message_id do
Telegram → conversa), "outbox", "drafts" (sugestões da IA ainda não enviadas) e "groups"
(respostas da IA em partes agendadas). Um envio (ou parte) fica "sending" ANTES do POST: se
o processo cair no meio, NÃO é reenviado (nem as partes seguintes) — o usuário é avisado pra
conferir (reenviar às cegas duplicaria a mensagem pro cliente).

Estado da leitura em data/messages_state.json: badge (unread_count), por conversa o id da
última mensagem de cliente já tratada (last_notified) e quantas respostas a IA já mandou
(auto_count).
"""
import html
import json
import os
import re
import random
import threading
import time
import urllib.parse
from datetime import datetime

from bot import ai_writer
from bot.sources.freelas99 import views
from bot import site_selectors as sel
from bot import telegram_api
from bot.logger_setup import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "messages_state.json")
THREADS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "message_threads.json")

MAX_THREADS = 500               # mapeamentos message_id → conversa guardados (os mais antigos saem)
MAX_DRAFTS = 200
MAX_MESSAGES_PER_CONVERSA = 20  # mensagens lidas por conversa (novas + contexto pra IA)
MAX_UNREAD_CONVERSAS = 20

_STATUS_SUCESSO = 1
_STATUS_DESLOGADO = 8
_STATUS_PROJETO_ABERTO = 2  # idStatusProjeto (2 aberto, 3 em andamento; 4/5/11 fechado/cancelado)

_FAIL_REASONS = {
    "SUSPECT_PATTERN_REQUIRE_CONFIRMATION": (
        "o 99Freelas detectou possível troca de contato fora da plataforma (e-mail, telefone, link...) "
        "e pediu confirmação — o bot não confirma por você. Reescreva sem o contato, ou envie pelo site."
    ),
    "SUSPECT_PATTERN_REQUIRE_CONFIRMATION_BLOCKED": (
        "o 99Freelas bloqueou a mensagem por possível troca de contato fora da plataforma."
    ),
}

_api_falhando = False  # só avisa (warning → Telegram) na 1ª falha de uma sequência


class InboxError(Exception):
    """Resposta da API de mensagens que não dá pra usar (status inesperado, formato mudou)."""


# --- Persistência -------------------------------------------------------------------------


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)  # escrita atômica, evita corromper o arquivo


def _load_cache() -> dict:
    return _load_json(CACHE_PATH, None) or {}


def read_cached() -> dict | None:
    with _LOCK:
        return _load_json(CACHE_PATH, None)


def _load_threads() -> dict:
    data = _load_json(THREADS_PATH, {})
    data.setdefault("threads", {})
    data.setdefault("outbox", [])
    data.setdefault("drafts", {})
    data.setdefault("groups", {})
    return data


def _trim(mapping: dict, limit: int) -> None:
    for velho in list(mapping)[: max(0, len(mapping) - limit)]:
        del mapping[velho]


def register_thread(telegram_message_id: int | None, conversa: dict) -> None:
    """Liga uma mensagem do Telegram à conversa — responder a ela responde ao cliente."""
    if not telegram_message_id:
        return
    with _LOCK:
        data = _load_threads()
        data["threads"][str(telegram_message_id)] = conversa
        _trim(data["threads"], MAX_THREADS)
        _save_json(THREADS_PATH, data)


def find_thread(telegram_message_id: int) -> dict | None:
    with _LOCK:
        return _load_threads()["threads"].get(str(telegram_message_id))


def _mark_handled(conversa_id, last_message_id: int, respondida: bool = True) -> None:
    """
    last_notified: até onde já agi (não dispara de novo). answered_floor (respondida=True):
    até onde o cliente já foi respondido — por você, pela IA (resposta completa) ou porque
    virou aviso/sugestão pra você decidir.
    """
    with _LOCK:
        cache = _load_cache()
        chave = str(conversa_id)
        notificadas = cache.setdefault("last_notified", {})
        notificadas[chave] = max(notificadas.get(chave, 0), last_message_id)
        if respondida:
            pisos = cache.setdefault("answered_floor", {})
            pisos[chave] = max(pisos.get(chave, 0), last_message_id)
        _save_json(CACHE_PATH, cache)


def _new_id() -> str:
    return f"{datetime.utcnow().timestamp():.6f}"


# --- API do site --------------------------------------------------------------------------


def _decode(resp) -> dict:
    """A API responde JSON url-encoded ("%7B%22status%22..."); desfaz e confere o formato."""
    if resp.status != 200:
        raise InboxError(f"HTTP {resp.status}")
    try:
        body = json.loads(urllib.parse.unquote(resp.text()))
    except ValueError as e:
        raise InboxError(f"resposta não é JSON: {resp.text()[:200]!r}") from e
    if not isinstance(body, dict) or "status" not in body:
        raise InboxError(f"resposta inesperada: {str(body)[:200]}")
    return body


def _api_get(page, url: str, data: dict) -> dict:
    resp = page.context.request.get(
        f"{url}?data={urllib.parse.quote(json.dumps(data))}",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = _decode(resp)
    status = body["status"].get("id")
    if status == _STATUS_DESLOGADO:
        raise InboxError("sessão do 99Freelas expirada")
    if status != _STATUS_SUCESSO:
        raise InboxError(f"status {status}: {body.get('failInfo')}")
    return body["result"]


def _fail_detail(body: dict) -> str:
    status = body["status"].get("id")
    if status == _STATUS_DESLOGADO:
        return "sessão do 99Freelas expirada — renove os cookies (import_cookies.py)"
    fail = body.get("failInfo") or {}
    key = fail.get("key") if isinstance(fail, dict) else None
    if key in _FAIL_REASONS:
        return _FAIL_REASONS[key]
    detalhe = (fail.get("message") if isinstance(fail, dict) else None) or key or f"status {status}"
    return f"o site recusou ({html_to_text(str(detalhe))})"


def html_to_text(raw: str | None) -> str:
    """Texto das mensagens vem em HTML (<br/>, &ccedil;, &nbsp;) — vira texto puro."""
    texto = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    texto = re.sub(r"<[^>]+>", "", texto)
    return html.unescape(texto).replace("\xa0", " ").strip()


def _fetch_unread_conversations(page) -> list[dict]:
    result = _api_get(
        page, sel.MESSAGES_API_CONVERSATIONS,
        {"diretorio": "unread", "dhCorte": 0, "start": 0, "limit": MAX_UNREAD_CONVERSAS},
    )
    registros = result.get("registros")
    if not isinstance(registros, list):
        raise InboxError(f"lista de conversas sem 'registros': {str(result)[:200]}")
    return registros


def _fetch_messages(page, conversa_id: int) -> list[dict]:
    result = _api_get(
        page,
        sel.MESSAGES_API_LIST,
        {"idConversa": conversa_id, "reverse": True, "visualizar": False, "dhCorte": 0, "start": 0,
         "limit": MAX_MESSAGES_PER_CONVERSA},
    )
    mensagens = result.get("mensagensDaConversa")
    if not isinstance(mensagens, list):
        raise InboxError(f"conversa {conversa_id} sem 'mensagensDaConversa': {str(result)[:200]}")
    return mensagens


def _negotiation_blocker(conv: dict) -> str | None:
    """Por que a IA NÃO deve responder sozinha nessa conversa (None = em negociação)."""
    if conv.get("freelancerProjeto"):
        return "projeto em andamento (você já foi contratado)"
    if conv.get("propostaRejeitada"):
        return "sua proposta foi rejeitada nesse projeto"
    if conv.get("readOnly") or conv.get("fechada") or conv.get("idStatusProjeto") != _STATUS_PROJETO_ABERTO:
        return "o projeto não está mais aberto"
    return None


def fetch_new_client_messages(page, state: dict, ignorar_meus: dict | None = None) -> list[dict]:
    """
    Conversas não lidas em que chegou mensagem do CLIENTE depois da última vez (id maior que
    last_notified e sem dhVisualizacao — não lida por mim). Pra cada uma:
    - "recem_chegadas": só essas mensagens novas (o que o aviso simples mostra);
    - "mensagens": TUDO do cliente que ainda está sem resposta — depois do answered_floor da
      conversa e da minha última mensagem própria. É o que a IA responde: se o cliente mandou
      "fala meu brother" e depois "as tecnologias são...", ou escreveu de novo no meio de uma
      resposta em partes, ela responde tudo junto. `ignorar_meus[conversa]` = ids de partes
      de respostas da IA ainda em andamento/substituídas (um "Fala, Bruno!" já enviado de uma
      resposta que foi substituída não conta como "já respondi");
    - "ultima_chegada": epoch da mensagem mais recente do cliente (pra esperar ele parar de
      escrever antes de responder).
    Mensagens minhas e de sistema ("Enviei uma proposta...") vão pro histórico da IA.
    """
    last_notified = state.get("last_notified", {})
    floors = state.get("answered_floor", {})
    ignorar_meus = ignorar_meus or {}
    novas = []
    for conv in _fetch_unread_conversations(page):
        conversa_id = conv.get("idConversa")
        if not conversa_id:
            continue
        eu = conv.get("idFreelancer")
        todas = sorted(
            (m for m in _fetch_messages(page, conversa_id) if not m.get("excluida")),
            key=lambda m: m.get("id", 0),
        )

        def do_cliente(m):
            return (m.get("pessoa") or {}).get("id") != eu and not m.get("sistema")

        recem = [
            m for m in todas
            if do_cliente(m) and not m.get("dhVisualizacaoInMillis")
            and m.get("id", 0) > last_notified.get(str(conversa_id), 0)
        ]
        if not recem:
            continue
        ignorar = set(ignorar_meus.get(str(conversa_id), ()))
        minha_ultima = max(
            (m["id"] for m in todas if not do_cliente(m) and not m.get("sistema") and m["id"] not in ignorar),
            default=0,
        )
        piso = max(floors.get(str(conversa_id), 0), minha_ultima)
        pendentes = [m for m in todas if do_cliente(m) and m["id"] > piso]
        ids_pendentes = {m["id"] for m in pendentes}
        cliente = html_to_text(conv.get("nomeCliente")) or "cliente"
        freelancer = (html_to_text(conv.get("nomeFreelancer")) or "").split(" ")[0]

        def resumo(m):
            return {"id": m["id"], "texto": html_to_text(m.get("texto")), "tem_arquivos": bool(m.get("arquivos"))}

        novas.append({
            "conversa": {
                "id": conversa_id,
                "cliente": cliente,
                "projeto": html_to_text(conv.get("nomeProjeto")),
                "freelancer": freelancer,
            },
            "recem_chegadas": [resumo(m) for m in recem],
            "ultimo_id": recem[-1]["id"],
            "mensagens": [resumo(m) for m in pendentes],
            "ultima_chegada": max((m.get("dhCriacaoInMillis") or 0) for m in recem) / 1000,
            "historico": [
                ("Site (sistema)" if m.get("sistema") else (cliente if do_cliente(m) else freelancer or "Eu"),
                 html_to_text(m.get("texto")) or "(anexo)")
                for m in todas if m["id"] not in ids_pendentes
            ],
            "meus_textos": [html_to_text(m.get("texto")) for m in todas if not do_cliente(m)],
            "ja_conversou": any(
                not do_cliente(m) and not m.get("sistema") for m in todas if m["id"] not in ids_pendentes
            ),
            "bloqueio_ia": _negotiation_blocker(conv),
        })
    return novas


def send_reply(page, conversa_id: int, texto: str) -> tuple[bool, str, int | None]:
    """
    POST da mensagem. Retorna (ok, detalhe, id da mensagem criada no site — o que o botão
    "🗑️ Apagar do site" usa). Nunca confirma o aviso de "padrão suspeito" do site (o chat
    do site reenviaria com confirmarPadraoDetectado=true depois de um checkbox de termos de
    uso) — isso fica com o usuário.
    """
    resp = page.context.request.post(
        sel.MESSAGES_API_SEND,
        form={"data": json.dumps({"idConversa": conversa_id, "texto": texto, "idsArquivos": []})},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = _decode(resp)
    if body["status"].get("id") == _STATUS_SUCESSO:
        result = body.get("result")
        return True, "", (result.get("id") if isinstance(result, dict) else None)
    return False, _fail_detail(body), None


def delete_message(page, mensagem_id: int) -> tuple[bool, str]:
    """Mesmo endpoint do "Excluir" de cada mensagem no chat do site (deletar=false desfaz)."""
    resp = page.context.request.post(
        sel.MESSAGES_API_DELETE.format(id=mensagem_id) + "?deletar=true",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = _decode(resp)
    if body["status"].get("id") == _STATUS_SUCESSO:
        return True, ""
    return False, _fail_detail(body)


# --- Loop: leitura e resposta automática ------------------------------------------------------


def _check_badge(page) -> None:
    """Jeito antigo (fallback): badge do header em /dashboard, avisa quando o total AUMENTA."""
    anterior_count = (read_cached() or {}).get("unread_count", 0)
    try:
        # "load" em vez de "networkidle": timeout intermitente de networkidle em /dashboard
        # confirmado em produção (2026-09-18, ver connections.py).
        page.goto(sel.DASHBOARD_URL, wait_until="load")
        novo_count = _read_unread_count(page)
    except Exception as e:
        log.warning("Erro ao checar mensagens não lidas: %s", e)
        return
    with _LOCK:
        cache = _load_cache()
        cache.update({"unread_count": novo_count, "fetched_at": datetime.utcnow().isoformat()})
        _save_json(CACHE_PATH, cache)
    if novo_count > anterior_count:
        views.notify_new_messages(novo_count, anterior_count)


def _read_unread_count(page) -> int:
    container = page.query_selector(sel.MESSAGES_BADGE_CONTAINER)
    if container is None:
        return 0
    classes = (container.get_attribute("class") or "").split()
    if "show" not in classes:
        # Container existe mas está escondido — sem mensagens não lidas, mesmo que o
        # <span> interno ainda tenha um valor antigo.
        return 0
    valor = page.query_selector(sel.MESSAGES_BADGE_VALUE)
    texto = (valor.inner_text().strip() if valor else "")
    return int(texto) if texto.isdigit() else 0


def _auto_reply_settings(config: dict | None) -> tuple[bool, int, float]:
    cfg = (config or {}).get("mensagens") or {}
    return (
        bool(cfg.get("resposta_automatica", False)),
        int(cfg.get("max_respostas_ia_por_conversa", 8)),
        float(cfg.get("aguardar_cliente_parar_segundos", 60)),
    )


def _open_groups(conversa_id) -> list[dict]:
    with _LOCK:
        grupos = _load_threads()["groups"].values()
    return [
        g for g in grupos
        if str(g["conversa"]["id"]) == str(conversa_id)
        and any(p["status"] in ("pending", "sending") for p in g["partes"])
    ]


def _ignored_own_ids() -> dict:
    """Por conversa: ids (no site) de partes enviadas de respostas da IA em andamento ou substituídas."""
    with _LOCK:
        grupos = list(_load_threads()["groups"].values())
    ignorar: dict = {}
    for g in grupos:
        if g.get("estado", "ativo") in ("ativo", "substituido"):
            ids = [p["mensagem_id"] for p in g["partes"] if p["status"] == "sent" and p.get("mensagem_id")]
            ignorar.setdefault(str(g["conversa"]["id"]), []).extend(ids)
    return ignorar


def _supersede(conversa_id) -> None:
    """
    O cliente escreveu de novo enquanto uma resposta da IA ainda saía em partes: o que falta
    não sai mais (responderia algo já desatualizado). A próxima resposta cobre tudo.
    """
    for grupo in _open_groups(conversa_id):
        if any(p["status"] == "pending" for p in grupo["partes"]):
            _cancel_pending(grupo["id"], status="superseded", estado="substituido")
            _refresh_group_message(grupo["id"])
            log.info("Resposta da IA %s substituída: o cliente mandou mensagem nova.", grupo["id"])


def check_and_notify(page, config: dict | None = None) -> None:
    """
    Chamada pelo Freelas99Source (tick e entre os preparos da varredura) e por dry_run.py
    (sem config → nunca responde sozinho). Cada conversa com mensagem nova de cliente vira
    resposta automática (+ aviso) ou aviso com sugestão; a mensagem só é marcada como
    tratada depois que algo foi feito com ela (Telegram fora → tenta de novo depois, mas uma
    resposta automática já na fila de envio nunca é gerada duas vezes).
    """
    global _api_falhando
    try:
        novas = fetch_new_client_messages(page, _load_cache(), _ignored_own_ids())
    except Exception as e:
        if not _api_falhando:
            log.warning("API de mensagens do 99Freelas falhou, usando o badge do header: %s", e)
        else:
            log.info("API de mensagens ainda falhando: %s", e)
        _api_falhando = True
        _check_badge(page)
        return
    if _api_falhando:
        log.info("API de mensagens do 99Freelas voltou a responder.")
        _api_falhando = False

    auto, max_auto, aguardar = _auto_reply_settings(config)
    for item in novas:
        conversa = item["conversa"]
        ultimo_id = item["ultimo_id"]
        if not item["mensagens"]:
            _mark_handled(conversa["id"], ultimo_id)  # já respondido (ex: você respondeu pelo site)
            continue
        motivo = item["bloqueio_ia"] if auto else None
        if auto and motivo is None:
            if _load_cache().get("auto_count", {}).get(str(conversa["id"]), 0) >= max_auto:
                motivo = f"a IA já mandou {max_auto} respostas nessa conversa — daqui pra frente é com você"
            else:
                # Uma pessoa para de digitar quando chega mensagem nova, e espera o outro
                # terminar de escrever antes de responder.
                _supersede(conversa["id"])
                if _now() - item["ultima_chegada"] < aguardar:
                    continue  # o cliente pode estar escrevendo mais — checa de novo depois
                _auto_reply(page, item, config)
                continue

        if auto:
            message_id = views.notify_client_messages(conversa, item["recem_chegadas"], motivo_sem_ia=motivo)
        else:
            message_id = views.notify_client_messages(conversa, item["recem_chegadas"])
        if message_id is None:
            continue
        register_thread(message_id, conversa)
        _mark_handled(conversa["id"], ultimo_id)
        log.info("Mensagem nova de %s (conversa %s) notificada.", conversa["cliente"], conversa["id"])

    # Respostas automáticas enfileiradas acima saem já, sem esperar o próximo tick.
    process_outbox(page)


def _auto_reply(page, item: dict, config: dict) -> None:
    conversa = item["conversa"]
    mensagens = item["mensagens"]
    ultimo_id = max(item["ultimo_id"], mensagens[-1]["id"])
    ctx = {
        "freelancer": conversa.get("freelancer"),
        "cliente": conversa["cliente"],
        "projeto": conversa["projeto"],
        "historico": item["historico"],
        "novas": [m["texto"] or "(anexo)" for m in mensagens],
        "tem_anexo": any(m["tem_arquivos"] for m in mensagens),
        "ja_conversou": item["ja_conversou"],
    }
    resultado = ai_writer.generate_chat_reply(ctx, config)
    if resultado is None:
        _escalate(item, "a IA falhou ao gerar a resposta", "")
        return
    if resultado["acao"] == "escalar":
        _escalate(item, resultado["motivo"], resultado["rascunho"])
        return

    texto = resultado["texto"]
    violacao = ai_writer.check_chat_reply_safety(texto, item["meus_textos"])
    if violacao:
        _escalate(item, f"a resposta da IA {violacao}", texto)
        return
    if _approval_required(config):
        # Modo aprovação: tudo igual (espera, travas, divisão em partes), mas só sai com o
        # clique em "✅ Enviar pro cliente" — aí vai em partes, no ritmo de digitação.
        _escalate(item, None, texto, digitacao=_typing_settings(config))
        return

    cliente = [m["texto"] or "(anexo)" for m in mensagens]
    horarios_de = lambda partes: schedule_parts(partes, cliente, _typing_settings(config), _now())  # noqa: E731
    _create_group(conversa, cliente, ultimo_id, texto, horarios_de, conta_resposta_ia=True)


def _approval_required(config: dict | None) -> bool:
    return bool(((config or {}).get("mensagens") or {}).get("aprovar_antes_de_enviar", False))


def _create_group(conversa: dict, cliente: list[str], ultimo_id: int, texto: str, horarios_de, conta_resposta_ia: bool) -> str:
    """
    Resposta em partes agendadas (ver split_human_messages/schedule_parts). Grava o grupo E
    marca a mensagem como tratada no mesmo lock: se o processo cair depois disso, as partes
    saem nos próximos ticks (ou viram aviso de "não sei se foi"), mas a resposta nunca é
    gerada de novo.
    """
    partes = split_human_messages(texto)
    horarios = horarios_de(partes)
    gid = f"{conversa['id']}-{ultimo_id}"
    grupo = {
        "id": gid, "conversa": conversa, "cliente": cliente,
        "partes": [{"texto": p, "send_at": t, "status": "pending", "mensagem_id": None} for p, t in zip(partes, horarios)],
        "telegram_message_id": None, "criado_em": _now(),
        "estado": "ativo", "ultimo_cliente_id": ultimo_id,
    }
    with _LOCK:
        data = _load_threads()
        data["groups"][gid] = grupo
        _trim(data["groups"], MAX_GROUPS)
        _save_json(THREADS_PATH, data)
        cache = _load_cache()
        notificadas = cache.setdefault("last_notified", {})
        notificadas[str(conversa["id"])] = max(notificadas.get(str(conversa["id"]), 0), ultimo_id)
        if conta_resposta_ia:
            contagem = cache.setdefault("auto_count", {})
            contagem[str(conversa["id"])] = contagem.get(str(conversa["id"]), 0) + 1
        _save_json(CACHE_PATH, cache)
    log.info("Resposta pra %s (conversa %s) agendada em %d parte(s).", conversa["cliente"], conversa["id"], len(partes))
    _refresh_group_message(gid)
    return gid


# --- Resposta da IA "como uma pessoa digitando" -----------------------------------------------
# Pedido do usuário (2026-09-30): resposta instantânea a toda hora parece robô. O cumprimento
# ("Fala, Bruno!", "Bom dia!") sai na hora; cada parágrafo seguinte vira uma mensagem
# separada, com o tempo que uma pessoa levaria pra ler o cliente, pensar e digitar aquele
# texto. Enquanto falta parte, o Telegram mostra o plano com "✋ Cancelar o que falta".

MAX_GROUPS = 200
MAX_PARTES = 4

_GREETING = re.compile(
    r"^(ol[áa]|oi+|opa|fala|e a[íi]|eai|salve|bom dia|boa tarde|boa noite|tudo (bem|certo|bom))\b",
    re.I,
)


def _now() -> float:
    return time.time()


def split_human_messages(texto: str) -> list[str]:
    """
    Quebra a resposta em mensagens de chat: o cumprimento (se a primeira frase for um e for
    curta) sozinho, depois um parágrafo por mensagem (no máximo MAX_PARTES — o excesso junta
    na última).
    """
    paragrafos = [p.strip() for p in re.split(r"\n\s*\n", texto.strip()) if p.strip()]
    partes = []
    if paragrafos:
        primeiro = paragrafos[0]
        # 1ª frase (até o primeiro ! ? ou ., na mesma linha, até 60 caracteres): "Fala, Bruno!"
        m = re.match(r"^([^\n!?.]{1,60}[!?.])(\s+|$)", primeiro)
        if m and _GREETING.match(primeiro):
            partes.append(m.group(1).strip())
            resto = primeiro[m.end():].strip()
            paragrafos[0] = resto
            paragrafos = [p for p in paragrafos if p]
    partes += paragrafos
    if len(partes) > MAX_PARTES:
        partes = partes[: MAX_PARTES - 1] + ["\n\n".join(partes[MAX_PARTES - 1:])]
    return partes or [texto.strip()]


def _typing_settings(config: dict | None) -> dict:
    cfg = ((config or {}).get("mensagens") or {}).get("digitacao") or {}
    return {
        "cps": tuple(cfg.get("caracteres_por_segundo", (3.5, 5.0))),
        "pensar": tuple(cfg.get("pensar_segundos", (15, 45))),
        "pausa": tuple(cfg.get("pausa_entre_mensagens_segundos", (3, 10))),
        "leitura_cps": float(cfg.get("leitura_caracteres_por_segundo", 25)),
    }


def schedule_parts(
    partes: list[str], textos_cliente: list[str], cfg: dict, agora: float, ja_pensou: bool = False
) -> list[float]:
    """
    Horário (epoch) de envio de cada parte. Cumprimento: agora. Primeira parte de conteúdo:
    ler as mensagens do cliente + pensar + digitar. Cada parte seguinte: pausa + digitar. Cada
    texto tem o seu tempo, com variação aleatória. `ja_pensou` (resposta aprovada por você no
    Telegram — o cliente já esperou): a 1ª parte de conteúdo leva só o tempo de digitar.
    """
    horarios = []
    t = agora
    leitura = min(60.0, max(3.0, sum(len(x or "") for x in textos_cliente) / cfg["leitura_cps"]))
    if ja_pensou:
        leitura, cfg = 0.0, {**cfg, "pensar": (0, 0)}
    conteudo_iniciado = False
    for i, parte in enumerate(partes):
        if i == 0 and len(partes) > 1 and _GREETING.match(parte) and len(parte) <= 60:
            horarios.append(t)  # cumprimento: na hora
            continue
        digitar = len(parte) / random.uniform(*cfg["cps"])
        if not conteudo_iniciado:
            t += leitura + random.uniform(*cfg["pensar"]) + digitar
            conteudo_iniciado = True
        else:
            t += random.uniform(*cfg["pausa"]) + digitar
        horarios.append(t)
    return horarios


def _get_group(gid: str) -> dict | None:
    with _LOCK:
        return _load_threads()["groups"].get(gid)


def _set_group_state(gid: str, estado: str) -> None:
    with _LOCK:
        data = _load_threads()
        if gid in data["groups"]:
            data["groups"][gid]["estado"] = estado
            _save_json(THREADS_PATH, data)


def _update_part(gid: str, idx: int, **campos) -> dict | None:
    with _LOCK:
        data = _load_threads()
        grupo = data["groups"].get(gid)
        if grupo is None:
            return None
        grupo["partes"][idx].update(campos)
        _save_json(THREADS_PATH, data)
        return grupo


def _cancel_pending(gid: str, status: str = "cancelled", estado: str | None = None) -> dict | None:
    """
    Tira da fila (status final) as partes que ainda não começaram a ser enviadas. `estado`
    do grupo: "substituido" (cliente escreveu de novo — a próxima resposta cobre tudo) ou,
    por padrão, encerrado ("cancelado" etc.): aí as mensagens do cliente contam como
    respondidas (answered_floor) — você assumiu a conversa.
    """
    with _LOCK:
        data = _load_threads()
        grupo = data["groups"].get(gid)
        if grupo is None:
            return None
        for parte in grupo["partes"]:
            if parte["status"] == "pending":
                parte["status"] = status
        if estado:
            grupo["estado"] = estado
        elif grupo.get("estado", "ativo") == "ativo":
            grupo["estado"] = "encerrado"
        _save_json(THREADS_PATH, data)
    if grupo["estado"] == "encerrado" and grupo.get("ultimo_cliente_id"):
        _mark_handled(grupo["conversa"]["id"], grupo["ultimo_cliente_id"])
    return grupo


def _refresh_group_message(gid: str) -> None:
    """Manda (1ª vez) ou atualiza a mensagem do Telegram que mostra o plano/andamento."""
    grupo = _get_group(gid)
    if grupo is None:
        return
    texto, teclado = views.render_auto_reply(grupo)
    message_id = grupo.get("telegram_message_id")
    if message_id:
        telegram_api.edit_text(message_id, texto, teclado)
        return
    message_id = telegram_api.send_with_keyboard(texto, teclado)
    if message_id:
        with _LOCK:
            data = _load_threads()
            if gid in data["groups"]:
                data["groups"][gid]["telegram_message_id"] = message_id
                _save_json(THREADS_PATH, data)
        register_thread(message_id, grupo["conversa"])


def cancel_group(gid: str) -> tuple[int, int] | None:
    """
    "✋ Cancelar o que falta" (vem do polling do Telegram — só mexe no arquivo). Retorna
    (partes canceladas, partes já enviadas), ou None se o grupo não existe.
    """
    grupo = _cancel_pending(gid)
    if grupo is None:
        return None
    canceladas = sum(p["status"] == "cancelled" for p in grupo["partes"])
    enviadas = sum(p["status"] == "sent" for p in grupo["partes"])
    _refresh_group_message(gid)
    return canceladas, enviadas


def queue_group_delete(gid: str, telegram_message_id: int | None) -> int | None:
    """
    "🗑️ Apagar do site" de uma resposta da IA: cancela o que falta e enfileira a exclusão
    de todas as partes já enviadas. Retorna quantas vão ser apagadas (None = grupo não existe).
    """
    grupo = _cancel_pending(gid)
    if grupo is None:
        return None
    ids = [p["mensagem_id"] for p in grupo["partes"] if p["status"] == "sent" and p["mensagem_id"]]
    if ids:
        queue_delete(ids, telegram_message_id, group_id=gid)
    return len(ids)


def next_send_in() -> float | None:
    """Segundos até a próxima parte agendada (None = nada agendado) — pro loop acordar na hora."""
    with _LOCK:
        grupos = _load_threads()["groups"].values()
    pendentes = [p["send_at"] for g in grupos for p in g["partes"] if p["status"] == "pending"]
    return max(0.0, min(pendentes) - _now()) if pendentes else None


def process_groups(page) -> None:
    """Envia as partes que já deram a hora, em ordem, e atualiza a mensagem do Telegram."""
    with _LOCK:
        gids = [gid for gid, g in _load_threads()["groups"].items()
                if any(p["status"] in ("pending", "sending") for p in g["partes"])]
    for gid in gids:
        _process_group(page, gid)


def _process_group(page, gid: str) -> None:
    while True:
        grupo = _get_group(gid)
        if grupo is None:
            return
        abertas = [(i, p) for i, p in enumerate(grupo["partes"]) if p["status"] in ("pending", "sending")]
        if not abertas:
            return
        idx, parte = abertas[0]
        conversa = grupo["conversa"]

        if parte["status"] == "sending":
            # Caiu entre marcar "sending" e terminar o POST — pode ter ido ou não. Não
            # reenvia nem manda o resto (a conversa ficaria sem sentido): avisa.
            _update_part(gid, idx, status="uncertain")
            _cancel_pending(gid)
            _refresh_group_message(gid)
            register_thread(views.notify_reply_uncertain(conversa, parte["texto"]), conversa)
            return
        if parte["send_at"] > _now():
            return

        _update_part(gid, idx, status="sending")
        try:
            ok, detalhe, mensagem_id = send_reply(page, conversa["id"], parte["texto"])
        except Exception as e:
            log.exception("Erro ao enviar parte %d da resposta da IA (conversa %s): %s", idx + 1, conversa["id"], e)
            ok, detalhe, mensagem_id = False, f"erro inesperado: {e}", None
        log.info("Parte %d/%d da resposta da IA pra %s: %s %s", idx + 1, len(grupo["partes"]),
                 conversa["cliente"], "ENVIADA" if ok else "FALHOU", detalhe)

        if ok:
            grupo = _update_part(gid, idx, status="sent", mensagem_id=mensagem_id, sent_at=_now())
            if grupo and all(p["status"] == "sent" for p in grupo["partes"]):
                _set_group_state(gid, "concluido")
                if grupo.get("ultimo_cliente_id"):
                    _mark_handled(conversa["id"], grupo["ultimo_cliente_id"])
            _refresh_group_message(gid)
            continue

        # O site recusou uma parte: não manda o resto; o que faltou vira sugestão.
        _update_part(gid, idx, status="failed", detalhe=detalhe)
        grupo = _cancel_pending(gid)
        _refresh_group_message(gid)
        faltou = "\n\n".join(p["texto"] for p in grupo["partes"] if p["status"] in ("failed", "cancelled"))
        draft_id = _save_draft(conversa, faltou, f"{idx}{gid.rsplit('-', 1)[-1]}")
        mensagens = [{"texto": t, "tem_arquivos": False} for t in grupo["cliente"]]
        register_thread(
            views.notify_escalation(conversa, mensagens, f"a IA respondeu, mas {detalhe}", faltou, draft_id),
            conversa,
        )
        return


def _save_draft(conversa: dict, texto: str, ref, extra: dict | None = None) -> str | None:
    """
    Guarda a sugestão pro botão de enviar. Id = conversa-ref (cabe nos 64 bytes). `extra`
    com "digitacao" = resposta do modo aprovação: ao aprovar, sai em partes (queue_draft).
    """
    if not texto:
        return None
    draft_id = f"{conversa['id']}-{ref}"
    with _LOCK:
        data = _load_threads()
        data["drafts"][draft_id] = {"conversa": conversa, "texto": texto, **(extra or {})}
        _trim(data["drafts"], MAX_DRAFTS)
        _save_json(THREADS_PATH, data)
    return draft_id


def _escalate(item: dict, motivo: str | None, rascunho: str, digitacao: dict | None = None) -> None:
    """
    A IA não responde sozinha: mensagem do cliente + motivo + sugestão pro usuário.
    motivo=None + `digitacao` = modo aprovação (a resposta passou em tudo, só falta o clique).
    """
    conversa = item["conversa"]
    ultimo_id = max(item["ultimo_id"], item["mensagens"][-1]["id"])
    extra = None
    if digitacao:
        extra = {"digitacao": digitacao, "cliente": [m["texto"] or "(anexo)" for m in item["mensagens"]],
                 "ultimo_cliente_id": ultimo_id}
    draft_id = _save_draft(conversa, rascunho, item["mensagens"][-1]["id"], extra)
    message_id = views.notify_escalation(conversa, item["mensagens"], motivo, rascunho, draft_id)
    if message_id is None:
        return  # Telegram fora — tenta de novo na próxima checagem
    register_thread(message_id, conversa)
    _mark_handled(conversa["id"], ultimo_id)
    log.info("Mensagem de %s (conversa %s) precisa de você: %s", conversa["cliente"], conversa["id"],
             motivo or "resposta da IA aguardando aprovação")


# --- Fila de envio (respostas manuais, sugestões, respostas da IA) e de exclusão ---------------


def _enqueue(item: dict) -> None:
    with _LOCK:
        data = _load_threads()
        data["outbox"].append({"id": _new_id(), "status": "pending", "queued_at": datetime.utcnow().isoformat(), **item})
        _save_json(THREADS_PATH, data)


def queue_reply(telegram_message_id: int, texto: str) -> dict | None:
    """
    Resposta do usuário (reply no Telegram) a uma mensagem ligada a uma conversa. Só grava
    na fila — o envio é no tick. None = essa mensagem do Telegram não é de nenhuma conversa.
    """
    conversa = find_thread(telegram_message_id)
    if conversa is None:
        return None
    _enqueue({"kind": "send", "origem": "voce", "conversa": conversa, "texto": texto})
    return conversa


def queue_draft(draft_id: str, telegram_message_id: int | None) -> str | None:
    """
    Botão de enviar sugestão/resposta aprovada. Retorna "partes" (resposta do modo aprovação:
    vira grupo em partes, no ritmo de digitação, acompanhado numa mensagem própria),
    "unica" (sugestão comum: uma mensagem só, pela fila) ou None (já enviada/expirada).
    """
    with _LOCK:
        data = _load_threads()
        draft = data["drafts"].pop(draft_id, None)
        if draft is None:
            return None
        if not draft.get("digitacao"):
            data["outbox"].append({
                "id": _new_id(), "kind": "send", "origem": "sugestao", "conversa": draft["conversa"],
                "texto": draft["texto"], "label_message_id": telegram_message_id, "status": "pending",
                "queued_at": datetime.utcnow().isoformat(),
            })
        _save_json(THREADS_PATH, data)
    if not draft.get("digitacao"):
        return "unica"
    cfg = {k: tuple(v) if isinstance(v, list) else v for k, v in draft["digitacao"].items()}
    _create_group(
        draft["conversa"], draft.get("cliente", []), draft["ultimo_cliente_id"], draft["texto"],
        lambda partes: schedule_parts(partes, [], cfg, _now(), ja_pensou=True), conta_resposta_ia=False,
    )
    return "partes"


def queue_delete(mensagem_ids: list[int], telegram_message_id: int | None, group_id: str | None = None) -> None:
    """
    Botão "🗑️ Apagar do site" (uma mensagem, ou todas as partes de uma resposta da IA —
    `group_id` é o que o botão recolocado mostra se a exclusão falhar).
    """
    _enqueue({
        "kind": "delete", "mensagem_ids": list(mensagem_ids), "label_message_id": telegram_message_id,
        "group_id": group_id,
    })


def _set_outbox_status(item_id: str, status: str | None) -> None:
    """status=None tira o item da fila."""
    with _LOCK:
        data = _load_threads()
        if status is None:
            data["outbox"] = [i for i in data["outbox"] if i["id"] != item_id]
        else:
            for i in data["outbox"]:
                if i["id"] == item_id:
                    i["status"] = status
        _save_json(THREADS_PATH, data)


def process_outbox(page) -> None:
    """
    Envia/apaga o que está na fila e as partes de respostas da IA que já deram a hora.
    Resultado de cada item vai pro Telegram. Barato quando não há nada (só lê um arquivo).
    """
    with _LOCK:
        itens = list(_load_threads()["outbox"])
    for item in itens:
        if item.get("kind") == "delete":
            _process_delete(page, item)
        else:
            _process_send(page, item)
    process_groups(page)


def _process_send(page, item: dict) -> None:
    conversa = item["conversa"]
    if item["status"] == "sending":
        # Caiu entre marcar "sending" e terminar o POST — pode ter ido ou não.
        _set_outbox_status(item["id"], None)
        register_thread(views.notify_reply_uncertain(conversa, item["texto"]), conversa)
        return

    _set_outbox_status(item["id"], "sending")
    try:
        ok, detalhe, mensagem_id = send_reply(page, conversa["id"], item["texto"])
    except Exception as e:
        log.exception("Erro ao enviar mensagem pra conversa %s: %s", conversa["id"], e)
        ok, detalhe, mensagem_id = False, f"erro inesperado: {e}", None
    _set_outbox_status(item["id"], None)
    origem = item.get("origem", "voce")
    log.info("Mensagem (%s) pra %s (conversa %s): %s %s", origem, conversa["cliente"], conversa["id"],
             "ENVIADA" if ok else "FALHOU", detalhe)

    if item.get("label_message_id"):
        telegram_api.edit_reply_markup(
            item["label_message_id"],
            telegram_api.static_label_keyboard("✅ Sugestão enviada" if ok else "❌ Não enviada — veja abaixo"),
        )
    message_id = views.notify_reply_result(conversa, item["texto"], ok, detalhe, mensagem_id)
    # Toda mensagem de resultado também vira thread: responder a ela manda outra mensagem.
    register_thread(message_id, conversa)


def _process_delete(page, item: dict) -> None:
    _set_outbox_status(item["id"], None)  # apagar é idempotente; não precisa de "sending"
    ids = item.get("mensagem_ids") or [item["mensagem_id"]]  # item antigo: uma mensagem só
    ok, detalhe = True, ""
    for mensagem_id in ids:
        try:
            ok_um, detalhe_um = delete_message(page, mensagem_id)
        except Exception as e:
            log.exception("Erro ao apagar mensagem %s: %s", mensagem_id, e)
            ok_um, detalhe_um = False, f"erro inesperado: {e}"
        log.info("Apagar mensagem %s do site: %s %s", mensagem_id, "OK" if ok_um else "FALHOU", detalhe_um)
        if not ok_um:
            ok, detalhe = False, detalhe_um
    if item.get("label_message_id"):
        if ok:
            rotulo = "🗑️ Apagada do site" if len(ids) == 1 else f"🗑️ {len(ids)} mensagens apagadas do site"
            teclado = telegram_api.static_label_keyboard(rotulo)
        else:
            teclado = views.delete_keyboard(item.get("group_id") or ids[0])  # botão de volta pra tentar de novo
        telegram_api.edit_reply_markup(item["label_message_id"], teclado)
    if not ok:
        telegram_api.send_message(f"{views.TAG} ❌ Não consegui apagar a mensagem do site: {telegram_api.esc(detalhe)}")
