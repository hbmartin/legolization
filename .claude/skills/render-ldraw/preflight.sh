#!/usr/bin/env bash
# Install a headless LDraw renderer and parts library so render.py can run.
#
# Idempotent and best-effort: if a renderer and an LDraw parts library are
# already present it does nothing. On Debian/Ubuntu it installs LeoCAD, Xvfb
# (for headless OpenGL), and the ldraw-parts library from apt. On macOS it
# points you at LDView. Exits 0 when a renderer + library are available.
set -euo pipefail

have() { command -v "$1" >/dev/null 2>&1; }

ldraw_dir=""
for d in /usr/share/ldraw /usr/local/share/ldraw "$HOME/.ldraw" "$HOME/ldraw"; do
  if [ -d "$d/parts" ]; then ldraw_dir="$d"; break; fi
done

renderer=""
for r in ldview leocad; do
  if have "$r"; then renderer="$r"; break; fi
done

if [ -n "$renderer" ] && [ -n "$ldraw_dir" ]; then
  echo "ok: renderer=$renderer ldraw_dir=$ldraw_dir"
  exit 0
fi

os="$(uname -s)"
case "$os" in
  Linux)
    if ! have apt-get; then
      echo "error: only apt-based Linux is automated; install leocad + an" >&2
      echo "       LDraw parts library manually, then re-run." >&2
      exit 1
    fi
    sudo="" ; [ "$(id -u)" -ne 0 ] && sudo="sudo"
    echo "installing leocad, xvfb, ldraw-parts via apt ..."
    $sudo apt-get update -qq
    # ldraw-parts lives in the multiverse component.
    $sudo apt-get install -y --no-install-recommends leocad xvfb ldraw-parts
    ;;
  Darwin)
    echo "On macOS install LDView (has a headless -SaveSnapshot mode):" >&2
    echo "  brew install --cask ldview   # or download from tcobbs.github.io/ldview" >&2
    echo "LDView bundles a parts library; no separate install needed." >&2
    have ldview || exit 1
    ;;
  *)
    echo "error: unsupported OS '$os'; install leocad or ldview manually." >&2
    exit 1
    ;;
esac

# Re-detect after install so the summary reflects reality.
ldraw_dir=""
for d in /usr/share/ldraw /usr/local/share/ldraw "$HOME/.ldraw" "$HOME/ldraw"; do
  if [ -d "$d/parts" ]; then ldraw_dir="$d"; break; fi
done
renderer=""
for r in ldview leocad; do
  if have "$r"; then renderer="$r"; break; fi
done

if [ -z "$renderer" ]; then
  echo "error: no renderer on PATH after install." >&2
  exit 1
fi
echo "ok: renderer=$renderer ldraw_dir=${ldraw_dir:-<none, basic parts only>}"
