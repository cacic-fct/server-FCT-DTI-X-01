#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT

for dir in docker-compose compose-overlays; do
  if [ -d "$repo_root/$dir" ]; then
    mkdir -p "$tmp_root/$(dirname "$dir")"
    cp -R "$repo_root/$dir" "$tmp_root/$dir"
  fi
done

ci_env_file="$tmp_root/compose-ci.env"

python3 - "$tmp_root" "$ci_env_file" <<'PY'
from pathlib import Path
import re
import sys

import yaml

tmp_root = Path(sys.argv[1])
ci_env_file = Path(sys.argv[2])
compose_files = sorted(
    list((tmp_root / "docker-compose").glob("*/docker-compose.yml"))
    + list((tmp_root / "compose-overlays").glob("*/docker-compose.yml"))
)

variable_pattern = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)")
variables = set()

def env_file_paths(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        paths = []
        for item in value:
            if isinstance(item, str):
                paths.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(item["path"])
        return paths
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return [value["path"]]
    return []

for compose_file in compose_files:
    text = compose_file.read_text()
    variables.update(variable_pattern.findall(text))
    data = yaml.safe_load(text) or {}
    for service in (data.get("services") or {}).values():
        if not isinstance(service, dict):
            continue
        for env_path in env_file_paths(service.get("env_file")):
            path = (compose_file.parent / env_path).resolve()
            try:
                path.relative_to(tmp_root.resolve())
            except ValueError:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

def value_for(name):
    if name.endswith("_PORT") or name.startswith("COMPOSE_PORT_"):
        return "8080"
    if name.endswith("_URL") or name.endswith("_DISCOVERY"):
        return "https://example.invalid"
    if name.endswith("_LOCATION") or name.endswith("_PATH"):
        path = tmp_root / "compose-ci-paths" / name.lower()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    if name.endswith("_IMAGE"):
        return "docker.io/library/alpine"
    if name.endswith("_TAG") or name.endswith("_VERSION"):
        return "latest"
    return "ci-placeholder"

ci_env_file.write_text(
    "".join(f"{name}={value_for(name)}\n" for name in sorted(variables))
)
PY

compose_list="$tmp_root/compose-files.txt"
find "$tmp_root/docker-compose" "$tmp_root/compose-overlays" \
  -mindepth 2 -maxdepth 2 -name docker-compose.yml -print 2>/dev/null \
  | sort > "$compose_list"

if [ ! -s "$compose_list" ]; then
  echo "No Compose projects found." >&2
  exit 1
fi

while IFS= read -r compose_file; do
  project_dir="$(dirname "$compose_file")"
  display_dir="${project_dir#"$tmp_root/"}"
  echo "Validating $display_dir"
  (
    cd "$project_dir"
    docker compose --env-file "$ci_env_file" config --quiet
  )
done < "$compose_list"
