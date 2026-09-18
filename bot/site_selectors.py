"""
Todos os seletores CSS/XPath usados pelo bot ficam centralizados aqui.

IMPORTANTE: estes seletores são um ponto de partida razoável baseado na
estrutura típica de um site como o 99Freelas, mas NÃO foram validados contra
uma sessão real logada (o ambiente onde este código foi gerado não tem acesso
de rede para testar). Na primeira execução com HEADLESS=false, abra o DevTools
(F12) do navegador, confirme cada seletor abaixo contra o HTML real e ajuste
aqui. Centralizar aqui evita ter que caçar seletores espalhados pelo código.
"""

# --- Dashboard (saldo de conexões) ---
DASHBOARD_URL = "https://www.99freelas.com.br/dashboard"

# --- Mensagens (badge de não lidas, header) ---
# Confirmado contra HTML real (fornecido pelo usuário): badge no header do site, presente
# em qualquer página autenticada (ex: a própria /dashboard, já visitada 1x por ciclo por
# connections.refresh — reaproveitada aqui, sem precisar de uma URL de mensagens própria).
# A classe "show" só é adicionada quando há mensagens não lidas; sem ela, tratar como 0.
# Ex real: <div class="box-mensagem-count count show"><span class="count-value">1</span></div>
MESSAGES_BADGE_CONTAINER = "div.box-mensagem-count"
MESSAGES_BADGE_VALUE = "div.box-mensagem-count .count-value"

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
# Confirmado: quando a conta não tem o plano Freelancer Premium ativo, ESTE link
# ("Ver plano") aparece no lugar de PROPOSAL_BUTTON — testado em 10/10 projetos reais
# da listagem, mesmo com conexões disponíveis. Checar isso ANTES de tentar clicar em
# PROPOSAL_BUTTON evita timeout e dá um motivo de falha claro em vez de "botão não encontrado".
PROPOSAL_PREMIUM_REQUIRED_MARKER = "a[href='/freelancer-premium']"
# Confirmado: bloco com "Valor médio das propostas" e "Duração média estimada" (não o
# menor valor — só a média está disponível sem Premium), dentro da página de envio
# (/project/bid/..., depois de clicar em PROPOSAL_BUTTON). Ex real:
# <div class="generic information">Valor médio das propostas: <b>R$&nbsp;857,87</b><br>
# Duração média estimada: <b>10 dias</b></div>
# Só existe quando o projeto já tem propostas suficientes pra calcular a média — em
# projetos bem recentes (o alvo do nosso filtro de idade) costuma estar ausente; nesse
# caso _read_lowest_bid retorna None e build_proposal cai pro fallback normal.
PROPOSAL_LOWEST_BID = ".generic.information"
PROPOSAL_OFERTA_INPUT = "#oferta-final"
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
