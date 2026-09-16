from bot import ai_writer
from bot.logger_setup import get_logger
from bot.utils import format_currency_br

log = get_logger(__name__)


def _build_texto(project: dict, config: dict, oferta: float, prazo_dias: int, full_description: str | None) -> str:
    proposal_cfg = config.get("proposal", {})
    modo = proposal_cfg.get("texto_modo", "fixo")

    if modo == "ia" and full_description:
        gerado = ai_writer.generate_proposal_text(project, full_description, config)
        if gerado:
            return gerado
        log.warning("Geração via IA indisponível/rejeitada para '%s' — usando template fixo.", project.get("title"))

    template = proposal_cfg.get("texto", "Olá! Tenho interesse em {titulo}.")
    return template.format(
        titulo=project.get("title", ""),
        oferta=format_currency_br(oferta),
        prazo_dias=prazo_dias,
    )


def build_proposal(
    project: dict,
    config: dict,
    lowest_bid: float | None = None,
    full_description: str | None = None,
) -> dict:
    """
    Monta oferta, prazo e texto da proposta. Retorna dict pronto pra ser usado pelo
    submitter: {"oferta", "prazo_dias", "texto"}.

    lowest_bid: menor valor já proposto por outro freelancer no projeto (lido da página
    de envio de proposta), usado quando oferta_estrategia == "menor_proposta". None se
    o projeto ainda não tem nenhuma proposta ou o valor não pôde ser lido.

    full_description: descrição completa do projeto (lida da página do projeto, sem o
    truncamento da listagem), usada quando proposal.texto_modo == "ia" em config.yaml.
    Se ausente ou a geração via IA falhar/for rejeitada pelo filtro de segurança, cai
    pro template fixo em proposal.texto.
    """
    proposal_cfg = config.get("proposal", {})
    estrategia = proposal_cfg.get("oferta_estrategia", "orcamento_cliente")
    orcamento_cliente = project.get("budget") or proposal_cfg.get("oferta_fixa") or 0

    if estrategia == "menor_proposta" and lowest_bid:
        undercut_percent = proposal_cfg.get("undercut_percent", 5)
        oferta = round(lowest_bid * (1 - undercut_percent / 100), 2)
    elif estrategia == "fixo" and proposal_cfg.get("oferta_fixa") is not None:
        oferta = proposal_cfg["oferta_fixa"]
    else:
        # sem proposta concorrente pra usar de base (ou estratégia orcamento_cliente):
        # usa o orçamento anunciado pelo cliente; se não houver, cai pra oferta_fixa ou 0
        oferta = orcamento_cliente

    prazo_dias = proposal_cfg.get("prazo_dias", 7)
    texto = _build_texto(project, config, oferta, prazo_dias, full_description)

    log.info("Proposta montada para '%s': oferta=R$%s, prazo=%sd", project.get("title"), oferta, prazo_dias)

    return {"oferta": oferta, "prazo_dias": prazo_dias, "texto": texto}
