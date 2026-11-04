"""
"Projeto" que é outro freelancer anunciando o próprio serviço (pedido do usuário,
2026-10-28): ignorado na varredura, sem proposta. Regras no código (filter.py) e, nos que
passarem, a IA (ai_writer.classify_project).
"""
import pytest

from bot import ai_writer, connections, scraper, storage, submitter
from bot import site_selectors as sel
from bot.filter import anuncio_de_freelancer, is_match
from tests.conftest import project_99

PID = "785400"
TITULO_NICOLE = "Criadora de Sites | Desenvolvedora Web"
DESCRICAO_NICOLE = """Olá! Me chamo Nicole Mendes e trabalho com desenvolvimento web e assistência administrativa. Tenho formação técnica em Informática e Administração e ajudo profissionais e empresas tanto na presença digital quanto na organização das tarefas do dia a dia.

Desenvolvimento web
• Criação de sites institucionais e landing pages;
• Desenvolvimento de lojas virtuais e sistemas web;
• Layouts responsivos para celulares, tablets e computadores;

Trabalho com HTML, CSS, JavaScript, TypeScript, React, Next.js e Node.js.

Valorizo a organização, a comunicação clara, a atenção aos detalhes e o cuidado com as informações de cada cliente.

Precisa de um site ou de apoio para organizar as demandas do seu negócio? Me conte o que você precisa e vamos conversar!"""

COM_IA = {"proposal": {"ia_provider": "gemini", "ia_model": "x"}, "anuncios_de_freelancer": {"usar_ia": True}}


def test_caso_real_da_nicole():
    motivo = anuncio_de_freelancer(TITULO_NICOLE, DESCRICAO_NICOLE)
    assert motivo and "título de profissão" in motivo and "apresentação pessoal" in motivo


def test_so_o_trecho_do_card_ja_basta():
    assert anuncio_de_freelancer(TITULO_NICOLE, DESCRICAO_NICOLE[:150])


@pytest.mark.parametrize("titulo, descricao", [
    ("Site institucional para clínica", "Preciso de um site institucional para minha clínica, com 5 páginas e formulário."),
    ("Desenvolvedor React para dashboard", "Procuro desenvolvedor React para montar um painel de vendas. Trabalho com vendas há anos."),
    ("Sistema de agendamento", "Tenho uma barbearia e quero um sistema de agendamento. Trabalho com cortes e barba."),
    ("Programador para automação", "Sou dono de uma loja de peças e quero automatizar minhas planilhas."),
    ("Desenvolvedor para app de entregas", "Trabalho com logística e meus motoristas usam papel. Vamos conversar."),
])
def test_pedidos_normais_passam(titulo, descricao):
    assert anuncio_de_freelancer(titulo, descricao) is None


def test_is_match_recusa_anuncio():
    ok, motivo = is_match(project_99(title=TITULO_NICOLE, description=DESCRICAO_NICOLE[:300]), {})
    assert not ok and motivo.startswith("parece anúncio de freelancer")


def test_is_match_com_filtro_desligado():
    config = {"anuncios_de_freelancer": {"ignorar": False}}
    assert is_match(project_99(title=TITULO_NICOLE, description=DESCRICAO_NICOLE), config)[0]


# --- descrição completa + IA, no prepare_proposal ------------------------------------------


class _El:
    def __init__(self, texto):
        self.texto = texto

    def inner_text(self):
        return self.texto


class FakePage:
    def __init__(self, descricao):
        self.descricao = descricao

    def goto(self, *a, **k):
        pass

    def query_selector(self, selector):
        return _El(self.descricao) if selector == sel.PROJECT_PAGE_DESCRIPTION else None

    def expect_navigation(self, **k):
        pytest.fail("não devia clicar em 'Enviar proposta'")


@pytest.fixture
def ia(monkeypatch):
    estado = {"resposta": None, "chamadas": 0}

    def classifica(project, full_description, config):
        estado["chamadas"] += 1
        anuncio = estado["resposta"]
        return None if anuncio is None else {"anuncio": anuncio, "programacao": True}

    monkeypatch.setattr(ai_writer, "classify_project", classifica)
    return estado


def test_regras_na_descricao_completa_sem_chamar_ia(ia):
    projeto = project_99(title="Serviços web", description="Olá!")
    proposal, motivo = submitter.prepare_proposal(FakePage(DESCRICAO_NICOLE), projeto, COM_IA, check_exclude=True)
    assert proposal is None and motivo.startswith(submitter.ANUNCIO_DE_FREELANCER)
    assert ia["chamadas"] == 0


def test_ia_pega_o_que_as_regras_deixaram_passar(ia):
    ia["resposta"] = True
    _, motivo = submitter.prepare_proposal(FakePage("Faço sites bonitos e baratos."), project_99(), COM_IA, check_exclude=True)
    assert motivo == f"{submitter.ANUNCIO_DE_FREELANCER}: a IA classificou como anúncio"


@pytest.mark.parametrize("resposta", [False, None])  # pedido, ou IA fora do ar: segue normal
def test_ia_diz_pedido_ou_falha_segue(ia, resposta):
    ia["resposta"] = resposta
    assert submitter._triagem(project_99(), "Preciso de um site.", COM_IA) is None
    assert ia["chamadas"] == 1


def test_ia_desligada_nao_e_chamada(ia):
    ia["resposta"] = True
    assert submitter._triagem(project_99(), "Faço sites.", {}) is None
    assert ia["chamadas"] == 0


@pytest.mark.parametrize("raw, esperado", [
    ('{"tipo": "anuncio", "programacao": true}', {"anuncio": True, "programacao": True}),
    ('```json\n{"tipo": "pedido", "programacao": false}\n```', {"anuncio": False, "programacao": False}),
    ('{"tipo": "talvez", "programacao": true}', None),
    ('{"tipo": "pedido", "programacao": "sim"}', None),
    ('{"tipo": "pedido"}', None),
    ("não sei", None),
    (None, None),
])
def test_classify_project_le_o_json(monkeypatch, raw, esperado):
    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (lambda *a, **k: raw, "x"))
    assert ai_writer.classify_project(project_99(), "desc", COM_IA) == esperado


def test_varredura_registra_como_ignorado(api, telegram, monkeypatch):
    monkeypatch.setattr(connections, "refresh", lambda page: None)
    monkeypatch.setattr(scraper, "fetch_open_projects", lambda page: [project_99()])
    api.patch_submitter("prepare_proposal", lambda *a, **k: (None, f"{submitter.ANUNCIO_DE_FREELANCER}: x"))
    api.run_cycle({"proposal": {"ia_provider": "gemini", "ia_model": "x"}})
    assert storage.get_application(PID)["status"] == "skipped_duplicate"
    assert not any("aprovar" in p["text"] for p in telegram.of("sendMessage"))
