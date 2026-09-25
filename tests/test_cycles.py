"""
Varreduras de cada fonte: 99Freelas (scraper/prepare_proposal mockados, sem Playwright) e
GitHub (fetch_issues mockado, sem rede). Confere o que vira pedido de aprovação, o que é
registrado e com qual status.
"""
from datetime import datetime, timedelta

import pytest

from bot import approvals, connections, manual_queue, scraper, storage, submitter
from tests.conftest import github_config, issue, project_99, proposal_99, read_json

PID = "785400"
LINK = "https://www.99freelas.com.br/project/site-institucional-785400"


# --- 99Freelas: run_cycle ----------------------------------------------------------------


@pytest.fixture
def site(api, monkeypatch):
    """Listagem e preparo falsos. `prepare` é uma função (page, project, config, **kw)."""
    state = {"projects": [], "prepare_calls": []}

    def fetch(page):
        assert page is api.page
        return [dict(p) for p in state["projects"]]

    def prepare(page, project, config, require_average=False):
        state["prepare_calls"].append((project["id"], require_average))
        return state["prepare"](project)

    monkeypatch.setattr(connections, "refresh", lambda page: None)
    monkeypatch.setattr(scraper, "fetch_open_projects", fetch)
    api.patch_submitter("prepare_proposal", prepare)
    state["prepare"] = lambda project: (proposal_99(), "ok")
    return state


def test_run_cycle_match_vira_pedido_de_aprovacao(api, telegram, site):
    site["projects"] = [project_99()]
    api.run_cycle({"proposal": {}})

    entry = approvals.get_pending(PID)
    assert entry["proposal"] == proposal_99()
    assert entry["telegram_message_id"] is not None
    assert storage.get_application(PID)["status"] == "pending_approval"
    assert site["prepare_calls"] == [(PID, False)]
    aprovacao = [p for p in telegram.of("sendMessage") if "Nova proposta pra aprovar" in p["text"]]
    assert len(aprovacao) == 1


def test_run_cycle_ignora_ja_avaliados(api, telegram, site):
    storage.register_application(PID, "t", status="failed")
    site["projects"] = [project_99()]
    api.run_cycle({"proposal": {}})
    assert site["prepare_calls"] == []
    assert telegram.calls == []


def test_run_cycle_filtro(api, telegram, site):
    site["projects"] = [project_99()]
    api.run_cycle({"proposal": {}, "keywords_exclude": ["landing"]})
    rec = storage.get_application(PID)
    assert rec["status"] == "skipped_duplicate"
    assert "landing" in rec["detail"]
    assert site["prepare_calls"] == []
    assert any(p["text"].startswith("<b>[99Freelas]</b> 🚫 Ignorado") for p in telegram.of("sendMessage"))


