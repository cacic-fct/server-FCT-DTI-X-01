#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run the apt preflight as root." >&2
  exit 1
fi

lock_timeout=600

audit_output="$(dpkg --audit 2>&1)"
if [[ -z "${audit_output//[[:space:]]/}" ]]; then
  echo "dpkg package state is clean."
  exit 0
fi

printf 'Incomplete dpkg setup detected:\n%s\n' "$audit_output" >&2
printf 'Running dpkg --configure -a before apt operations.\n' >&2

elapsed=0
# Preserve existing local conffile changes during automatic recovery.
while true; do
  if configure_output="$(
    LC_ALL=C DEBIAN_FRONTEND=noninteractive \
      dpkg --force-confold --configure -a 2>&1
  )"; then
    if [[ -n "$configure_output" ]]; then
      printf '%s\n' "$configure_output"
    fi
    break
  else
    configure_status=$?
  fi

  if [[ "$configure_output" != *lock* && "$configure_output" != *"another process"* ]]; then
    printf '%s\n' "$configure_output" >&2
    exit "$configure_status"
  fi

  if ((elapsed >= lock_timeout)); then
    printf 'Timed out waiting for dpkg locks after %s seconds.\n%s\n' \
      "$lock_timeout" "$configure_output" >&2
    exit "$configure_status"
  fi

  sleep 5
  ((elapsed += 5))
done

audit_output="$(dpkg --audit 2>&1)"
if [[ -n "${audit_output//[[:space:]]/}" ]]; then
  printf 'dpkg setup remains incomplete; blocking apt operations:\n%s\n' "$audit_output" >&2
  exit 1
fi

echo "Recovered incomplete dpkg setup."
