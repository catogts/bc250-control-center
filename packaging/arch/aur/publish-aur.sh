#!/usr/bin/env bash
# Update the AUR package bc250-control-center-git from this folder.
#
#   bash packaging/arch/aur/publish-aur.sh          # prepare and commit, no push
#   bash packaging/arch/aur/publish-aur.sh --push   # also push to the AUR
#
# Run it after the release commit is on GitHub's main branch: the package is
# built from that branch, so pkgver is computed from what users will get,
# not from this working tree. Needs an SSH key registered on the AUR
# account (https://aur.archlinux.org/account/ › SSH Public Key).
set -euo pipefail

AUR_PACKAGE="bc250-control-center-git"
AUR_REMOTE="${BC250_AUR_REMOTE:-ssh://aur@aur.archlinux.org/${AUR_PACKAGE}.git}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd -P)"
WORK="${BC250_AUR_WORKDIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bc250-control-center/aur/$AUR_PACKAGE}"
PUSH=0
case "${1:-}" in
  --push) PUSH=1 ;;
  "") ;;
  *) echo "Usage: $0 [--push]" >&2; exit 64 ;;
esac

for tool in git ssh makepkg; do
  command -v "$tool" >/dev/null || { echo "$tool is required." >&2; exit 69; }
done

echo "== Checking the AUR login =="
# The AUR answers "help" only to a registered key; anything else is a clear
# "not yet" rather than a clone that fails halfway.
if [[ "$AUR_REMOTE" == ssh://aur@aur.archlinux.org/* ]] \
  && ! ssh -o BatchMode=yes -o ConnectTimeout=15 aur@aur.archlinux.org help >/dev/null 2>&1; then
  cat >&2 <<'EOF'
The AUR did not accept an SSH login from this machine.
  1. ssh-keygen -t ed25519 -f ~/.ssh/aur -C "aur"
  2. Add to ~/.ssh/config:
       Host aur.archlinux.org
         IdentityFile ~/.ssh/aur
         User aur
  3. Paste ~/.ssh/aur.pub into https://aur.archlinux.org/account/ (Edit account ›
     SSH Public Key) and save.
  4. ssh aur@aur.archlinux.org help   (accept the host key after checking its
     fingerprint against the one on https://aur.archlinux.org)
EOF
  exit 77
fi

echo "== Preparing $WORK =="
if [[ -d "$WORK/.git" ]]; then
  git -C "$WORK" fetch --quiet origin
  git -C "$WORK" checkout --quiet master 2>/dev/null || true
  git -C "$WORK" reset --quiet --hard origin/master
else
  mkdir -p -- "$(dirname -- "$WORK")"
  git clone --quiet "$AUR_REMOTE" "$WORK"
fi
install -m644 "$SCRIPT_DIR/PKGBUILD" "$SCRIPT_DIR/bc250-control-center.install" "$WORK/"

echo "== Refreshing pkgver from GitHub's main branch =="
# --nobuild: fetch the sources and run pkgver() without building; it
# rewrites the pkgver line of PKGBUILD in place. The clone and the build
# tree go to a scratch folder so the AUR checkout holds only its files.
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT
(cd "$WORK" && SRCDEST="$scratch/sources" BUILDDIR="$scratch/build" \
  makepkg --nobuild --nodeps --cleanbuild >/dev/null)
pkgver="$(sed -n 's/^pkgver=//p' "$WORK/PKGBUILD")"
local_version="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
if [[ "$pkgver" != "$local_version".r* ]]; then
  echo "GitHub's main branch builds $pkgver, but VERSION here says $local_version." >&2
  echo "Push the release commit first, then run this again." >&2
  exit 65
fi
(cd "$WORK" && makepkg --printsrcinfo > .SRCINFO)

echo "== Changes for the AUR =="
git -C "$WORK" add PKGBUILD .SRCINFO bc250-control-center.install
if git -C "$WORK" diff --cached --quiet; then
  echo "The AUR already has $pkgver. Nothing to publish."
  exit 0
fi
git -C "$WORK" --no-pager diff --cached --stat
git -C "$WORK" commit --quiet -m "Update to $pkgver"
echo "Committed \"Update to $pkgver\" in $WORK"

if ((PUSH)); then
  git -C "$WORK" push origin HEAD:master
  echo "Published: https://aur.archlinux.org/packages/$AUR_PACKAGE"
else
  echo "Not pushed. Review it, then run: bash $0 --push"
fi
