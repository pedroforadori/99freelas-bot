"""
Fonte APinfo: parse/filtros portados do bot standalone e a máquina de estados de
ApinfoSource (um passo por tick, só quando a pausa anti-bloqueio passou). Sem rede: o
HTTP do APinfo é um site falso em requests.Session.request, o SMTP é mockado e o relógio
(client.now) é controlado pelo teste.
"""
from urllib.parse import parse_qsl

import pytest
import requests

from bot import email_sender
from bot.sources import registry
from bot.sources.apinfo import client, views
from bot.sources.apinfo.source import ApinfoSource
from tests.conftest import FREELAS99, GITHUB


def _box(codigo: str, cargo_html: str, empresa: str = "ACME Ltda", local: str = "Home Office - HO",
         data: str = "23/09/26", desc: str = "Vaga para React.") -> str:
    return f"""
    <div class="box-vagas">
      <div class="info-data">{local} - {data}</div>
      <div class="cargo">{cargo_html}</div>
      <div class="info">Empresa ....: {empresa} Código: {codigo}</div>
      <div class="texto"><p>{desc}</p></div>
      <a class="btn3" href="enviecv.cfm?codvaga={codigo}&amp;pkey=k{codigo}">Envie seu currículo</a>
    </div>"""


def _listing(*boxes: str, pagina: int = 1, total: int = 1) -> str:
    paginacao = f"""
      <p>Página {pagina} de {total}</p>
      <form action="list4.cfm" method="post">
        <input type="hidden" name="keyw" value="react">
        <input type="hidden" name="tcv" value="1">
        <input type="text" name="pag" value="{pagina}">
        <input type="submit" value="Ir">
      </form>"""
    return f"<html><body>{''.join(boxes)}{paginacao}</body></html>"


FORM_HTML = """<html><body><form id="form-incluir-cv" action="enviecv.cfm" method="post">
  <input type="hidden" name="codvaga" value="{codigo}"><input type="hidden" name="pkey" value="k{codigo}">
  <input type="text" name="cpf2"><input type="password" name="chave3"></form></body></html>"""

CONTACT_HTML = """<html><body><p>Dados para o envio do curriculum</p>
  <p>Empresa : ACME Ltda</p>
  <p>Email : <a href="mailto:{email}?subject=x">{email}</a></p>
  <p>Assunto a ser colocado no email : {assunto}</p>
  <p>Dúvidas: suporte@apinfo.com</p></body></html>"""


# --- Parse e filtros ---------------------------------------------------------------------------


def test_parse_vagas():
    html = _listing(
        _box("90001", "Desenvolvedor <span>Front</span>end React", desc="Linha 1<br>Linha 2"),
        '<div class="box-vagas"><div class="cargo">Sem link</div></div>',
    )
    [v] = client.parse_vagas(html)
    assert v.codigo == "90001"
    assert v.cargo == "Desenvolvedor Frontend React"  # destaque em <span> não quebra a palavra
    assert v.empresa == "ACME Ltda"
    assert v.local == "Home Office - HO"
    assert v.publicada == "23/09/26"
    assert v.descricao == "Linha 1\nLinha 2"
    assert v.link_envio == "https://www.apinfo.com/apinfo/inc/enviecv.cfm?codvaga=90001&pkey=k90001"


def test_next_page_form():
    html = _listing(pagina=1, total=3)
    assert client.next_page_form(html, 2) == [("keyw", "react"), ("tcv", "1"), ("pag", "2")]
    assert client.next_page_form(html, 4) is None
    assert client.next_page_form("<html>sem paginação</html>", 2) is None


def test_keyword_regex():
    assert client.keyword_regex("front-end").search("Desenvolvedor Frontend Sênior")
    assert client.keyword_regex("front end").search("Front-End React")
    assert client.keyword_regex("react native").search("Dev React Native")
    assert not client.keyword_regex("ios").search("Analista de Negócios")


