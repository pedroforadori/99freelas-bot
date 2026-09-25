"""
Registro das fontes de vagas — o ÚNICO lugar que lista quais fontes existem. Nova fonte:
subclasse de JobSource (bot/sources/base.py) + uma linha em ALL_SOURCES.

Fica fora de bot/sources/__init__.py de propósito: submitter.py/messages.py importam
bot.sources.freelas99.views, e se o __init__ do pacote importasse as fontes, isso puxaria
freelas99/source.py → submitter.py de volta (import circular).
"""
from bot.sources.apinfo.source import ApinfoSource
from bot.sources.base import JobSource
from bot.sources.freelas99.source import Freelas99Source
from bot.sources.github.source import GitHubSource

# A primeira é a padrão: entradas antigas de data/pending_approvals.json não têm "source"
# (anteriores ao GitHub) e são todas do 99Freelas.
ALL_SOURCES: list[JobSource] = [Freelas99Source(), GitHubSource(), ApinfoSource()]
DEFAULT_SOURCE = ALL_SOURCES[0]

_BY_NAME = {s.name: s for s in ALL_SOURCES}
assert len(_BY_NAME) == len(ALL_SOURCES), "nome de fonte duplicado em ALL_SOURCES"


def enabled_sources(config: dict) -> list[JobSource]:
    return [s for s in ALL_SOURCES if s.is_enabled(config)]


def source_of(project: dict) -> JobSource:
    """Fonte dona de um projeto/vaga (campo "source", gravado por JobSource.queue_for_approval)."""
    return _BY_NAME.get(project.get("source") or "", DEFAULT_SOURCE)


def source_for_id(project_id: str) -> JobSource:
    """Fonte pelo formato do id — quando não há entrada em approvals pra ler o "source"."""
    return next((s for s in ALL_SOURCES if s.owns_id(project_id)), DEFAULT_SOURCE)
