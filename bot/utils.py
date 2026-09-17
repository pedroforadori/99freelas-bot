import calendar
import re
import time
from datetime import date


def parse_currency(raw: str) -> float | None:
    """Extrai um número de uma string tipo 'R$ 1.200,00' -> 1200.00"""
    if not raw:
        return None
    digits = re.sub(r"[^\d,]", "", raw).replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


def format_currency_br(value: float) -> str:
    """Formata um float pro formato esperado por campos de valor do site: '1234,56' (vírgula decimal)."""
    return f"{value:.2f}".replace(".", ",")


def daily_quota(monthly_quota: int) -> int:
    """
    Divide a cota mensal de propostas pelos dias do mês corrente, sem arredondar pra cima.
    Vive aqui (não em main.py) pra notifier.py poder importar sem criar ciclo de import
    (main.py já importa notifier.py).
    """
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return monthly_quota // days_in_month


_RELATIVE_TIME_UNIT_MINUTES = {
    "segundo": 1 / 60,
    "minuto": 1,
    "hora": 60,
    "dia": 60 * 24,
    "semana": 60 * 24 * 7,
    "mes": 60 * 24 * 30,
}
# Aceita tanto a palavra por extenso quanto abreviações comuns (ex: "min", "h").
_RELATIVE_TIME_PATTERN = re.compile(
    r"(\d+)\s*(segundos?|seg|minutos?|min|horas?|h|dias?|semanas?|sem|m[eê]ses?|m[eê]s|d)\b",
    re.IGNORECASE,
)


def _canonical_unit(raw_unit: str) -> str:
    unit = raw_unit.lower().replace("ê", "e")
    if unit.startswith("seg"):
        return "segundo"
    if unit.startswith("min"):
        return "minuto"
    if unit.startswith("sem"):
        return "semana"
    if unit.startswith("mes"):
        return "mes"
    if unit.startswith("h"):
        return "hora"
    return "dia"  # "dia(s)" ou abreviação "d"


def minutes_since_epoch_ms(epoch_ms) -> float | None:
    """
    Minutos decorridos desde um timestamp em milissegundos (ex: atributo cp-datetime
    do 99Freelas: <b class="datetime" cp-datetime="1789577458000">21 minutos atrás</b>).
    Fonte preferida sobre parse_relative_time_minutes por não depender de interpretar texto.
    """
    try:
        epoch_ms = float(epoch_ms)
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time() * 1000 - epoch_ms) / 60000)


def parse_relative_time_minutes(raw: str) -> float | None:
    """
    Converte um texto de tempo relativo em pt-BR (ex: 'há 45 minutos', 'há 2h',
    'agora mesmo') em minutos decorridos desde a publicação.

    Retorna None se não reconhecer o formato — nesse caso o filtro de idade do projeto
    (bot/filter.py) não bloqueia, trata como "idade desconhecida" em vez de rejeitar.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if "agora" in text or "instante" in text:
        return 0.0
    match = _RELATIVE_TIME_PATTERN.search(text)
    if not match:
        return None
    value = int(match.group(1))
    unit = _canonical_unit(match.group(2))
    return value * _RELATIVE_TIME_UNIT_MINUTES.get(unit, 0)
