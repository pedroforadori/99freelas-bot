"""
Fonte APinfo: busca vagas, se candidata (CPF/senha → e-mail/assunto da vaga) e manda o
e-mail com o currículo SOZINHA — sem o portão de aprovação do Telegram (decisão do
usuário: mesmo comportamento do bot standalone que existia antes). O Telegram recebe só o
resumo de cada rodada e o aviso de bloqueio.

O APinfo limita consultas por IP, então o bot standalone dormia 8–20s entre requisições e
40–90s entre candidaturas. Aqui não dá pra dormir: é um processo só, e isso travaria o
polling de aprovações das outras fontes por até ~15min. Por isso a fonte é uma máquina de
estados que dá NO MÁXIMO um passo por tick (ritmo APPROVAL_POLL_INTERVAL_SECONDS), e só
quando a pausa sorteada já passou (client.Apinfo.ready):
  run_cycle → agenda uma busca nova (a cada apinfo_jobs.intervalo_busca_min, com backoff)
  tick      → avança a busca (1 requisição) OU candidata-se a 1 vaga da fila
            → fila vazia: manda o resumo e agenda a próxima busca
"""
import os
import random
from datetime import datetime, timedelta

import requests

from bot.logger_setup import get_logger
from bot.sources.apinfo import client, views
from bot.sources.base import JobSource

log = get_logger(__name__)

MODOS = ("real", "teste", "sem_email")


def _cfg(config: dict) -> dict:
    return config.get("apinfo_jobs") or {}


def _credenciais() -> tuple[str, str] | None:
    cpf, senha = os.environ.get("APINFO_CPF"), os.environ.get("APINFO_SENHA")
    return (cpf, senha) if cpf and senha else None


