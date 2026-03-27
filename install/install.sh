#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${GHOST_REPO_URL:-https://github.com/denisalbertofx/ghostllm.git}"
INSTALL_DIR="${GHOST_INSTALL_DIR:-$HOME/.ghostllm}"
BIN_DIR="${GHOST_BIN_DIR:-$HOME/.local/bin}"
BRANCH="${GHOST_BRANCH:-master}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd git
need_cmd python3
need_cmd uv

resolve_install_dir() {
  python3 - "$1" <<'PY'
import os, sys
raw = sys.argv[1]
if not raw or raw.strip() in {"", ".", "/", "~"}:
    raise SystemExit(1)
print(os.path.realpath(os.path.expanduser(raw)))
PY
}

safe_install_dir() {
  local resolved="$1"
  local home_resolved
  home_resolved="$(python3 - <<'PY'
import os
print(os.path.realpath(os.path.expanduser("~")))
PY
)"
  [[ -n "$resolved" ]] || return 1
  [[ "$resolved" != "/" ]] || return 1
  [[ "$resolved" != "$home_resolved" ]] || return 1
  case "$resolved" in
    "$home_resolved"/*) return 0 ;;
    *) return 1 ;;
  esac
}

INSTALL_DIR="$(resolve_install_dir "$INSTALL_DIR")"
if ! safe_install_dir "$INSTALL_DIR"; then
  echo "Refusing unsafe install dir: $INSTALL_DIR" >&2
  exit 1
fi

mkdir -p "$BIN_DIR"

if [[ -d "$INSTALL_DIR" && -d "$INSTALL_DIR/.git" ]]; then
  current_remote="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  if [[ "$current_remote" != "$REPO_URL" ]]; then
    echo "Refusing to update unrelated git checkout in $INSTALL_DIR" >&2
    exit 1
  fi
  echo "Updating GhostLLM in $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --all --tags
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only
else
  if [[ -e "$INSTALL_DIR" ]]; then
    if [[ ! -d "$INSTALL_DIR" ]]; then
      echo "Install path exists and is not a directory: $INSTALL_DIR" >&2
      exit 1
    fi
    if [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
      echo "Install path exists and is not an approved GhostLLM checkout: $INSTALL_DIR" >&2
      exit 1
    fi
    rmdir "$INSTALL_DIR"
  fi
  echo "Cloning GhostLLM into $INSTALL_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
uv sync

cat > "$BIN_DIR/ghost" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
exec uv run python apps/cli/main.py "\$@"
EOF

chmod +x "$BIN_DIR/ghost"

cat <<EOF

GhostLLM installed.

Binary:
  $BIN_DIR/ghost

If '$BIN_DIR' is not in your PATH, add this line to your shell profile:
  export PATH="$BIN_DIR:\$PATH"

Next steps:
  ghost init
  ghost start
  ghost doctor
  ghost codex

For updates, run:
  ghost update
EOF