def test_run_cycle_preparo_falhou(api, telegram, site):
    site["projects"] = [project_99()]
    site["prepare"] = lambda project: (None, "projeto foi fechado")
    api.run_cycle({"proposal": {}})
    assert storage.get_application(PID)["status"] == "failed"
    assert approvals.get_pending(PID) is None
    falha = telegram.last("sendMessage")
    assert "Falha ao enviar proposta" in falha["text"]
    assert falha["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"retry:{PID}"


def test_run_cycle_limite_por_ciclo(api, site):
    site["projects"] = [project_99(id=str(i), title=f"P{i}") for i in range(12)]
    api.run_cycle({"proposal": {}})
    assert len(site["prepare_calls"]) == 10
    assert not storage.already_applied("10") and not storage.already_applied("11")


def test_run_cycle_aguardando_media(api, site):
    site["projects"] = [project_99()]
    site["prepare"] = lambda project: (None, submitter.AGUARDANDO_MEDIA)
    api.run_cycle({"proposal": {"aguardar_media": True}})
    rec = storage.get_application(PID)
    assert rec["status"] == "awaiting_average"
    assert rec["project"]["id"] == PID
    assert site["prepare_calls"] == [(PID, True)]


def test_recheck_media_apareceu(api, site):
    storage.register_application(
        PID, "t", status="awaiting_average", detail="x",
        extra={"project": project_99(), "aguardando_desde": datetime.utcnow().isoformat()},
    )
    api.run_cycle({"proposal": {"aguardar_media": True}})
    assert site["prepare_calls"] == [(PID, True)]
    assert storage.get_application(PID)["status"] == "pending_approval"
    assert approvals.get_pending(PID) is not None


def test_recheck_media_desiste_apos_limite(api, site):
    storage.register_application(
        PID, "t", status="awaiting_average", detail="x",
        extra={"project": project_99(), "aguardando_desde": (datetime.utcnow() - timedelta(hours=50)).isoformat()},
    )
    api.run_cycle({"proposal": {"aguardar_media": True, "aguardar_media_max_horas": 48}})
    assert site["prepare_calls"] == []
    assert storage.get_application(PID)["status"] == "failed"


def test_recheck_projeto_fechou(api, telegram, site):
    storage.register_application(
        PID, "t", status="awaiting_average", detail="x",
        extra={"project": project_99(), "aguardando_desde": datetime.utcnow().isoformat()},
    )
    site["prepare"] = lambda project: (None, "projeto foi fechado")
    api.run_cycle({"proposal": {"aguardar_media": True}})
    assert storage.get_application(PID)["status"] == "failed"
    assert any("Parou de aguardar" in p["text"] for p in telegram.of("sendMessage"))


# --- 99Freelas: links colados (manual_queue) -----------------------------------------------


def test_manual_prepara_sem_filtro(api, site):
    manual_queue.add(PID, LINK)
    storage.register_application(PID, "t", status="skipped_duplicate")

    def prepare(project):
        assert project["url"] == LINK
        project["title"] = "Título da página"
        return proposal_99(), "ok"

    site["prepare"] = prepare
    api.process_manual_projects({"proposal": {}})
    assert manual_queue.peek_all() == []
    assert approvals.get_pending(PID)["project"]["title"] == "Título da página"
    assert storage.get_application(PID)["status"] == "pending_approval"


def test_manual_falha_nao_sobrescreve_registro(api, telegram, site):
    manual_queue.add(PID, LINK)
    storage.register_application(PID, "t", status="sent")
    site["prepare"] = lambda project: (None, "proposta já enviada")
    api.process_manual_projects({"proposal": {}})
    assert manual_queue.peek_all() == []
    assert storage.get_application(PID)["status"] == "sent"
    assert "Não consegui preparar a proposta" in telegram.last("sendMessage")["text"]


def test_manual_excecao_tira_da_fila(api, site):
    manual_queue.add(PID, LINK)

    def boom(project):
        raise RuntimeError("timeout")

    site["prepare"] = boom
    api.process_manual_projects({"proposal": {}})
    assert manual_queue.peek_all() == []
    assert storage.get_application(PID)["status"] == "failed"


def test_manual_ja_aguardando(api, telegram, site):
    approvals.add_pending(project_99(), proposal_99(), 555)
    manual_queue.add(PID, LINK)
    api.process_manual_projects({"proposal": {}})
    assert site["prepare_calls"] == []
    assert manual_queue.peek_all() == []
    assert "já está aguardando" in telegram.last("sendMessage")["text"]


def test_manual_cancelar_proposta(api, telegram, site):
    storage.register_application(PID, "Site", status="sent", extra={"texto_variante": "plano"})
    manual_queue.add(PID, LINK, action="cancel")
    chamadas = []
    api.patch_submitter("cancel_proposal", lambda page, url: chamadas.append(url) or (True, "proposta cancelada"))
    api.process_manual_projects({"proposal": {}})
    assert chamadas == [LINK]
    assert site["prepare_calls"] == []
    assert manual_queue.peek_all() == []
    rec = storage.get_application(PID)
    assert rec["status"] == "cancelled_by_user"
    assert rec["texto_variante"] == "plano"
    assert "Proposta cancelada" in telegram.last("sendMessage")["text"]


def test_manual_cancelar_falha_mantem_registro(api, telegram, site):
    storage.register_application(PID, "Site", status="sent")
    manual_queue.add(PID, LINK, action="cancel")
    api.patch_submitter("cancel_proposal", lambda page, url: (False, "não há proposta ativa sua nesse projeto pra cancelar"))
    api.process_manual_projects({"proposal": {}})
    assert manual_queue.peek_all() == []
    assert storage.get_application(PID)["status"] == "sent"
    assert "Não consegui cancelar" in telegram.last("sendMessage")["text"]


# --- GitHub --------------------------------------------------------------------------------


@pytest.fixture
def issues(api):
    state = {"por_repo": {}, "calls": []}

    def fetch(repo, labels):
        state["calls"].append((repo, labels))
        return state["por_repo"].get(repo, [])

    api.patch_fetch_issues(fetch)
    return state


COM_EMAIL = "## Sobre\nx\n\n## Como se candidatar\nEnvie pra jobs@acme.com.\n"
SEM_EMAIL = "## Como se candidatar\nPelo site https://acme.com/vagas"


def test_github_desligado(api, issues):
    api.run_github_cycle({"github_jobs": {"enabled": False, "repos": [{"repo": "frontendbr/vagas"}]}})
    api.run_github_cycle({})
    assert issues["calls"] == []


def test_github_com_email_vira_pedido(api, telegram, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(7, "Dev React", COM_EMAIL)]
    api.run_github_cycle(github_config())

    job_id = "gh-frontendbr/vagas#7"
    entry = approvals.get_pending(job_id)
    assert entry["project"]["source"] == "github"
    assert entry["proposal"]["email_to"] == "jobs@acme.com"
    assert entry["proposal"]["email_da_secao_candidatura"] is True
    assert entry["proposal"]["assunto"] == "Dev React"
    assert entry["proposal"]["texto"] == (
        'Olá! Vi a vaga "Dev React" (https://github.com/frontendbr/vagas/issues/7).\n\nGitHub (https://github.com/eu)'
    )
    assert '<a href="https://github.com/eu">GitHub</a>' in entry["proposal"]["texto_html"]
    rec = api.github.get_record(job_id)
    assert (rec["status"], rec["email_to"]) == ("pending_approval", "jobs@acme.com")
    assert storage.get_application(job_id) is None
    assert issues["calls"] == [("frontendbr/vagas", ["Remoto"])]
    assert telegram.last("sendMessage")["text"].startswith("<b>[GitHub]</b> 📧 <b>Vaga pra aprovar")


def test_github_sem_email_so_avisa(api, telegram, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(8, "Dev Vue", SEM_EMAIL)]
    api.run_github_cycle(github_config())
    assert approvals.get_pending("gh-frontendbr/vagas#8") is None
    assert api.github.get_record("gh-frontendbr/vagas#8")["status"] == "no_email"
    assert "Vaga nova sem e-mail" in telegram.last("sendMessage")["text"]


def test_github_issue_velha(api, telegram, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(9, "Antiga", COM_EMAIL, age_days=30)]
    api.run_github_cycle(github_config())
    assert api.github.get_record("gh-frontendbr/vagas#9")["status"] == "skipped_old"
    assert telegram.calls == []


def test_github_ja_vista_nao_repete(api, telegram, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(7, "Dev React", COM_EMAIL)]
    api.run_github_cycle(github_config())
    telegram.clear()
    api.run_github_cycle(github_config())
    assert telegram.calls == []


def test_github_telegram_fora_nao_registra(api, telegram, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(7, "Dev React", COM_EMAIL)]
    telegram.down = True
    api.run_github_cycle(github_config())
    assert api.github.get_record("gh-frontendbr/vagas#7") is None
    assert approvals.get_pending("gh-frontendbr/vagas#7") is None


def test_github_limite_por_ciclo(api, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(n, f"Vaga {n}", COM_EMAIL) for n in range(12)]
    api.run_github_cycle(github_config())
    data = read_json(api.github.DATA_PATH)
    assert len(data) == 10


def test_github_repo_com_erro_nao_para_os_outros(api, issues):
    cfg = github_config(repos=[{"repo": "quebrado/x"}, {"repo": "frontendbr/vagas", "labels": []}])

    def fetch(repo, labels):
        if repo == "quebrado/x":
            raise RuntimeError("503")
        return [issue(7, "Dev", COM_EMAIL)]

    api.patch_fetch_issues(fetch)
    api.run_github_cycle(cfg)
    assert approvals.get_pending("gh-frontendbr/vagas#7") is not None


def test_github_anexo_relativo_vira_absoluto(api, issues):
    issues["por_repo"]["frontendbr/vagas"] = [issue(7, "Dev", COM_EMAIL)]
    cfg = github_config(email={"texto": "Oi", "anexo": "data/cv.pdf"})
    api.run_github_cycle(cfg)
    anexo = approvals.get_pending("gh-frontendbr/vagas#7")["proposal"]["anexo"]
    assert anexo.replace("\\", "/").endswith("data/cv.pdf")
    assert anexo != "data/cv.pdf"


# --- GitHub: funções puras -------------------------------------------------------------------


@pytest.mark.parametrize("body,esperado", [
    (COM_EMAIL, ("jobs@acme.com", True)),
    ("Contato: feedback@acme.com\n\n## Como se candidatar\nPelo site", ("feedback@acme.com", False)),
    ("## Como se candidatar\nnoreply@acme.com ou rh@acme.com.", ("rh@acme.com", True)),
    ("sem e-mail nenhum", (None, False)),
    ("", (None, False)),
    (None, (None, False)),
    ("x@example.com e y@users.noreply.github.com", (None, False)),
])
def test_extract_email(api, body, esperado):
    assert api.github.extract_email(body) == esperado


def test_job_id_for(api):
    assert api.github.job_id_for("frontendbr/vagas", 12) == "gh-frontendbr/vagas#12"
    longo = api.github.job_id_for("uma-organizacao-muito-grande/um-repositorio-de-vagas-enorme", 123456)
    assert len(longo.encode()) <= 50
    assert ":" not in longo
    assert longo.startswith("gh-") and longo.endswith("#123456")
