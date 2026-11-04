"""
Projeto fora da área — design, vetor, vídeo, protótipo sem programar (pedido do usuário,
2026-10-29 — caso real 794898, vetorização no Illustrator/Inkscape, recebeu proposta).
Regras no código (filter.fora_da_area) e, nos que passarem, a IA (ai_writer.classify_project).
"""
import pytest

from bot import ai_writer, connections, scraper, storage, submitter
from bot.filter import fora_da_area, is_match
from tests.conftest import project_99
from tests.test_anuncio_freelancer import PID, FakePage

TITULO_VETOR = "Vectorizacao em ilustrator + inscape"
DESCRICAO_VETOR = """### Procuro profissional para vetorização de imagem – Illustrator / Inkscape

Olá!

Estou procurando um profissional com experiência em **Adobe Illustrator e/ou Inkscape** para vetorizar uma ilustração que será utilizada em um projeto de marchetaria em madeira.

A imagem já está pronta em PNG, e o trabalho consiste em transformá-la em um arquivo vetorial editável, mantendo fielmente o desenho original.

**Detalhes do projeto:**
- Dimensões finais: 70 × 50 cm.
- Vetorização detalhada, com separação dos elementos em peças individuais (pétalas, folhas, galhos etc.).
- Organização das peças por cores, utilizando aproximadamente 6 ou 7 tonalidades.
- Simplificação de detalhes muito pequenos, considerando que as peças serão cortadas a laser em lâminas de madeira, preferencialmente com largura mínima de 3 mm.
- Arquivos organizados em camadas ou grupos, permitindo editar cada peça individualmente.
- Entrega em formatos editáveis SVG e AI (quando disponível), além de PDF vetorial.

**Não preciso de criação ou alteração do desenho**, apenas de uma vetorização bem executada, com atenção aos detalhes e à viabilidade do corte.

Tenho a imagem de referência disponível para enviar.

Gostaria de receber orçamentos, prazos e, se possível, exemplos de trabalhos semelhantes.

Obrigado!"""

COM_IA = {"proposal": {"ia_provider": "gemini", "ia_model": "x"}, "fora_da_area": {"usar_ia": True}}


def test_caso_real_da_vetorizacao():
    motivo = fora_da_area(TITULO_VETOR, DESCRICAO_VETOR)
    assert motivo and "illustrator" in motivo and "inkscape" in motivo


def test_so_o_card_ja_basta():
    ok, motivo = is_match(project_99(title=TITULO_VETOR, description=DESCRICAO_VETOR[:200]), {})
    assert not ok and motivo.startswith("fora da área")


@pytest.mark.parametrize("titulo, descricao", [
    # Material que o cliente já tem, pra virar site: decide a IA, nunca a regra.
    ("Site para minha marca", "Tenho um logotipo pronto e quero desenvolver meu site."),
    ("Layout pronto", "Tenho o layout no Photoshop e preciso transformar em site WordPress."),
    ("Protótipo de app", "Preciso de um protótipo no Figma do meu app."),
    ("Sistema de agendamento", "Quero um sistema de agendamento para minha barbearia."),
])
def test_com_termo_de_desenvolvimento_a_regra_nao_exclui(titulo, descricao):
    assert fora_da_area(titulo, descricao) is None


def test_filtro_desligado():
    config = {"fora_da_area": {"ignorar": False}}
    assert is_match(project_99(title=TITULO_VETOR, description=DESCRICAO_VETOR), config)[0]


# --- descrição completa + IA, no prepare_proposal ------------------------------------------


@pytest.fixture
def ia(monkeypatch):
    estado = {"resposta": None, "chamadas": 0}

    def classifica(project, full_description, config):
        estado["chamadas"] += 1
        return estado["resposta"]

    monkeypatch.setattr(ai_writer, "classify_project", classifica)
    return estado


def test_regras_na_descricao_completa_sem_chamar_ia(ia):
    projeto = project_99(title="Arte para marchetaria", description="Olá!")
    proposal, motivo = submitter.prepare_proposal(FakePage(DESCRICAO_VETOR), projeto, COM_IA, check_exclude=True)
    assert proposal is None and motivo.startswith(submitter.FORA_DA_AREA)
    assert ia["chamadas"] == 0


def test_ia_diz_sem_programacao(ia):
    ia["resposta"] = {"anuncio": False, "programacao": False}
    _, motivo = submitter.prepare_proposal(
        FakePage("Preciso de um protótipo no Figma do meu app."), project_99(), COM_IA, check_exclude=True
    )
    assert motivo == f"{submitter.FORA_DA_AREA}: a IA classificou como trabalho sem programação"


@pytest.mark.parametrize("resposta", [{"anuncio": False, "programacao": True}, None])  # IA fora do ar: segue
def test_com_programacao_ou_ia_falhando_segue(ia, resposta):
    ia["resposta"] = resposta
    assert submitter._triagem(project_99(), "Tenho o logotipo e quero o site.", COM_IA) is None


def test_uma_chamada_so_para_as_duas_checagens(ia):
    config = {**COM_IA, "anuncios_de_freelancer": {"usar_ia": True}}
    ia["resposta"] = {"anuncio": True, "programacao": False}
    motivo = submitter._triagem(project_99(), "Faço sites bonitos.", config)
    assert motivo.startswith(submitter.ANUNCIO_DE_FREELANCER) and ia["chamadas"] == 1


def test_so_a_ia_de_anuncio_ligada_ignora_programacao(ia):
    config = {"proposal": COM_IA["proposal"], "anuncios_de_freelancer": {"usar_ia": True}}
    ia["resposta"] = {"anuncio": False, "programacao": False}
    assert submitter._triagem(project_99(), "Preciso de um protótipo.", config) is None


def test_varredura_registra_como_ignorado(api, telegram, monkeypatch):
    monkeypatch.setattr(connections, "refresh", lambda page: None)
    monkeypatch.setattr(scraper, "fetch_open_projects", lambda page: [project_99()])
    api.patch_submitter("prepare_proposal", lambda *a, **k: (None, f"{submitter.FORA_DA_AREA}: x"))
    api.run_cycle({"proposal": {"ia_provider": "gemini", "ia_model": "x"}})
    assert storage.get_application(PID)["status"] == "skipped_duplicate"
    assert not any("aprovar" in p["text"] for p in telegram.of("sendMessage"))
