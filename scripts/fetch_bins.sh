#!/usr/bin/env bash
# Download the KILLDPI xray core (latest core-build-* release of CORE_REPO) and cloudflared.
# Usage: scripts/fetch_bins.sh <owner/repo> <outdir>
set -euo pipefail
REPO="${1:-fjxjxjjdjddjjd042-rgb/KILLDPI}"; OUT="${2:-bin}"
mkdir -p "$OUT"
AUTH=()
[ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GH_TOKEN")
ARCH="$(uname -m)"; case "$ARCH" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) echo "unsupported arch $ARCH"; exit 1;; esac

TAG="$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/releases?per_page=30" \
  | python3 -c 'import sys,json; rs=[r["tag_name"] for r in json.load(sys.stdin) if r["tag_name"].startswith("core-build-")]; print(rs[0] if rs else "")')"
[ -n "$TAG" ] || { echo "no core-build release in $REPO"; exit 1; }
echo "core release: $TAG"
curl -fsSL -o "$OUT/xray" "https://github.com/$REPO/releases/download/$TAG/xray-linux-$A"
chmod +x "$OUT/xray"
"$OUT/xray" version | head -1
"$OUT/xray" killdpi-keygen >/dev/null && echo "killdpi protocol: OK"

curl -fsSL -o "$OUT/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A"
chmod +x "$OUT/cloudflared"
"$OUT/cloudflared" --version
