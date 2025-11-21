#!/usr/bin/env bash
# set-env.sh — MSYS2 Bash helper for Xpra Windows packaging
# Usage:
#   source ./set-env.sh [--persist] [--inno "C:/.../ISCC.exe"] [--signtool "C:/.../signtool.exe"] \
#                       [--verpatch "D:/.../verpatch.exe"] [--tools-dir "/d/project/.../tools"] \
#                       [--upx "C:/.../upx.exe"] [--7z "C:/.../7z.exe"]
#
# Notes:
# - Run with `source` so exports affect your current shell.
# - Paths can be in /c/Program Files/... (MSYS style) or C:/Program Files/... (Windows style).
# - If not provided, common locations will be auto-detected.

set -euo pipefail

# ---------- helpers ----------
say() { printf "%b\n" "$*"; }
ok()  { say "\e[32m✔\e[0m $*"; }
warn(){ say "\e[33m⚠\e[0m $*"; }
err() { say "\e[31m✖\e[0m $*"; }
path_exists() { [ -f "$1" ] || [ -x "$1" ]; }
to_msys_path() {
  # Accept both "C:\x\y.exe" and "C:/x/y.exe" and "/c/x/y.exe"
  case "$1" in
    /[c-zC-Z]/*) echo "$1" ;;
    [cC]:\\*)    cygpath -u "$1" 2>/dev/null || echo "$1" ;;
    [cC]:/*)     cygpath -u "$1" 2>/dev/null || echo "$1" ;;
    *)           echo "$1" ;;
  esac
}

append_path_once() {
  local dir="$1"
  case ":$PATH:" in
    *":$dir:"*) ;; # already in PATH
    *) export PATH="$dir:$PATH" ;;
  esac
}

persist_line() {
  local line="$1"
  local rc="$HOME/.bashrc"
  if ! grep -Fqx "$line" "$rc" 2>/dev/null; then
    printf "%s\n" "$line" >> "$rc"
  fi
}

# ---------- defaults & args ----------
PERSIST=0
INNOSETUP="${INNOSETUP-}"
SIGNTOOL="${SIGNTOOL-}"
VERPATCH="${VERPATCH-}"
UPX="${UPX-}"
SEVENZIP="${SEVENZIP-}"
TOOLS_DIR="${TOOLS_DIR-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --persist) PERSIST=1; shift ;;
    --inno)    INNOSETUP="$(to_msys_path "$2")"; shift 2 ;;
    --signtool)SIGNTOOL="$(to_msys_path "$2")"; shift 2 ;;
    --verpatch)VERPATCH="$(to_msys_path "$2")"; shift 2 ;;
    --upx)     UPX="$(to_msys_path "$2")"; shift 2 ;;
    --7z|--7zip) SEVENZIP="$(to_msys_path "$2")"; shift 2 ;;
    --tools-dir) TOOLS_DIR="$(to_msys_path "$2")"; shift 2 ;;
    *) warn "Unknown arg: $1 (ignored)"; shift ;;
  esac
done

# Infer PROJECT_ROOT as the repository root (2 levels up from this script)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

# Default tools dir to repo's packaging tools
if [ -z "${TOOLS_DIR}" ]; then
  TOOLS_DIR="$PROJECT_ROOT/packaging/MSWindows/tools"
fi

# Ensure tools dir in PATH
if [ -d "$TOOLS_DIR" ]; then
  append_path_once "$TOOLS_DIR"
  ok "Added tools dir to PATH: $TOOLS_DIR"
else
  warn "Tools dir not found: $TOOLS_DIR"
fi

# ---------- detect INNOSETUP (ISCC.exe) ----------
detect_inno() {
  local candidates=(
    "/c/Program Files (x86)/Inno Setup 6/ISCC.exe"
    "/c/Program Files/Inno Setup 6/ISCC.exe"
    "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"
    "C:/Program Files/Inno Setup 6/ISCC.exe"
  )
  for c in "${candidates[@]}"; do
    local p="$(to_msys_path "$c")"
    if path_exists "$p"; then echo "$p"; return 0; fi
  done
  return 1
}

if [ -z "$INNOSETUP" ]; then
  if INNO=$(detect_inno); then
    INNOSETUP="$INNO"
  fi
fi

if [ -n "$INNOSETUP" ] && path_exists "$INNOSETUP"; then
  export INNOSETUP
  ok "INNOSETUP = $INNOSETUP"
else
  err "Inno Setup (ISCC.exe) not found. Set with: --inno \"C:/.../ISCC.exe\""
fi

# ---------- detect SIGNTOOL (optional) ----------
detect_signtool() {
  # Try Windows 10/11 SDK common paths; pick latest if multiple
  local roots=(
    "/c/Program Files (x86)/Windows Kits/10/bin"
    "C:/Program Files (x86)/Windows Kits/10/bin"
  )
  local found=""
  for r in "${roots[@]}"; do
    local rr="$(to_msys_path "$r")"
    if [ -d "$rr" ]; then
      # find latest x64 signtool.exe
      local cand
      cand="$(ls -1d "$rr"/*/x64 2>/dev/null | sort -V | tail -n 1 || true)"
      if [ -n "$cand" ] && [ -f "$cand/signtool.exe" ]; then
        found="$cand/signtool.exe"
      fi
    fi
  done
  if [ -n "$found" ]; then echo "$found"; return 0; fi
  return 1
}

