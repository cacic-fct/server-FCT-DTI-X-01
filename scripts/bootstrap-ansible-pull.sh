#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cacic-fct/server-FCT-DTI-X-01.git}"
BRANCH="${BRANCH:-production}"
CHECKOUT="${CHECKOUT:-/opt/ansible-pull/server-FCT-DTI-X-01}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root on FCT-DTI-X-01." >&2
  exit 1
fi

apt-get update
apt-get install -y ansible ca-certificates git python3 python3-apt
ansible-galaxy collection install community.docker

install -d -m 0755 "$(dirname "$CHECKOUT")"

ansible-pull \
  --url "$REPO_URL" \
  --checkout "$BRANCH" \
  --directory "$CHECKOUT" \
  --clean \
  --inventory "$CHECKOUT/inventory/hosts.yml" \
  site.yml
