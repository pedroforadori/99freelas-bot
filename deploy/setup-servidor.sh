#!/bin/bash
# Prepara o servidor pro deploy automático do GitHub Actions (.github/workflows/deploy.yml).
# Rodar UMA vez, no servidor, a partir da pasta do bot:
#   cd ~/99freelas-bot && bash deploy/setup-servidor.sh <IP público do servidor>
# Imprime no final os valores dos 3 secrets do GitHub (ver deploy/README.md).
set -euo pipefail
HOST_IP="${1:?uso: bash deploy/setup-servidor.sh <IP público do servidor>}"
REPO=/home/ubuntu/99freelas-bot
cd "$REPO"

# 1. Repositório aceita push no branch atual, atualizando os arquivos.
git config receive.denyCurrentBranch updateInstead

# 2. Hook de deploy e porteiro da chave do CI.
install -m 755 deploy/post-receive .git/hooks/post-receive
mkdir -p ~/bin
install -m 755 deploy/ci-deploy-gate ~/bin/ci-deploy-gate

# 3. Chave exclusiva do GitHub Actions, presa ao porteiro (só git push no bot).
if ! grep -q "github-actions-deploy" ~/.ssh/authorized_keys 2>/dev/null; then
  TMP=$(mktemp -d)
  ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f "$TMP/key"
  echo "command=\"$HOME/bin/ci-deploy-gate\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat "$TMP/key.pub")" >> ~/.ssh/authorized_keys
  echo
  echo "===== secret ORACLE_SSH_KEY (copie tudo, incluindo as linhas BEGIN/END) ====="
  cat "$TMP/key"
  rm -rf "$TMP"   # a chave privada fica só no GitHub
else
  echo
  echo "A chave do GitHub Actions já existia em authorized_keys; não gerei outra."
  echo "Pra gerar de novo, apague a linha 'github-actions-deploy' de ~/.ssh/authorized_keys e rode outra vez."
fi

echo
echo "===== secret ORACLE_HOST ====="
echo "$HOST_IP"
echo
echo "===== secret ORACLE_KNOWN_HOSTS ====="
echo "$HOST_IP $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
