"""
Mensagens do Telegram das vagas do APinfo — sem botões (envio automático, ver source.py).
Mesmo resumo por rodada que o bot standalone mandava, agora com a tag de origem.
"""
from bot import telegram_api
from bot.telegram_api import esc

TAG = "<b>[APinfo]</b>"

_ICONE = {"enviado": "✅", "capturado": "🧪", "falhou": "⚠️"}


def _linha(r: dict) -> str:
    ic = "⚠️" if r.get("erro") else _ICONE.get(r.get("status"), "•")
    linha = f"{ic} <b>{esc(r['codigo'])}</b> {esc(r.get('cargo', ''))} - {esc(r.get('empresa', ''))}"
    if r.get("publicada"):
        linha += f" · {esc(r['publicada'])}"
    if r.get("email"):
        linha += f"\n    {esc(r['email'])}"
    if r.get("erro"):
        linha += f"\n    <i>{esc(r['erro'][:200])}</i>"
    return linha


def resumo_text(resultados: list[dict], parada: str | None, restantes: int, modo: str = "real") -> str:
    enviados = sum(r.get("status") == "enviado" for r in resultados)
    titulo = f"{TAG} <b>{enviados} e-mail(s) enviado(s)</b>"
    if modo == "teste":
        titulo += " · modo teste (e-mails foram pra você)"
    elif modo == "sem_email":
        titulo += " · modo sem_email (só capturou o contato)"
    rodape = []
    if parada:
        rodape.append(f"⏸ {esc(parada)}")
    if restantes > 0:
        rodape.append(f"⏭ {restantes} vaga(s) ficaram para a próxima busca.")

    # Corta linhas inteiras (nunca no meio de uma tag HTML) se passar do limite do Telegram.
    linhas = [_linha(r) for r in resultados]
    while linhas and len("\n".join([titulo, *linhas, *rodape])) > telegram_api.MSG_LIMIT - 50:
        linhas.pop()
    omitidas = len(resultados) - len(linhas)
    if omitidas:
        linhas.append(f"… e mais {omitidas} (veja data/apinfo_jobs.json)")
    return "\n".join([titulo, *linhas, *rodape])


def send_resumo(resultados: list[dict], parada: str | None, restantes: int, modo: str) -> None:
    telegram_api.send_message(resumo_text(resultados, parada, restantes, modo))


def notify_blocked(bloqueios: int, hora: str) -> None:
    telegram_api.send_message(
        f"{TAG} ⏸ O APinfo bloqueou as consultas ({bloqueios}x seguidas). Nova tentativa às {hora}."
    )


def notify_missing_credentials() -> None:
    telegram_api.send_message(
        f"{TAG} ⚠️ apinfo_jobs está ligado, mas APINFO_CPF/APINFO_SENHA não estão no .env — "
        "nenhuma busca vai rodar até configurar e reiniciar o bot."
    )
