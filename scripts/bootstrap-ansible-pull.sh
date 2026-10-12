#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cacic-fct/server-FCT-DTI-X-01.git}"
BRANCH="${BRANCH:-production}"
CHECKOUT="${CHECKOUT:-/opt/ansible-pull/server-FCT-DTI-X-01}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root on FCT-DTI-X-01." >&2
  exit 1
fi

retry_helper="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/network_retry.py"
export NETWORK_RETRY_ATTEMPTS="${NETWORK_RETRY_ATTEMPTS:-10}"
export NETWORK_RETRY_DELAY_SECONDS="${NETWORK_RETRY_DELAY_SECONDS:-30}"
if [[ ! "$NETWORK_RETRY_ATTEMPTS" =~ ^[1-9][0-9]*$ || ! "$NETWORK_RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Invalid network retry limits." >&2
  exit 1
fi
# Python is normally present on Debian; use a small shell retry for its bootstrap.
retry_bootstrap() {
  local attempt status
  for ((attempt = 1; attempt <= NETWORK_RETRY_ATTEMPTS; attempt++)); do
    if "$@"; then
      return 0
    else
      status=$?
    fi
    if ((attempt == NETWORK_RETRY_ATTEMPTS)); then
      return "$status"
    fi
    echo "Bootstrap network operation failed ($attempt/$NETWORK_RETRY_ATTEMPTS); retrying" >&2
    sleep "$NETWORK_RETRY_DELAY_SECONDS"
  done
}

retry_bootstrap apt-get update --error-on=any
retry_bootstrap apt-get install -y ansible ca-certificates git python3 python3-apt
python3 "$retry_helper" command ansible-galaxy collection install community.docker

install -d -m 0755 "$(dirname "$CHECKOUT")"

python3 "$retry_helper" pull /usr/bin/ansible-pull \
  --url "$REPO_URL" \
  --checkout "$BRANCH" \
  --directory "$CHECKOUT" \
  --clean \
  --inventory "$CHECKOUT/inventory/hosts.yml" \
  site.yml
