"""
Todos os seletores CSS/XPath usados pelo bot ficam centralizados aqui.

IMPORTANTE: estes seletores são um ponto de partida razoável baseado na
estrutura típica de um site como o 99Freelas, mas NÃO foram validados contra
uma sessão real logada (o ambiente onde este código foi gerado não tem acesso
de rede para testar). Na primeira execução com HEADLESS=false, abra o DevTools
(F12) do navegador, confirme cada seletor abaixo contra o HTML real e ajuste
aqui. Centralizar aqui evita ter que caçar seletores espalhados pelo código.
"""

# --- Login ---
LOGIN_URL = "https://www.99freelas.com.br/login"
LOGIN_EMAIL_INPUT = "#email"
LOGIN_PASSWORD_INPUT = "#senha"
LOGIN_SUBMIT_BUTTON = "#btnEfetuarLogin"
# Elemento que só aparece quando o login deu certo: link com o nome do usuário no header
LOGIN_SUCCESS_MARKER = "a[href^='/user/']"

# --- Listagem de projetos ---
# URL do filtro salvo "dev" do usuário: categoria Web, Mobile e Software, com as
# subcategorias desenvolvimento mobile, desenvolvimento web, outra (web/mobile/software)
# e UX/UI e web design. Ajustada manualmente pelo usuário — não é um chute.
PROJECTS_LIST_URL = (
    "https://www.99freelas.com.br/projects?order=mais-recentes"
    "&categoria=web-mobile-e-software"
    "&sub-categorias=desenvolvimento-mobile+desenvolvimento-web"
    "+outra-web-mobile-e-software+ux-ui-e-web-design"
)
# Todos os seletores de card abaixo foram validados contra o HTML real da listagem.
PROJECT_CARD = "li.result-item"
# data-id no próprio <li> é mais confiável que extrair o id da URL (usado como fonte
# principal; PROJECT_ID_URL_REGEX abaixo é o fallback).
PROJECT_CARD_ID_ATTR = "data-id"
PROJECT_CARD_TITLE = "h1.title a"
PROJECT_CARD_LINK = "h1.title a"
# Categoria e nível não têm elemento próprio: vêm como texto solto dentro deste parágrafo,
# separado por "|" (ex: "Modelagem 3D & CAD | Especialista | Publicado: ..."). A categoria
# é extraída pegando o primeiro trecho antes do "|" (ver scraper._extract_category).
PROJECT_CARD_INFO = ".item-text.information"
PROJECT_CARD_DESCRIPTION = ".item-text.description"
# NOTA: a listagem não mostra orçamento/valor do projeto — só aparece na página do
# projeto/proposta. project["budget"] fica sempre None a partir do scraper por enquanto.
# Elemento de data/hora de publicação: <b class="datetime" cp-datetime="1789577458000">21 minutos atrás</b>
# O atributo cp-datetime (epoch em milissegundos) é usado como fonte principal;
# o texto ("21 minutos atrás") serve de fallback via parse_relative_time_minutes.
PROJECT_CARD_POSTED_AT = ".datetime"
# Fallback: extrai o ID a partir da URL, caso data-id não esteja presente
PROJECT_ID_URL_REGEX = r"/project/[a-z0-9-]+-(\d+)"

# --- Página do projeto / envio de proposta ---
# Descrição completa (sem truncar) do projeto, na própria página do projeto — diferente
# de PROJECT_CARD_DESCRIPTION (".description"), que é o trecho truncado da listagem.
PROJECT_PAGE_DESCRIPTION = ".item-text.project-description"
# "Enviar proposta" é um link que NAVEGA pra uma página separada (/project/bid/<slug>-<id>),
# não abre um formulário na mesma página — ver navegação explícita em submitter.py.
PROPOSAL_BUTTON = "a.clickable:has-text('Enviar proposta')"
# Campo que mostra o menor valor já proposto por outro freelancer nesse projeto
# (aparece dentro do formulário de proposta, depois de clicar em PROPOSAL_BUTTON).
# Se o projeto ainda não tem nenhuma proposta, o elemento pode não existir — tratado como None.
PROPOSAL_LOWEST_BID = "[data-testid='lowest-proposal'], .menor-proposta, .lowest-bid"
PROPOSAL_OFERTA_INPUT = "#oferta"
PROPOSAL_PRAZO_INPUT = "#duracao-estimada"
PROPOSAL_DETALHES_TEXTAREA = "#proposta"
PROPOSAL_SUBMIT_BUTTON = "#btnConcluirEnvioProposta"
# Confirmado: ao enviar, o site redireciona de volta pra página do PROJETO (não fica
# na página /project/bid/...) e mostra este ícone ao lado do nome do projeto — o mesmo
# usado em PROPOSAL_ALREADY_SENT_MARKER.
PROPOSAL_SUCCESS_MARKER = ".icon-proposal"
# Na página do PROJETO (não na de envio): aparecem só quando você já tem uma proposta
# enviada pra esse projeto. Checados por PRESENÇA (query_selector), nunca clicados —
# "#btnCancelarProposta" é um link que CANCELA a proposta se clicado.
PROPOSAL_ALREADY_SENT_MARKER = "#btnCancelarProposta, .icon-proposal"
