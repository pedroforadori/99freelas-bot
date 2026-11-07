"""Leitura do saldo de conexões no texto do /dashboard (connections.parse_text)."""

from bot import connections

# Texto real do bloco "Minhas conexões" (2026-11-07), com 1 conexão do plano — o singular
# "1 conexão restante" quebrava a leitura inteira antes.
TEXTO_SINGULAR = """
Conexões disponíveis: 128
1 conexão
restante de um total de 240
referentes ao seu plano (Premium).
Essas conexões serão renovadas no dia 17/11/2026.
127 conexões
não expiráveis.
Comprar conexões
"""

TEXTO_PLURAL = """
Conexões disponíveis: 246
240 conexões restantes de um total de 240 referentes ao seu plano (Premium).
Essas conexões serão renovadas no dia 17/10/2026.
6 conexões não expiráveis.
"""


def test_le_conexoes_disponiveis_com_uma_conexao_no_plano():
    assert connections.parse_text(TEXTO_SINGULAR) == {
        "disponiveis": 128,
        "plano_restantes": 1,
        "plano_total": 240,
        "nao_expiraveis": 127,
        "renovacao": "17/11/2026",
    }


def test_le_conexoes_no_plural():
    assert connections.parse_text(TEXTO_PLURAL) == {
        "disponiveis": 246,
        "plano_restantes": 240,
        "plano_total": 240,
        "nao_expiraveis": 6,
        "renovacao": "17/10/2026",
    }


def test_sem_conexoes_disponiveis_nao_le():
    assert connections.parse_text("240 conexões restantes de um total de 240") is None


def test_so_conexoes_disponiveis_basta():
    data = connections.parse_text("Conexões disponíveis: 5")
    assert data["disponiveis"] == 5 and data["plano_total"] is None