if [ -z "$SIGNTOOL" ]; then
  if ST=$(detect_signtool); then
    SIGNTOOL="$ST"
  fi
fi

if [ -n "${SIGNTOOL}" ] && path_exists "$SIGNTOOL"; then
  export SIGNTOOL
  ok "SIGNTOOL = $SIGNTOOL"
else
  warn "signtool.exe not found (optional). Provide with --signtool or install Windows SDK."
fi

# ---------- detect VERPATCH (optional but recommended) ----------
# Try tools dir first
if [ -z "$VERPATCH" ]; then
  for c in "$TOOLS_DIR/verpatch.exe" "$TOOLS_DIR/VerPatch.exe"; do
    if path_exists "$c"; then VERPATCH="$c"; break; fi
  done
fi
if [ -n "$VERPATCH" ] && path_exists "$VERPATCH"; then
  export VERPATCH
  append_path_once "$(dirname "$VERPATCH")"
  ok "VERPATCH = $VERPATCH"
else
  warn "verpatch.exe not found (optional). Put it in: $TOOLS_DIR or pass --verpatch."
fi

# ---------- detect UPX (optional) ----------
if [ -z "$UPX" ]; then
  for c in \
    "/c/Program Files/UPX/upx.exe" \
    "C:/Program Files/UPX/upx.exe" \
    "/c/Program Files (x86)/UPX/upx.exe" \
    "C:/Program Files (x86)/UPX/upx.exe" \
    "$TOOLS_DIR/upx.exe"
  do
    c="$(to_msys_path "$c")"
    if path_exists "$c"; then UPX="$c"; break; fi
  done
fi
if [ -n "$UPX" ] && path_exists "$UPX"; then
  export UPX
  ok "UPX = $UPX"
else
  warn "UPX not found (optional). Provide with --upx if you enable UPX compression."
fi

# ---------- detect 7-Zip (optional) ----------
if [ -z "$SEVENZIP" ]; then
  for c in \
    "/c/Program Files/7-Zip/7z.exe" \
    "C:/Program Files/7-Zip/7z.exe" \
    "$TOOLS_DIR/7z.exe"
  do
    c="$(to_msys_path "$c")"
    if path_exists "$c"; then SEVENZIP="$c"; break; fi
  done
fi
if [ -n "$SEVENZIP" ] && path_exists "$SEVENZIP"; then
  export SEVENZIP
  ok "7-Zip = $SEVENZIP"
else
  warn "7-Zip CLI (7z.exe) not found (optional). Provide with --7z if needed."
fi

# ---------- final PATH + summary ----------
append_path_once "$PROJECT_ROOT/packaging/MSWindows/tools"

say ""
say "Env summary:"
say "  INNOSETUP : ${INNOSETUP:-<missing>}"
say "  SIGNTOOL  : ${SIGNTOOL:-<missing>}"
say "  VERPATCH  : ${VERPATCH:-<missing>}"
say "  UPX       : ${UPX:-<missing>}"
say "  7-Zip     : ${SEVENZIP:-<missing>}"
say "  TOOLS_DIR : ${TOOLS_DIR:-<missing>}"
say "  PATH prep : $(command -v verpatch >/dev/null 2>&1 && echo yes || echo no) (verpatch in PATH)"

# ---------- persist to ~/.bashrc if requested ----------
if [ "$PERSIST" -eq 1 ]; then
  say ""
  say "Persisting to ~/.bashrc ..."
  [ -n "$INNOSETUP" ]  && persist_line "export INNOSETUP=\"$INNOSETUP\""
  [ -n "$SIGNTOOL" ]   && persist_line "export SIGNTOOL=\"$SIGNTOOL\""
  [ -n "$VERPATCH" ]   && persist_line "export VERPATCH=\"$VERPATCH\""
  [ -n "$UPX" ]        && persist_line "export UPX=\"$UPX\""
  [ -n "$SEVENZIP" ]   && persist_line "export SEVENZIP=\"$SEVENZIP\""
  persist_line "export PATH=\"$TOOLS_DIR:\$PATH\""
  ok "Appended exports to ~/.bashrc (open a new shell or 'source ~/.bashrc')."
fi

ok "Done. You can now run: python packaging/MSWindows/BUILD.py --clean --build --install"
