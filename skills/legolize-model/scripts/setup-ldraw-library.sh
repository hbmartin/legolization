#!/usr/bin/env bash
# Install or update the managed official LDraw parts library.
#
# Thin wrapper: the download/validation/metadata/weekly logic lives in
# `legolization parts sync` so shell and PowerShell stay in lockstep.
# Silent and admin-free; a valid existing library survives a failed
# weekly check. Pass --force to re-download regardless of freshness.
set -euo pipefail

if ! command -v legolization >/dev/null 2>&1; then
  echo "legolization CLI not found; installing via uvx.sh..." >&2
  curl -LsSf https://uvx.sh/legolization/install.sh | sh
fi

if ! command -v legolization >/dev/null 2>&1; then
  echo "error: legolization installation failed; cannot set up the parts library" >&2
  exit 1
fi

exec legolization parts sync "$@"
