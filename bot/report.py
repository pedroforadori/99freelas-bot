"""
Relatório de conversão por estilo de texto da proposta (`python bot/report.py`).

Cada proposta enviada grava em data/applied_jobs.json o estilo de texto sorteado
("texto_variante", ver proposal._sortear_variante_texto); o resultado ("respondeu" |
"fechou") é marcado pelo usuário colando o link do projeto no chat do bot e clicando
"💬 Respondeu"/"🏆 Fechou" (ver notifier._handle_link_action). Este script cruza os dois
pra mostrar qual estilo está convertendo melhor. Só leitura — não altera nada.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # permite `python bot/report.py`

from bot.proposal import TEXTO_VARIANTE_LABELS
from bot.storage import list_by_status

_SEM_REGISTRO = "(enviadas antes do sorteio)"


def main() -> None:
    enviadas = list_by_status("sent")
    if not enviadas:
        print("Nenhuma proposta enviada ainda.")
        return

    por_estilo = defaultdict(lambda: {"enviadas": 0, "respondeu": 0, "fechou": 0})
    for rec in enviadas.values():
        estilo = rec.get("texto_variante") or _SEM_REGISTRO
        linha = por_estilo[estilo]
        linha["enviadas"] += 1
        # "fechou" também conta como resposta (o cliente respondeu antes de fechar).
        if rec.get("resultado") in ("respondeu", "fechou"):
            linha["respondeu"] += 1
        if rec.get("resultado") == "fechou":
            linha["fechou"] += 1

    print(f"{'Estilo':<38} {'Enviadas':>8} {'Resp.':>6} {'% resp.':>8} {'Fech.':>6} {'% fech.':>8}")
    print("-" * 80)
    ordenado = sorted(por_estilo.items(), key=lambda kv: (kv[0] == _SEM_REGISTRO, -kv[1]["enviadas"]))
    for estilo, linha in ordenado:
        nome = TEXTO_VARIANTE_LABELS.get(estilo, estilo)
        n = linha["enviadas"]
        print(
            f"{nome:<38} {n:>8} {linha['respondeu']:>6} {linha['respondeu'] / n:>8.0%} "
            f"{linha['fechou']:>6} {linha['fechou'] / n:>8.0%}"
        )
    print("-" * 80)
    print("Com poucos envios por estilo (< ~20), a diferença entre as taxas ainda é ruído.")


if __name__ == "__main__":
    main()
