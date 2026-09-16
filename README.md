# 99Freelas Auto-Apply Bot

Bot em Python que monitora novos projetos no [99Freelas](https://www.99freelas.com.br),
filtra os que são aderentes ao seu perfil e envia propostas **automaticamente**,
sem revisão manual, usando um template fixo.

Feito para rodar continuamente (24/7) em servidor/cloud via Docker.

---

## ⚠️ Avisos importantes (leia antes de rodar em produção)

1. **Termos de uso**: os Termos do 99Freelas não deixam explícito "proibido usar
   bots", mas proíbem spam e dizem que comportamento fora do esperado pode fazer
   a plataforma **rebaixar suas propostas** ou marcar seu perfil com alerta.
   Enviar candidaturas 100% automáticas em alto volume é o tipo de comportamento
   que mais se assemelha a spam aos olhos da plataforma. Use por sua conta e risco.
2. **Seletores de HTML não testados**: eu não tive acesso à sessão logada do site
   para validar os seletores exatos de cada elemento (login, formulário de
   proposta, listagem de projetos). Todos os seletores ficam centralizados em
   `bot/site_selectors.py` — é bem provável que você precise ajustá-los usando o
   DevTools do navegador (botão direito → Inspecionar) na primeira execução.
3. **Credenciais**: nunca commite o arquivo `.env` com usuário/senha reais.
4. **Proposta genérica**: como você optou por template fixo (não gerado por IA),
   a taxa de conversão tende a ser mais baixa que uma proposta personalizada.
   Isso é uma escolha de trade-off (menos manutenção/custo, menos personalização).

---

## Como funciona (arquitetura)

```
main.py       → loop principal: busca, filtra, gera proposta, envia, registra
scraper.py    → abre a listagem de projetos e extrai título, categoria, orçamento, descrição, link
filter.py     → decide se um projeto é "aderente" com base no config.yaml
proposal.py   → monta o texto da proposta a partir do template + variáveis da vaga
submitter.py  → faz login (sessão reutilizada) e envia a proposta no projeto
storage.py    → guarda os IDs de projetos já candidatados, pra nunca duplicar
site_selectors.py → TODOS os seletores CSS/XPath do site, num único lugar pra facilitar manutenção
logger_setup.py → configuração de logs (arquivo + console)
```

Fluxo por ciclo:
1. Faz login (se sessão expirou).
2. Abre a página de listagem de projetos mais recentes.
3. Para cada projeto novo (que ainda não está em `data/applied_jobs.json`):
   - Aplica os critérios de `config.yaml` (categorias, palavras-chave, orçamento).
   - Se aderente: gera a proposta com `proposal.py` e envia com `submitter.py`.
   - Registra o resultado (sucesso/erro) em `data/applied_jobs.json` e no log.
4. Espera um intervalo aleatório (configurável) e repete.

---

## Setup local (teste antes de subir pra cloud)

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env       # preencha usuário/senha do 99Freelas
cp config.example.yaml config.yaml  # preencha seus critérios de perfil
python bot/main.py
```

Rode primeiro com `HEADLESS=false` no `.env` pra ver o navegador abrindo e
confirmar visualmente que o login e o envio de proposta estão funcionando,
antes de colocar em produção sem supervisão.

## Deploy em cloud (24/7)

```bash
docker compose up -d --build
docker compose logs -f     # acompanhar
```

O `docker-compose.yml` já reinicia o container automaticamente
(`restart: unless-stopped`) e persiste `data/` e `logs/` fora do container.

## Configuração do perfil (`config.yaml`)

Ainda não preenchido — você disse que quer decidir os critérios depois.
Preencha `categories`, `keywords_include`, `keywords_exclude`, `budget_min`
e `budget_max` em `config.yaml` antes de rodar de verdade. Enquanto estiver
vazio, o filtro vai deixar tudo passar (comportamento inseguro só pra dev/teste
— não suba pra produção assim).