class ApinfoSource(JobSource):
    name = "apinfo"
    tag = views.TAG

    def __init__(self):
        self._api: client.Apinfo | None = None
        self._search = None                     # gerador de client.search_steps em andamento
        self._fila: list[client.Vaga] = []      # vagas a candidatar nesta rodada
        self._restantes = 0                     # passaram do max_por_execucao, ficam pra próxima
        self._resultados: list[dict] = []
        self._bloqueios = 0
        self._next_search_at = 0.0              # client.now(); 0 = busca já no primeiro ciclo
        self._avisou_credenciais = False

    def is_enabled(self, config: dict) -> bool:
        return bool(_cfg(config).get("enabled"))

    def _modo(self, config: dict) -> str:
        modo = _cfg(config).get("modo") or "real"
        return modo if modo in MODOS else "teste"  # valor inválido nunca vira envio real

    def _get_api(self, config: dict) -> client.Apinfo:
        if self._api is None:
            envio = _cfg(config).get("envio") or {}
            self._api = client.Apinfo(
                envio.get("pausa_min", 8), envio.get("pausa_max", 20),
                (envio.get("pausa_vaga_min", 40), envio.get("pausa_vaga_max", 90)),
            )
        return self._api

    # --- ciclo -------------------------------------------------------------------------------

    def run_cycle(self, config: dict) -> None:
        """Só agenda a busca — nenhuma requisição aqui (ver docstring do módulo)."""
        if self._search is not None or self._fila or client.now() < self._next_search_at:
            return
        if _credenciais() is None:
            if not self._avisou_credenciais:
                log.warning("apinfo_jobs ligado sem APINFO_CPF/APINFO_SENHA no .env — APinfo parado.")
                views.notify_missing_credentials()
                self._avisou_credenciais = True
            return
        log.info("APinfo: iniciando busca (modo %s)", self._modo(config))
        self._search = client.search_steps(self._get_api(config), _cfg(config).get("busca") or {})

    def tick(self, config: dict) -> None:
        try:
            self._step(config)
        except client.RateLimited as e:
            log.warning("APinfo: %s", e)
            # Vagas ainda não processadas não foram registradas: a próxima busca acha de novo.
            self._restantes += len(self._fila)
            self._search, self._fila = None, []
            self._bloqueios += 1
            self._finish_round(config, parada=str(e))

    def _step(self, config: dict) -> None:
        api = self._get_api(config)
        cfg = _cfg(config)

        if self._search is not None:
            if not api.ready("req"):
                return
            try:
                next(self._search)
                return
            except StopIteration as fim:
                vagas = fim.value or []
            except requests.RequestException as e:
                log.warning("APinfo: erro na busca: %s", e)
                self._search = None
                self._finish_round(config, parada=None)
                return
            self._search = None
            modo, retentar = self._modo(config), bool(cfg.get("retentar_falhas"))
            vagas = client.local_filter(vagas, cfg.get("filtro_local") or {})
            pendentes = [v for v in vagas if not client.already_done(v.codigo, modo, retentar)]
            limite = (cfg.get("envio") or {}).get("max_por_execucao", 10)
            self._fila, self._restantes = pendentes[:limite], max(len(pendentes) - limite, 0)
            log.info("APinfo: %d vaga(s) após filtros, %d nova(s).", len(vagas), len(pendentes))
            if not self._fila:
                self._finish_round(config, parada=None)
            return

        if not self._fila:
            return
        vaga = self._fila[0]
        vai_consultar = not (client.get_record(vaga.codigo) or {}).get("email")
        if not api.ready("vaga" if vai_consultar else "req"):
            return
        cpf, senha = _credenciais() or ("", "")
        try:
            rec = client.candidatar(api, vaga, cpf, senha, cfg.get("email") or {}, self._modo(config))
        except client.RateLimited:
            raise
        except Exception as e:
            # Erro inesperado (não é bloqueio): tira a vaga da fila — senão ela seria
            # retentada a cada tick, martelando o site. Sem registro: a próxima busca acha de novo.
            log.exception("APinfo: erro inesperado na vaga %s: %s", vaga.codigo, e)
            rec = None
        self._fila.pop(0)
        if vai_consultar:
            api.marcar_vaga()
        if rec is not None:
            self._resultados.append(rec)
        if not self._fila:
            self._finish_round(config, parada=None)

    def _finish_round(self, config: dict, parada: str | None) -> None:
        """Fim de uma rodada (fila vazia, erro ou bloqueio): resumo + agenda a próxima busca."""
        if self._resultados:
            views.send_resumo(self._resultados, parada, self._restantes, self._modo(config))
        if parada is None:
            self._bloqueios = 0
        intervalo_min = _cfg(config).get("intervalo_busca_min", 60)
        if intervalo_min < client.INTERVALO_MIN_SEGURO:
            log.info("apinfo_jobs.intervalo_busca_min=%s muito baixo; usando %s", intervalo_min, client.INTERVALO_MIN_SEGURO)
            intervalo_min = client.INTERVALO_MIN_SEGURO
        # Horário irregular (±15%) e espera dobrada a cada bloqueio seguido.
        espera = intervalo_min * 60 * min(2 ** self._bloqueios, client.BACKOFF_MAX) * random.uniform(0.85, 1.15)
        self._next_search_at = client.now() + espera
        hora = f"{datetime.now() + timedelta(seconds=espera):%H:%M}"
        if self._bloqueios:
            views.notify_blocked(self._bloqueios, hora)
        log.info("APinfo: próxima busca às %s.", hora)
        self._resultados, self._restantes = [], 0

    # --- aprovação: não se aplica --------------------------------------------------------------
    # Fonte automática: nunca chama queue_for_approval, então nada dela chega em approvals.py
    # e estes métodos nunca são chamados pelo núcleo.

    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        raise NotImplementedError("APinfo envia sem aprovação")

    def deliver(self, entry: dict) -> tuple[bool, str]:
        raise NotImplementedError("APinfo envia sem aprovação")

    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        raise NotImplementedError("APinfo envia sem aprovação")

    def on_rejected(self, entry: dict) -> None:
        raise NotImplementedError("APinfo envia sem aprovação")
