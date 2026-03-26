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

mkdir -p "$BIN_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "Updating GhostLLM in $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --all --tags
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only
else
  rm -rf "$INSTALL_DIR"
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
