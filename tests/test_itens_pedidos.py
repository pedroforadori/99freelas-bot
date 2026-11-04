"""
Itens que o cliente pede pra proposta informar (pedido do usuário, 2026-10-28 — caso real
do Lucca: "Na proposta, informar: experiência, prazo, valor total, custos mensais").
Experiência vem do site de portfólio, sem citar empresa; R$ só pra custo mensal/por uso.
"""
import os
import time

import pytest

from bot import ai_writer, portfolio_context
from bot import proposal as proposal_mod
from tests.conftest import project_99

LUCCA = """Procuro um desenvolvedor experiente para criar uma plataforma web de músicas personalizadas com inteligência artificial.

Funcionalidades principais:
- Formulário para o cliente contar sua história.
- Pagamento automático via Pix e cartão de crédito.

Na proposta, informar:

- Experiência com projetos semelhantes.
- Prazo de desenvolvimento.
- Valor total do projeto.
- Custos mensais estimados de APIs e hospedagem."""

COMUM = "Preciso de um site institucional com 5 páginas e formulário de contato."
SITE = "Pedro Foradori\nDesenvolvedor Web e Mobile. Bradesco: Cartão de crédito. Plataformas com pagamento online."
CONFIG = {
    "proposal": {
        "ia_provider": "gemini", "ia_model": "x", "texto_modo": "ia", "valor_modo": "manual",
        "portfolio": {"url": "https://portfolio.exemplo/", "nomes_a_omitir": ["Bradesco", "Banco Pan"]},
    }
}
TEXTO_COM_CUSTOS = (
    "Olá, tudo bem?\n\nPosso criar a plataforma completa.\n\n"
    "Já desenvolvi plataformas com pagamento online e área do cliente; você pode ver mais no portfólio do meu perfil.\n\n"
    "O valor total e o prazo estão informados nos campos desta proposta. "
    "A hospedagem fica por volta de R$ 50 a R$ 100 por mês, e a geração das músicas custa de R$ 0,50 a R$ 2,00 por música gerada."
)


def test_detecta_os_pedidos():
    assert ai_writer.pede_experiencia(LUCCA) and ai_writer.pede_custos_mensais(LUCCA)
    assert not ai_writer.pede_experiencia(COMUM) and not ai_writer.pede_custos_mensais(COMUM)
    assert not ai_writer.pede_custos_mensais("Preciso reduzir os custos da minha empresa.")


@pytest.mark.parametrize("texto", [
    "A hospedagem fica por volta de R$ 50 a R$ 100 por mês.",
    "Hospedagem: R$ 40 a R$ 80 mensais.",
    "A IA cobra cerca de R$ 1,50 por música gerada.",
    "A hospedagem fica de 30 a 70 reais por mês.",
])
def test_custo_mensal_passa_com_a_flag(texto):
    assert ai_writer.check_text_safety(texto, custos_mensais=True) is None
    assert ai_writer.check_text_safety(texto) == "menciona valor (R$)"


@pytest.mark.parametrize("texto", [
    "O valor total é R$ 1.850.",
    "O valor total é R$ 1.850 e a hospedagem R$ 50 por mês.",
    "O projeto sai por R$ 1.850, e a hospedagem custa R$ 50 por mês.",
    "O projeto completo fica em 1850 reais.",
])
def test_valor_total_continua_barrado(texto):
    assert ai_writer.check_text_safety(texto, custos_mensais=True)


def test_empresa_do_portfolio_barrada():
    assert "Bradesco" in ai_writer.check_text_safety("Já fiz um app pro Bradesco.", nomes_a_omitir=["Bradesco"])
    assert ai_writer.check_text_safety("Fiz um app de banco.", nomes_a_omitir=["Bradesco"]) is None


@pytest.fixture
def site(monkeypatch):
    estado = {"texto": SITE, "erro": None, "chamadas": 0}

    def read_link(url, config):
        estado["chamadas"] += 1
        return (None, estado["erro"]) if estado["erro"] else (estado["texto"], None)

    monkeypatch.setattr(portfolio_context.attachments, "read_link", read_link)
    return estado


def _trabalho_local(nome, texto):
    pasta = os.path.join(portfolio_context.PORTFOLIO_DIR, nome)
    os.makedirs(pasta, exist_ok=True)
    with open(os.path.join(pasta, "texto.txt"), "w", encoding="utf-8") as f:
        f.write(texto)


def test_material_usa_site_e_trabalhos_locais_com_cache(site):
    _trabalho_local("Obras Online", "Plataforma de gestão de vendas")
    material = portfolio_context.material_portfolio(CONFIG)
    assert "Plataformas com pagamento online" in material and "Plataforma de gestão de vendas" in material
    portfolio_context.material_portfolio(CONFIG)
    assert site["chamadas"] == 1  # 2ª leitura veio do cache


