#!/usr/bin/env bash
# Install Terraform — the one tool `make deploy` needs that isn't bundled.
#
# Idempotent: no-op if terraform is already on PATH. Single-binary install
# (no apt repo), so it works on any Linux/macOS. Retries the download a few
# times and cleans up partial artifacts after every failed attempt.
#
# Usage:
#   bash dev-utils/install-terraform.sh
#   TF_VERSION=1.10.5 bash dev-utils/install-terraform.sh    # pin a version
#   DEST=~/.local/bin bash dev-utils/install-terraform.sh    # install without sudo
#   MAX_ATTEMPTS=5   bash dev-utils/install-terraform.sh     # more retries
set -euo pipefail

TF_VERSION="${TF_VERSION:-1.9.8}"
DEST="${DEST:-/usr/local/bin}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"

if command -v terraform >/dev/null 2>&1; then
  echo "terraform already installed: $(terraform version | head -1)"
  exit 0
fi

# Pre-flight — these can never succeed on retry, so fail fast (no loop).
for tool in curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "!! '$tool' is required but not installed — run: sudo apt install -y $tool"
    exit 1
  }
done

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64|amd64)  arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "!! unsupported architecture: $(uname -m)"; exit 1 ;;
esac
url="https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_${os}_${arch}.zip"

# Clean up the temp dir on any exit (including Ctrl+C mid-attempt).
_TMP=""
trap 'rm -rf "${_TMP:-}"' EXIT

# sudo only when DEST isn't writable by the current user.
maybe_sudo() { if [ -w "$DEST" ]; then "$@"; else sudo "$@"; fi; }

# One install attempt. Returns non-zero on failure, leaving no partial
# artifacts behind (temp files removed; a broken binary removed).
attempt_install() {
  _TMP="$(mktemp -d)"

  echo "  downloading ${url}"
  if ! curl -fsSL "$url" -o "$_TMP/terraform.zip"; then
    echo "  download failed"; rm -rf "$_TMP"; _TMP=""; return 1
  fi
  if ! unzip -o "$_TMP/terraform.zip" -d "$_TMP" >/dev/null 2>&1; then
    echo "  unzip failed (corrupt/incomplete download)"; rm -rf "$_TMP"; _TMP=""; return 1
  fi

  mkdir -p "$DEST"
  if ! maybe_sudo mv "$_TMP/terraform" "$DEST/terraform"; then
    echo "  install to ${DEST} failed"; rm -rf "$_TMP"; _TMP=""; return 1
  fi
  maybe_sudo chmod +x "$DEST/terraform" || true
  rm -rf "$_TMP"; _TMP=""

  # Test: the installed binary must actually run.
  if ! "$DEST/terraform" version >/dev/null 2>&1; then
    echo "  verification failed — removing broken binary"
    maybe_sudo rm -f "$DEST/terraform"
    return 1
  fi
  return 0
}

n=1
while [ "$n" -le "$MAX_ATTEMPTS" ]; do
  echo "── attempt ${n}/${MAX_ATTEMPTS} ──────────────────────────────"
  if attempt_install; then
    echo "Installed + verified: $("$DEST/terraform" version | head -1)"
    command -v terraform >/dev/null 2>&1 || echo "Note: ensure ${DEST} is on your PATH."
    exit 0
  fi
  echo "  attempt ${n} failed — cleaned up."
  n=$((n + 1))
  [ "$n" -le "$MAX_ATTEMPTS" ] && { echo "  retrying in 3s..."; sleep 3; }
done

echo "!! terraform install failed after ${MAX_ATTEMPTS} attempts."
echo "   Check network access to releases.hashicorp.com and that"
echo "   TF_VERSION=${TF_VERSION} exists. Nothing partial was left behind."
exit 1
