# 99Freelas Proposal Bot

Bot em Python (Playwright) que monitora novos projetos no [99Freelas](https://www.99freelas.com.br),
filtra os que combinam com o seu perfil, monta a proposta (texto via IA, oferta/prazo
calculados ou sugeridos por IA) e **manda pro Telegram pra você aprovar antes de enviar**.
Nada é enviado sem o seu clique em ✅ Aprovar.

Feito para rodar continuamente (24/7) em servidor via Docker.

---

## Funcionalidades

- **Varredura contínua** da listagem de projetos, com intervalo aleatório entre ciclos.
- **Filtro de perfil** (`config.yaml`): categorias, palavras-chave de inclusão/exclusão,
  faixa de orçamento e idade máxima do projeto.
- **Proposta montada automaticamente**:
  - Texto gerado por IA (Gemini ou Anthropic) a partir da descrição completa do projeto,
    ou template fixo. Filtro de segurança bloqueia contato, valor, prazo e links no texto.
  - Oferta baseada no orçamento do cliente, num valor fixo, na média das propostas
    concorrentes ou, sem nenhum dado, numa sugestão da IA (com desconto competitivo configurável).
  - Nota fixa opcional anexada ao final de toda proposta (`proposal.nota_extra`).
- **Aprovação no Telegram**, com botões:
  - ✅ Aprovar / ❌ Rejeitar
  - ✏️ Editar oferta / ✏️ Editar prazo (responde com o valor em texto livre)
  - 🔄 Tentar gerar o texto via IA novamente (quando a IA falhou e caiu pro template)
  - 🔄 Tentar de novo (em notificações de falha de preparo/envio)
- **Projeto por link**: cole o link de um projeto no chat do bot e ele prepara uma proposta
  pra aprovação, mesmo que o projeto não passe no filtro.
- **Alertas no Telegram**: resultado de cada envio, saldo de conexões do plano, ritmo diário
  ("Hoje: X/Y", com aviso quando o limite diário é ultrapassado), novas mensagens de
  clientes e mudanças de estado do bot (online, parado, erro, sessão expirada).
- **Modo simulação** (`dry_run.py`): preenche o formulário mas nunca clica em enviar.
- **Portfólio** (`portfolio.py`): captura screenshots de sites/apps, gera título e
  descrição via IA e manda pro Telegram pra aprovação. O upload no site é feito à mão.

---

## ⚠️ Avisos importantes

1. **Termos de uso**: os Termos do 99Freelas não proíbem bots de forma explícita, mas
   proíbem spam e podem rebaixar propostas de perfis com comportamento fora do esperado.
   O bot usa aprovação humana, cota diária e intervalos aleatórios pra reduzir esse risco.
   Use por sua conta e risco.
2. **Plano Freelancer Premium**: sem o plano ativo, o site não mostra o botão "Enviar
   proposta" em nenhum projeto. Cada proposta enviada consome uma conexão do plano.
3. **Login**: o formulário de login usa Cloudflare Turnstile, que bloqueia login
   automatizado. A autenticação é feita importando os cookies do seu navegador (ver abaixo).
4. **Credenciais**: nunca commite `.env`, `config.yaml` ou `data/auth_state.json`.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env                # Telegram, chaves de IA, intervalos, cota
cp config.example.yaml config.yaml  # critérios do perfil e estratégia de proposta
```

### Autenticação (cookies)

1. Faça login no 99Freelas no seu navegador de sempre.
2. Exporte os cookies de `99freelas.com.br` (ex: extensão Cookie-Editor → "Export as JSON").
3. Importe:

```bash
python bot/import_cookies.py caminho/para/cookies.json
```

Isso gera `data/auth_state.json`, usado pelo bot. Quando a sessão expirar, o bot avisa
no Telegram e para. Basta repetir a importação.

### Telegram

1. Crie um bot com o [@BotFather](https://t.me/BotFather) (`/newbot`) e pegue o token.
2. Mande qualquer mensagem pro bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` pra pegar o `chat.id`.
3. Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no `.env`.

Para os botões do fluxo de portfólio, crie um **segundo** bot e use o token em
`PORTFOLIO_TELEGRAM_BOT_TOKEN`: dois processos não podem ler os cliques do mesmo bot.

### IA

Configure `proposal.texto_modo: "ia"`, `ia_provider` e `ia_model` no `config.yaml`, e a
chave correspondente no `.env` (`GEMINI_API_KEY` ou `ANTHROPIC_API_KEY`).

---

## Uso

```bash
python bot/dry_run.py   # simulação: preenche o formulário, nunca envia
python bot/main.py      # bot real (rodar a partir da raiz do repo)
```

Rode primeiro com `HEADLESS=false` no `.env` pra ver o navegador e confirmar que tudo
funciona antes de deixar rodando sem supervisão.

Portfólio:

```bash
python bot/portfolio.py https://site-do-cliente.com.br
python bot/portfolio.py --app-store URL --play-store URL
python bot/portfolio.py --file trabalhos.yaml
```

### Deploy (24/7)

```bash
docker compose up -d --build
docker compose logs -f
```

O container reinicia automaticamente (`restart: unless-stopped`) e persiste `data/` e
`logs/` fora dele. `config.yaml` é montado como somente leitura.

---

## Como funciona

```
main.py          loop principal: varredura + polling de aprovações/mensagens/links
scraper.py       extrai os projetos da listagem
filter.py        decide se o projeto combina com o perfil (config.yaml)
proposal.py      monta oferta, prazo e texto
ai_writer.py     chamadas de IA (texto da proposta, sugestão de preço, portfólio)
submitter.py     prepara a proposta e, depois da aprovação, envia no site
approvals.py     fila de propostas aguardando aprovação (data/pending_approvals.json)
notifier.py      tudo que envolve Telegram (mensagens, botões, respostas)
manual_queue.py  fila de projetos enviados por link no Telegram
connections.py   saldo de conexões do plano (lido do /dashboard)
messages.py      contador de mensagens não lidas (badge do header)
storage.py       projetos já processados e contagem diária (data/applied_jobs.json)
site_selectors.py  todos os seletores CSS do site, num lugar só
portfolio*.py    captura e revisão de trabalhos de portfólio
```

Fluxo:

1. **Varredura** (a cada `CHECK_INTERVAL_MIN/MAX_SECONDS`): lê a listagem, ignora projetos
   já processados, aplica o filtro e, para cada projeto aderente, prepara a proposta
   (sem enviar) e manda o pedido de aprovação pro Telegram.
2. **Polling** (a cada `APPROVAL_POLL_INTERVAL_SECONDS`, default 20s): lê cliques e respostas
   no Telegram. A decisão é gravada em disco na hora, então uma aprovação não se perde se
   o processo cair.
3. **Envio**: propostas aprovadas são reenviadas ao site do zero (re-checa se o projeto
   ainda está aberto) e o resultado é notificado no Telegram.

Todos os seletores do site ficam em `bot/site_selectors.py`. Se o HTML do 99Freelas
mudar, é o único arquivo a ajustar.
