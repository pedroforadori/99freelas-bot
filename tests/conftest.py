"""
Infra comum dos testes: isola data/ num tmp_path, troca o Telegram por um fake que grava
as chamadas e congela a data (utils.daily_quota depende dos dias do mês corrente).

Nenhum teste toca a rede, o Playwright ou o data/ real. O fixture `api` é o ÚNICO lugar
que conhece onde cada função mora no código — se um refactor mover algo de módulo, só ele
muda; os testes em si (e os snapshots) continuam iguais.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import logging  # noqa: E402

from bot import logger_setup  # noqa: E402

# Antes de importar qualquer outro módulo do bot: loggers sem handler de console/arquivo
# (nada de escrever no logs/bot.log real; o pytest captura via propagação pro root).
logger_setup.get_logger = logging.getLogger

from bot import ai_writer, approvals, connections, email_sender, manual_queue, messages  # noqa: E402
from bot import main, notifier, storage, submitter, telegram_api, utils  # noqa: E402
from bot.sources import registry  # noqa: E402
from bot.sources.freelas99 import views as views_99  # noqa: E402
from bot.sources.github import client as github_jobs  # noqa: E402
from bot.sources.github import views as views_gh  # noqa: E402
from bot.telegram_dispatcher import TelegramDispatcher  # noqa: E402

FREELAS99 = registry.source_of({"source": "99freelas"})
GITHUB = registry.source_of({"source": "github"})

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")
CHAT_ID = "4242"
FIXED_TODAY = date(2026, 9, 25)  # setembro = 30 dias → daily_quota(240) == 8


# --- Telegram falso --------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data
        self.status_code = 200 if data.get("ok") else 400
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeTelegram:
    """Substitui requests.post: grava (method, payload) e responde como a Bot API."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.down = False  # True = toda chamada falha (Telegram fora do ar)
        self._next_message_id = 1000
        self._next_update_id = 1

    def post(self, url, json=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        payload = json or {}
        self.calls.append((method, payload))
        if self.down:
            return _FakeResponse({"ok": False, "description": "down"})
        if method == "getUpdates":
            offset = payload.get("offset", 0)
            pendentes = [u for u in self.updates if u["update_id"] >= offset]
            return _FakeResponse({"ok": True, "result": pendentes})
        if method == "sendMessage":
            self._next_message_id += 1
            return _FakeResponse({"ok": True, "result": {"message_id": self._next_message_id}})
        return _FakeResponse({"ok": True, "result": True})

    # helpers pros testes
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def last(self, method: str) -> dict:
        found = self.of(method)
        assert found, f"nenhuma chamada {method}; chamadas: {self.methods()}"
        return found[-1]

    def clear(self):
        self.calls.clear()

    def push_update(self, **kind) -> None:
        self.updates.append({"update_id": self._next_update_id, **kind})
        self._next_update_id += 1

    def push_callback(self, data: str, message_id: int = 555, text: str = "") -> None:
        self.push_update(callback_query={
            "id": f"cb{self._next_update_id}",
            "data": data,
            "message": {"message_id": message_id, "text": text, "chat": {"id": int(CHAT_ID)}},
        })

    def push_message(self, text: str, reply_to: int | None = None, chat_id: str = CHAT_ID, entities=None) -> None:
        message = {"message_id": 9000 + self._next_update_id, "text": text, "chat": {"id": int(chat_id)}}
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        if entities:
            message["entities"] = entities
        self.push_update(message=message)


@pytest.fixture
def telegram(monkeypatch) -> FakeTelegram:
    fake = FakeTelegram()
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


# --- Isolamento de data/, env e data ----------------------------------------------------


class _FixedDate(date):
    @classmethod
    def today(cls):
        return FIXED_TODAY


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, telegram):
    for key in list(os.environ):
        if key.startswith(("SMTP_", "EMAIL_", "GITHUB_", "TELEGRAM_", "PORTFOLIO_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("ACTIVITY_LOG_TELEGRAM", "true")
    monkeypatch.setenv("MONTHLY_PROPOSAL_QUOTA", "240")

    monkeypatch.setattr(utils, "date", _FixedDate)
    monkeypatch.setattr(storage, "date", _FixedDate)

    for module, attr, name in _data_paths():
        monkeypatch.setattr(module, attr, str(tmp_path / name))

    # Delays de "parecer humano" não têm lugar em teste.
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _data_paths():
    return [
        (storage, "DATA_PATH", "applied_jobs.json"),
        (approvals, "DATA_PATH", "pending_approvals.json"),
        (manual_queue, "DATA_PATH", "manual_queue.json"),
        (connections, "CACHE_PATH", "connections.json"),
        (messages, "CACHE_PATH", "messages_state.json"),
        (github_jobs, "DATA_PATH", "github_jobs.json"),
        (telegram_api, "_OFFSET_PATH", "telegram_offset.json"),
    ]


# --- Adaptador pro código (único ponto que muda num refactor) ---------------------------


@pytest.fixture
def api(monkeypatch):
    page = object()  # Playwright nunca é usado de verdade: submitter é mockado nos testes
    monkeypatch.setattr(FREELAS99, "page", page)

    def render_approval(project, proposal):
        return registry.source_of(project).render_approval(project, proposal)

    def run_github_cycle(config):
        if GITHUB.is_enabled(config):  # main.py só roda as fontes ativas
            GITHUB.run_cycle(config)

    return SimpleNamespace(
        page=page,
        render_approval=render_approval,
        send_approval_request=lambda project, proposal: notifier.send_approval_message(*render_approval(project, proposal)),
        notify_proposal_result=views_99.notify_proposal_result,
        notify_manual_project_failed=views_99.notify_manual_project_failed,
        notify_new_messages=views_99.notify_new_messages,
        notify_github_no_email=views_gh.notify_no_email,
        notify_github_email_result=views_gh.notify_email_result,
        poll=lambda config: TelegramDispatcher(registry.ALL_SOURCES, config).poll(),
        process_approvals=lambda config: main.process_pending_approvals(),
        process_manual_projects=FREELAS99.process_manual_projects,
        run_cycle=FREELAS99.run_cycle,
        run_github_cycle=run_github_cycle,
        github=github_jobs,
        patch_submitter=lambda name, fn: monkeypatch.setattr(submitter, name, fn),
        patch_email_send=lambda fn: monkeypatch.setattr(email_sender, "send", fn),
        patch_ai_text=lambda fn: monkeypatch.setattr(ai_writer, "generate_proposal_text", fn),
        patch_fetch_issues=lambda fn: monkeypatch.setattr(github_jobs, "fetch_issues", fn),
    )


# --- Snapshots ---------------------------------------------------------------------------


@pytest.fixture
def snapshot(request):
    """
    snapshot(nome, valor): compara `valor` (serializável em JSON) com
    tests/snapshots/<nome>.json. UPDATE_SNAPSHOTS=1 regrava — só faça isso quando a
    mudança de texto for intencional, nunca pra "consertar" um refactor.
    """
    def check(name: str, value):
        path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
        rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(rendered + "\n")
            return
        if not os.path.exists(path):
            pytest.fail(f"snapshot {name} não existe (rode com UPDATE_SNAPSHOTS=1 pra criar)")
        with open(path, "r", encoding="utf-8") as f:
            expected = f.read().rstrip("\n")
        assert rendered == expected, f"snapshot {name} mudou"
    return check


# --- Dados de exemplo ---------------------------------------------------------------------


def project_99(**over) -> dict:
    base = {
        "id": "785400",
        "title": "Site institucional & landing <page>",
        "url": "https://www.99freelas.com.br/project/site-institucional-785400",
        "category": "Web",
        "budget": None,
        "description": "Descrição curta da listagem",
        "posted_minutes_ago": 12,
    }
    return {**base, **over}


def proposal_99(**over) -> dict:
    base = {
        "oferta": 1200.0,
        "prazo_dias": 8,
        "texto": "Olá! Posso te ajudar com esse site.",
        "origem_valor": "media_arredondada",
        "media_concorrentes": 1234.5,
        "media_prazo": 10,
        "texto_variante": "pergunta",
        "texto_ia_falhou": False,
        "full_description": "Preciso de um site institucional com 5 páginas.",
    }
    return {**base, **over}


def github_job(**over) -> dict:
    base = {
        "id": "gh-frontendbr/vagas#123",
        "source": "github",
        "repo": "frontendbr/vagas",
        "number": 123,
        "title": "[Remoto] Dev Front-end Sênior na ACME",
        "url": "https://github.com/frontendbr/vagas/issues/123",
        "labels": ["Sênior", "Remoto"],
        "description": "## Descrição\nVaga legal.\n\n## Como se candidatar\nMande e-mail pra vagas@acme.com",
        "email_to": "vagas@acme.com",
        "email_da_secao_candidatura": True,
    }
    return {**base, **over}


def github_email(**over) -> dict:
    base = {
        "email_to": "vagas@acme.com",
        "email_da_secao_candidatura": True,
        "assunto": "[Remoto] Dev Front-end Sênior na ACME",
        "texto": "Olá! Segue meu currículo.",
        "texto_html": "<div>Olá! Segue meu currículo.</div>",
        "anexo": None,
    }
    return {**base, **over}


def github_config(**over) -> dict:
    cfg = {
        "enabled": True,
        "repos": [{"repo": "frontendbr/vagas", "labels": ["Remoto"]}],
        "max_issue_age_days": 7,
        "email": {"texto": "Olá! Vi a vaga \"{titulo}\" ({url}).\n\n[GitHub](https://github.com/eu)", "anexo": None},
    }
    cfg.update(over)
    return {"github_jobs": cfg, "proposal": {}}


def issue(number: int, title: str, body: str, age_days: float = 0.1) -> dict:
    # Relativo ao relógio real: github_jobs.check_new_issues usa datetime.now() pro corte.
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/frontendbr/vagas/issues/{number}",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "labels": [{"name": "Remoto"}],
    }


def read_json(path) -> dict | list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