def test_site_fora_do_ar_usa_cache_antigo(site):
    portfolio_context.material_portfolio(CONFIG)
    cache = portfolio_context._ler_cache()
    cache["lido_em"] = time.time() - 2 * portfolio_context.CACHE_SEGUNDOS
    portfolio_context._gravar_cache(cache)
    site["erro"] = "HTTP 503"
    assert "Plataformas com pagamento online" in portfolio_context.material_portfolio(CONFIG)


def test_sem_site_e_sem_trabalhos(site):
    site["erro"] = "HTTP 503"
    assert portfolio_context.material_portfolio(CONFIG) is None


@pytest.fixture
def ia(monkeypatch, site):
    chamadas = []

    def fake(user_prompt, model, system_prompt=None):
        chamadas.append((user_prompt, system_prompt))
        return chamadas_resposta[0]

    chamadas_resposta = [TEXTO_COM_CUSTOS]
    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (fake, "x"))
    return chamadas, chamadas_resposta


def test_caso_lucca_texto_com_custos_e_experiencia(ia):
    chamadas, _ = ia
    p = proposal_mod.build_proposal(project_99(), CONFIG, full_description=LUCCA)
    assert not p["texto_ia_falhou"] and p["texto_variante"] != "template"
    assert "R$ 50 a R$ 100 por mês" in p["texto"]
    user_prompt, system_prompt = chamadas[0]
    assert "Material do meu portfólio" in user_prompt and "Plataformas com pagamento online" in user_prompt
    assert "Bradesco" in user_prompt  # na lista de nomes proibidos
    assert "Na proposta, informar" in system_prompt


def test_projeto_comum_nao_leva_portfolio_nem_libera_rs(ia):
    chamadas, resposta = ia
    resposta[0] = TEXTO_COM_CUSTOS
    p = proposal_mod.build_proposal(project_99(), CONFIG, full_description=COMUM)
    assert p["texto_ia_falhou"]  # R$ sem o cliente pedir custos → template
    assert "Material do meu portfólio" not in chamadas[0][0]


def test_ia_citando_empresa_cai_no_template(ia):
    _, resposta = ia
    resposta[0] = "Olá, tudo bem?\n\nJá fiz um sistema parecido para o Bradesco."
    p = proposal_mod.build_proposal(project_99(), CONFIG, full_description=LUCCA)
    assert p["texto_ia_falhou"] and "Bradesco" not in p["texto"]


# --- texto barrado pela checagem: tenta de novo (caso real 2026-11-02) --------------------


@pytest.mark.parametrize("descricao", [
    "Gostaria de saber o valor da implantação, da mensalidade, o prazo de entrega.",
    "Informe o valor mensal da manutenção.",
    "Qual o custo de manutenção depois de pronto?",
])
def test_detecta_mensalidade_como_custo_mensal(descricao):
    assert ai_writer.pede_custos_mensais(descricao)


def test_nao_confunde_orcamento_com_custo_mensal():
    assert not ai_writer.pede_custos_mensais("Envie sua estimativa de prazo e orçamento.")
    assert not ai_writer.pede_custos_mensais("Apresentem orçamento discriminado.")


def test_texto_barrado_tenta_de_novo_com_o_motivo(monkeypatch):
    prompts, respostas = [], iter([
        "Olá, tudo bem?\n\nO Make.com fica em torno de R$ 50 a R$ 100 por mês.",
        "Olá, tudo bem?\n\nPosso automatizar as cobranças.",
    ])

    def fake_gemini(user_prompt, model, system_prompt=None):
        prompts.append(user_prompt)
        return next(respostas)

    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (fake_gemini, "x"))
    texto = ai_writer.generate_proposal_text(project_99(), "Envie prazo e orçamento.", CONFIG, variante="consultivo")
    assert texto == "Olá, tudo bem?\n\nPosso automatizar as cobranças."
    assert len(prompts) == 2
    assert "ATENÇÃO" not in prompts[0]
    assert "recusada porque menciona valor (R$)" in prompts[1]


def test_texto_barrado_em_todas_as_tentativas_cai_pro_template(monkeypatch):
    chamadas = []

    def fake_gemini(user_prompt, model, system_prompt=None):
        chamadas.append(1)
        return "Olá! Custa R$ 500."

    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (fake_gemini, "x"))
    assert ai_writer.generate_proposal_text(project_99(), "Descrição", CONFIG, variante="consultivo") is None
    assert len(chamadas) == ai_writer._TENTATIVAS_TEXTO


def test_ia_fora_do_ar_nao_tenta_de_novo(monkeypatch):
    chamadas = []

    def fake_gemini(user_prompt, model, system_prompt=None):
        chamadas.append(1)
        return None

    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (fake_gemini, "x"))
    assert ai_writer.generate_proposal_text(project_99(), "Descrição", CONFIG, variante="consultivo") is None
    assert len(chamadas) == 1