@pytest.mark.parametrize("desc, bloqueia", [
    ("Inglês avançado para reuniões.", True),
    ("Nível de inglês: B2", True),
    ("Inglês técnico para leitura.", False),
    ("Inglês básico é um diferencial.", False),
    ("We are looking for a developer with strong experience in React and the skills to work "
     "with our team. You will build the product and we expect knowledge of the stack.", True),
])
def test_english_block_reason(desc, bloqueia):
    assert (client.english_block_reason(desc) is not None) == bloqueia


def test_local_filter():
    vagas = [
        client.Vaga("1", "Dev Frontend Júnior", "", "São Paulo - SP", "", ""),
        client.Vaga("2", "Dev Frontend", "", "Rio de Janeiro - RJ", "", ""),
        client.Vaga("3", "Dev Frontend", "", "Home Office - HO", "Inglês fluente", ""),
        client.Vaga("4", "Dev Frontend", "", "Home Office - HO", "React", ""),
    ]
    cfg = {"excluir_titulo": ["júnior"], "locais": ["HO", "SP"], "bloquear_ingles": True}
    assert [v.codigo for v in client.local_filter(vagas, cfg)] == ["4"]
    assert len(client.local_filter(vagas, {})) == 4


def test_extract_contact():
    html = CONTACT_HTML.format(email="rh@acme.com.br", assunto="apinfo - 90001 - React")
    assert client.extract_contact(html) == ("rh@acme.com.br", "apinfo - 90001 - React")
    # Sem "Email :" explícito, ignora os e-mails do próprio APinfo
    assert client.extract_contact("<p>fale com suporte@apinfo.com ou vagas@empresa.com</p>")[0] == "vagas@empresa.com"
    assert client.extract_contact("<p>nada aqui</p>") == (None, None)


@pytest.mark.parametrize("status, modo, retentar, esperado", [
    ("enviado", "real", False, True),
    ("capturado", "real", False, False),    # candidatura feita, falta o e-mail real
    ("capturado", "teste", False, True),
    ("falhou", "real", False, True),
    ("falhou", "real", True, False),
    (None, "real", False, False),
])
def test_already_done(status, modo, retentar, esperado):
    if status:
        client.save_record("1", {"status": status})
    assert client.already_done("1", modo, retentar) is esperado


def test_resumo_text_nunca_passa_do_limite_do_telegram():
    resultados = [
        {"codigo": str(i), "cargo": "Dev <Frontend> & Mobile " * 5, "empresa": "ACME", "status": "enviado",
         "email": f"rh{i}@acme.com", "publicada": "23/09/26"}
        for i in range(200)
    ]
    texto = views.resumo_text(resultados, "bloqueou", 3)
    assert len(texto) <= 4096
    assert texto.startswith("<b>[APinfo]</b> <b>200 e-mail(s) enviado(s)</b>")
    assert "… e mais" in texto and texto.endswith("⏭ 3 vaga(s) ficaram para a próxima busca.")
    assert "&lt;Frontend&gt; &amp;" in texto


def test_registro_e_config_da_fonte():
    apinfo = registry.source_of({"source": "apinfo"})
    assert isinstance(apinfo, ApinfoSource)
    assert registry.enabled_sources({"apinfo_jobs": {"enabled": True}}) == [FREELAS99, apinfo]
    assert registry.enabled_sources({"apinfo_jobs": {"enabled": False}, "github_jobs": {"enabled": True}}) == [FREELAS99, GITHUB]
    assert registry.source_for_id("90001") is FREELAS99  # APinfo nunca entra em approvals


# --- Máquina de estados (site falso + relógio controlado) --------------------------------------


class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass


