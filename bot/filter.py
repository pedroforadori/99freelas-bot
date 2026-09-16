"""
Decide se um projeto é "aderente ao perfil" com base no config.yaml.

O usuário ainda não definiu os critérios finais (categorias, palavras-chave,
faixa de orçamento) — este módulo já está pronto pra quando isso for
preenchido em config.yaml. Enquanto os campos estiverem vazios, o filtro é
permissivo (deixa passar quase tudo), o que é intencional pra facilitar teste
do resto do pipeline, mas NÃO deve ser usado assim em produção.
"""
from bot.logger_setup import get_logger

log = get_logger(__name__)


def is_match(project: dict, config: dict) -> tuple[bool, str]:
    """Retorna (aderente: bool, motivo: str) para fins de log."""
    title = (project.get("title") or "").lower()
    description = (project.get("description") or "").lower()
    category = (project.get("category") or "").lower()
    budget = project.get("budget")  # float ou None

    text = f"{title} {description}"

    max_age_minutes = config.get("max_project_age_minutes")
    posted_minutes_ago = project.get("posted_minutes_ago")
    if max_age_minutes is not None and posted_minutes_ago is not None and posted_minutes_ago > max_age_minutes:
        return False, f"publicado há {posted_minutes_ago:.0f}min, acima do limite de {max_age_minutes}min"

    exclude = [k.lower() for k in config.get("keywords_exclude", [])]
    for kw in exclude:
        if kw in text:
            return False, f"contém palavra-chave excluída: '{kw}'"

    categories = [c.lower() for c in config.get("categories", [])]
    if categories and category not in categories:
        return False, f"categoria '{category}' fora da lista aceita"

    include = [k.lower() for k in config.get("keywords_include", [])]
    if include and not any(kw in text for kw in include):
        return False, "nenhuma palavra-chave incluída encontrada"

    budget_min = config.get("budget_min")
    budget_max = config.get("budget_max")
    if budget is not None:
        if budget_min is not None and budget < budget_min:
            return False, f"orçamento R${budget} abaixo do mínimo R${budget_min}"
        if budget_max is not None and budget > budget_max:
            return False, f"orçamento R${budget} acima do máximo R${budget_max}"

    return True, "aderente"
