# Deploy automático na Oracle

A cada push/merge na `master`, o GitHub Actions (`.github/workflows/deploy.yml`) roda os
testes e, se passarem, faz `git push` pro repositório do bot no servidor. O hook
`post-receive` aplica a atualização:

- só código → o hot reload do bot reinicia ele sozinho em alguns segundos;
- `requirements.txt`, `Dockerfile`, `docker-compose.yml` ou `.dockerignore` → rebuild da imagem.

`.env`, `config.yaml` e `data/` não estão no git e nunca são tocados pelo deploy.

## Configuração (uma vez)

1. Mande estes arquivos pro servidor (`git push oracle master` do PC) e rode lá:

   ```bash
   cd ~/99freelas-bot && bash deploy/setup-servidor.sh 64.181.179.118
   ```

2. Em **GitHub → repositório → Settings → Secrets and variables → Actions → New repository
   secret**, crie os 3 secrets com o que o script imprimiu:
   `ORACLE_SSH_KEY`, `ORACLE_HOST` e `ORACLE_KNOWN_HOSTS`.

3. Teste em **Actions → Deploy na Oracle → Run workflow**.

A chave do GitHub Actions só consegue fazer `git push` no repositório do bot
(`deploy/ci-deploy-gate`): sem shell, sem cópia de arquivos, sem túnel. Pra revogar, apague a
linha `github-actions-deploy` de `~/.ssh/authorized_keys` no servidor.