class FakeApinfoSite:
    """Responde como o APinfo: listagem por palavra-chave, formulário e página de contato."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.listings: dict[str, str] = {}
        self.contacts: dict[str, tuple[str, str]] = {}
        self.rate_limited = False

    def request(self, method, url, data=None, headers=None, timeout=None):
        form = dict(parse_qsl(data, encoding=client.ENCODING)) if data else {}
        self.requests.append((method, url, form))
        if self.rate_limited:
            return _Resp("<p>Seu limite de consultas está temporariamente esgotado.</p>")
        if url == client.SEARCH_URL:
            return _Resp(self.listings.get(form.get("keyw"), _listing()))
        if method == "GET" and "enviecv.cfm" in url:
            codigo = url.split("codvaga=")[1].split("&")[0]
            return _Resp(FORM_HTML.format(codigo=codigo))
        if url == client.APPLY_URL:
            assert (form["cpf2"], form["chave3"]) == ("123", "senha")
            email, assunto = self.contacts[form["codvaga"]]
            return _Resp(CONTACT_HTML.format(email=email, assunto=assunto))
        raise AssertionError(f"requisição inesperada: {method} {url}")

    def count(self, url_part: str) -> int:
        return sum(url_part in url for _, url, _ in self.requests)


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(client, "now", lambda: state["t"])

    def advance(seconds: float):
        state["t"] += seconds
    return advance


@pytest.fixture
def site(monkeypatch):
    fake = FakeApinfoSite()
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **kw: fake.request(*a, **kw))
    return fake


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def send(to, subject, body, attachment_path=None, body_html=None, bcc=None):
        calls.append({"to": to, "subject": subject, "body": body, "anexo": attachment_path, "html": body_html, "bcc": bcc})
        return True, f"e-mail enviado pra {to}"
    monkeypatch.setattr(email_sender, "send", send)
    return calls


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("APINFO_CPF", "123")
    monkeypatch.setenv("APINFO_SENHA", "senha")
    monkeypatch.setenv("SMTP_USER", "eu@gmail.com")
    monkeypatch.setenv("EMAIL_FROM_NAME", "Pedro")


def _config(modo="real", **over) -> dict:
    cfg = {
        "enabled": True,
        "modo": modo,
        "intervalo_busca_min": 60,
        "busca": {"palavras_chave": ["react"], "onde": 1, "max_paginas": 1},
        "filtro_local": {"excluir_titulo": ["júnior"]},
        "envio": {"max_por_execucao": 10, "pausa_min": 8, "pausa_max": 20, "pausa_vaga_min": 40, "pausa_vaga_max": 90},
        "email": {"texto": "Olá! Vaga {cargo} ({codigo}) na {empresa}.\n{nome}", "anexo": "data/cv.pdf", "copia_para_mim": True},
    }
    cfg.update(over)
    return {"apinfo_jobs": cfg}


def _duas_vagas(site):
    site.listings["react"] = _listing(
        _box("90001", "Dev React"), _box("90002", "Dev React Sênior", empresa="Beta"), _box("90003", "Dev React Júnior"),
    )
    site.contacts["90001"] = ("rh@acme.com", "apinfo - 90001 - Dev React")
    site.contacts["90002"] = ("jobs@beta.com", "apinfo - 90002 -")  # assunto sem título


def _resumos(telegram) -> list[str]:
    return [p["text"] for p in telegram.of("sendMessage") if p["text"].startswith("<b>[APinfo]</b> <b>")]


def test_rodada_completa_um_passo_por_tick(site, clock, sent, telegram):
    _duas_vagas(site)
    config = _config()
    fonte = ApinfoSource()

    fonte.run_cycle(config)
    assert site.requests == []  # run_cycle só agenda

    fonte.tick(config)  # 1ª requisição: a busca
    assert site.count("list4.cfm") == 1
    fonte.tick(config)  # pausa entre requisições ainda não passou
    assert len(site.requests) == 1

    clock(30)
    fonte.tick(config)  # fim da busca (sem requisição): monta a fila
    assert len(site.requests) == 1

    clock(30)
    fonte.tick(config)  # candidatura 1 (GET formulário + POST CPF) + e-mail
    assert site.count("enviecv.cfm") == 2 and len(sent) == 1
    clock(30)
    fonte.tick(config)  # pausa entre VAGAS (>= 40s) ainda não passou
    assert len(sent) == 1

    clock(100)
    fonte.tick(config)
    assert len(sent) == 2

    assert sent[0] == {
        "to": "rh@acme.com", "subject": "apinfo - 90001 - Dev React", "body": "Olá! Vaga Dev React (90001) na ACME Ltda.\nPedro",
        "anexo": client.attachment_path("data/cv.pdf"), "html": None, "bcc": "eu@gmail.com",
    }
    assert sent[1]["subject"] == "apinfo - 90002 - Dev React Sênior"  # completado pelo bot
    assert client.get_record("90001")["status"] == "enviado"
    assert client.get_record("90002")["email"] == "jobs@beta.com"
    assert client.get_record("90003") is None  # "Júnior" filtrado localmente

    [resumo] = _resumos(telegram)
    assert resumo.startswith("<b>[APinfo]</b> <b>2 e-mail(s) enviado(s)</b>")
    assert "✅ <b>90001</b> Dev React - ACME Ltda · 23/09/26\n    rh@acme.com" in resumo

    # Próxima busca só depois do intervalo (60 min ± 15%), e as vagas já enviadas não repetem.
    fonte.run_cycle(config)
    fonte.tick(config)
    assert site.count("list4.cfm") == 1
    clock(60 * 60 * 1.2)
    fonte.run_cycle(config)
    fonte.tick(config)
    clock(30)
    fonte.tick(config)
    assert site.count("list4.cfm") == 2
    assert len(sent) == 2 and len(_resumos(telegram)) == 1  # nada novo → sem resumo


def test_max_por_execucao_deixa_o_resto_pra_proxima(site, clock, sent, telegram):
    _duas_vagas(site)
    config = _config(envio={"max_por_execucao": 1})
    fonte = ApinfoSource()
    fonte.run_cycle(config)
    for _ in range(4):
        fonte.tick(config)
        clock(200)
    assert len(sent) == 1
    [resumo] = _resumos(telegram)
    assert resumo.endswith("⏭ 1 vaga(s) ficaram para a próxima busca.")


def test_modo_teste_manda_pra_mim_e_depois_o_real_reaproveita_o_contato(site, clock, sent, telegram):
    _duas_vagas(site)
    site.listings["react"] = _listing(_box("90001", "Dev React"))
    fonte = ApinfoSource()
    fonte.run_cycle(_config(modo="teste"))
    for _ in range(3):
        fonte.tick(_config(modo="teste"))
        clock(200)

    assert sent[0]["to"] == "eu@gmail.com"
    assert sent[0]["subject"] == "[TESTE -> rh@acme.com] apinfo - 90001 - Dev React"
    assert sent[0]["bcc"] is None
    assert client.get_record("90001")["status"] == "capturado"
    assert "modo teste" in _resumos(telegram)[0]

    # Virou modo real: manda pra empresa SEM consultar o site de novo (contato já capturado).
    fonte._next_search_at = 0
    antes = site.count("enviecv.cfm")
    fonte.run_cycle(_config())
    for _ in range(3):
        fonte.tick(_config())
        clock(200)
    assert site.count("enviecv.cfm") == antes
    assert sent[-1]["to"] == "rh@acme.com"
    assert client.get_record("90001")["status"] == "enviado"


def test_falha_no_email_guarda_o_contato(site, clock, monkeypatch, telegram):
    _duas_vagas(site)
    site.listings["react"] = _listing(_box("90001", "Dev React"))
    monkeypatch.setattr(email_sender, "send", lambda *a, **kw: (False, "erro SMTP: <auth>"))
    fonte = ApinfoSource()
    fonte.run_cycle(_config())
    for _ in range(3):
        fonte.tick(_config())
        clock(200)
    rec = client.get_record("90001")
    assert rec["status"] == "capturado" and rec["email"] == "rh@acme.com" and rec["erro"] == "erro SMTP: <auth>"
    assert "⚠️ <b>90001</b>" in _resumos(telegram)[0] and "&lt;auth&gt;" in _resumos(telegram)[0]
    assert not client.already_done("90001", "real")  # próxima rodada tenta o e-mail de novo


def test_bloqueio_na_busca_avisa_e_dobra_a_espera(site, clock, sent, telegram):
    site.rate_limited = True
    fonte = ApinfoSource()
    fonte.run_cycle(_config())
    fonte.tick(_config())

    avisos = [p["text"] for p in telegram.of("sendMessage")]
    assert len(avisos) == 1 and avisos[0].startswith("<b>[APinfo]</b> ⏸ O APinfo bloqueou as consultas (1x seguidas)")
    assert fonte._next_search_at - 1000 >= 60 * 60 * 2 * 0.85  # backoff: 2x o intervalo

    clock(60 * 60 * 3)
    fonte.run_cycle(_config())
    fonte.tick(_config())
    assert "(2x seguidas)" in telegram.last("sendMessage")["text"]
    assert fonte._next_search_at - client.now() >= 60 * 60 * 4 * 0.85


def test_bloqueio_no_meio_da_fila_nao_registra_o_resto(site, clock, sent, telegram):
    _duas_vagas(site)
    fonte = ApinfoSource()
    fonte.run_cycle(_config())
    fonte.tick(_config())
    clock(30)
    fonte.tick(_config())  # fila montada
    clock(30)
    fonte.tick(_config())  # vaga 1 ok
    site.rate_limited = True
    clock(200)
    fonte.tick(_config())  # vaga 2 bloqueada

    assert client.get_record("90001")["status"] == "enviado"
    assert client.get_record("90002") is None  # continua "nova" pra próxima busca
    [resumo] = _resumos(telegram)
    assert "⏸ O APinfo informou" in resumo and "⏭ 1 vaga(s)" in resumo
    assert "(1x seguidas)" in telegram.last("sendMessage")["text"]
    assert fonte._fila == [] and fonte._search is None


def test_erro_inesperado_numa_vaga_nao_trava_a_fila(site, clock, sent, monkeypatch):
    _duas_vagas(site)
    fonte = ApinfoSource()
    original = client.candidatar

    def candidatar(api, v, *a, **kw):
        if v.codigo == "90001":
            raise ValueError("html estranho")
        return original(api, v, *a, **kw)
    monkeypatch.setattr(client, "candidatar", candidatar)

    fonte.run_cycle(_config())
    for _ in range(5):
        fonte.tick(_config())
        clock(200)
    assert [c["to"] for c in sent] == ["jobs@beta.com"]


def test_sem_credenciais_avisa_uma_vez_e_nao_busca(site, clock, monkeypatch, telegram):
    monkeypatch.delenv("APINFO_CPF")
    fonte = ApinfoSource()
    for _ in range(3):
        fonte.run_cycle(_config())
        fonte.tick(_config())
    assert site.requests == []
    textos = [p["text"] for p in telegram.of("sendMessage")]
    assert len(textos) == 1 and "APINFO_CPF/APINFO_SENHA" in textos[0]


def test_modo_invalido_nunca_vira_envio_real(site, clock, sent):
    _duas_vagas(site)
    site.listings["react"] = _listing(_box("90001", "Dev React"))
    fonte = ApinfoSource()
    fonte.run_cycle(_config(modo="producao"))
    for _ in range(3):
        fonte.tick(_config(modo="producao"))
        clock(200)
    assert sent[0]["to"] == "eu@gmail.com"


def test_pisos_anti_bloqueio():
    api = client.Apinfo(1, 2, (5, 6))
    assert api.pausa_min == client.PAUSA_MIN_SEGURA and api.pausa_max >= api.pausa_min * 1.5
    assert api.pausa_vaga[0] == client.PAUSA_VAGA_MIN_SEGURA
