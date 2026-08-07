#!/usr/bin/env bash
# Detect or install an LDraw renderer.
#
# --check only reports what is available (renderer=<name|NONE>); the
# conversational skill asks the user before running the install mode.
# Platform policy: LDView via Homebrew on macOS, LeoCAD via winget on
# Windows (see setup-renderer.ps1), LeoCAD plus Xvfb via apt on Ubuntu.
set -euo pipefail

detect() {
  if [ -n "${LEGOLIZATION_RENDERER:-}" ] && [ "${LEGOLIZATION_RENDERER}" != "none" ]; then
    echo "${LEGOLIZATION_RENDERER}"
    return 0
  fi
  for name in ldview leocad; do
    if command -v "$name" >/dev/null 2>&1; then
      echo "$name"
      return 0
    fi
  done
  if [ -x "/Applications/LDView.app/Contents/MacOS/LDView" ]; then
    echo "ldview"
    return 0
  fi
  if [ -x "/Applications/LeoCAD.app/Contents/MacOS/LeoCAD" ]; then
    echo "leocad"
    return 0
  fi
  return 1
}

if [ "${1:-}" = "--check" ]; then
  if renderer="$(detect)"; then
    echo "renderer=${renderer}"
    exit 0
  fi
  echo "renderer=NONE"
  exit 1
fi

if renderer="$(detect)"; then
  echo "renderer=${renderer} (already installed)"
  exit 0
fi

case "$(uname -s)" in
  Darwin)
    if ! command -v brew >/dev/null 2>&1; then
      echo "error: Homebrew is required to install LDView on macOS." >&2
      echo "Install it from https://brew.sh or LDView from https://tcobbs.github.io/ldview/" >&2
      exit 1
    fi
    brew install --cask ldview
    ;;
  Linux)
    if ! command -v apt-get >/dev/null 2>&1; then
      echo "error: automatic install supports apt-based systems only." >&2
      echo "Install LeoCAD and Xvfb with your distribution's package manager." >&2
      exit 1
    fi
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends leocad xvfb
    ;;
  *)
    echo "error: unsupported platform $(uname -s); on Windows run setup-renderer.ps1" >&2
    exit 1
    ;;
esac

if renderer="$(detect)"; then
  echo "renderer=${renderer}"
  exit 0
fi
echo "error: renderer installation completed but no renderer was detected" >&2
exit 1
