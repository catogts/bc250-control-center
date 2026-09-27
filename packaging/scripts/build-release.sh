#!/usr/bin/env bash
# Build every file a GitHub release carries, into one folder, with SHA256SUMS.txt.
#
#   bash packaging/scripts/build-release.sh [output-dir]
#
# The version comes from the VERSION file; the output defaults to
# dist/<version>/. Like the builders it calls, it never installs anything,
# never publishes and never touches hardware.
#
# Arch (.pkg.tar.zst) and the source archive build on any host with zstd.
# The RPM needs rpmbuild (Arch: rpm-tools). The Debian package needs dpkg-deb
# (Arch: dpkg); without it, and with podman and a local debian image, only
# dpkg-deb itself runs inside that container: the payload is still staged
# and checked on this host. A package that cannot be built is reported and
# the rest are still produced; the exit status says whether all four were.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
OUTPUT_DIR="$(realpath -m -- "${1:-$ROOT_DIR/dist/$VERSION}")"
DEBIAN_IMAGE="${BC250_DEBIAN_IMAGE:-docker.io/library/debian:trixie}"

[[ "$VERSION" =~ ^[0-9]+([.][0-9A-Za-z]+)*$ ]] || { echo "Invalid release version: $VERSION" >&2; exit 64; }
[[ "$OUTPUT_DIR" != / ]] || { echo "Refusing to build into /" >&2; exit 64; }

echo "== BC250 Control Center $VERSION: release artifacts =="
echo "Output: $OUTPUT_DIR"
if command -v git >/dev/null && git -C "$ROOT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "[WARN] The working tree has uncommitted changes; they go into these packages."
  fi
  echo "Commit: $(git -C "$ROOT_DIR" rev-parse --short HEAD)"
fi
mkdir -p -- "$OUTPUT_DIR"
# A previous run for this version must not leave a stale file to upload.
rm -f -- "$OUTPUT_DIR"/bc250-control-center[-_]"$VERSION"* "$OUTPUT_DIR/SHA256SUMS.txt"

built=()
missing=()

step() {
  local label="$1"; shift
  echo
  echo "-- $label"
  if "$@"; then
    built+=("$label")
  else
    missing+=("$label")
    echo "[ERROR] $label was not built." >&2
  fi
}

deb_with_container() {
  # A stand-in dpkg-deb that runs the real one in the Debian image. Both the
  # staging directory and the output folder are mounted at the same paths,
  # so build-deb.sh passes its own arguments through unchanged.
  local shim
  shim="$(mktemp -d /tmp/bc250-dpkg-shim.XXXXXX)"
  cat > "$shim/dpkg-deb" <<EOF
#!/usr/bin/env bash
exec podman run --rm --network=none \\
  -e SOURCE_DATE_EPOCH -v /tmp:/tmp -v "$OUTPUT_DIR:$OUTPUT_DIR" \\
  "$DEBIAN_IMAGE" dpkg-deb "\$@"
EOF
  chmod 755 "$shim/dpkg-deb"
  local status=0
  PATH="$shim:$PATH" bash "$SCRIPT_DIR/build-deb.sh" "$OUTPUT_DIR" || status=$?
  rm -rf -- "$shim"
  return "$status"
}

step "Source archive (.tar.gz)" bash "$SCRIPT_DIR/build-tarball.sh" "$OUTPUT_DIR"
step "Arch / CachyOS / Manjaro (.pkg.tar.zst)" bash "$SCRIPT_DIR/build-local-pkg.sh" "$OUTPUT_DIR"

if command -v rpmbuild >/dev/null; then
  step "Fedora / Nobara / Bazzite (.rpm)" bash "$SCRIPT_DIR/build-rpm.sh" "$OUTPUT_DIR"
else
  echo
  echo "[SKIP] .rpm: rpmbuild is not installed (Arch: sudo pacman -S rpm-tools)." >&2
  missing+=(".rpm")
fi

if command -v dpkg-deb >/dev/null; then
  step "Ubuntu / Debian (.deb)" bash "$SCRIPT_DIR/build-deb.sh" "$OUTPUT_DIR"
elif command -v podman >/dev/null && podman image exists "$DEBIAN_IMAGE" 2>/dev/null; then
  step "Ubuntu / Debian (.deb, dpkg-deb in $DEBIAN_IMAGE)" deb_with_container
else
  echo
  echo "[SKIP] .deb: dpkg-deb is not installed (Arch: sudo pacman -S dpkg)." >&2
  missing+=(".deb")
fi

# The builders leave one .sha256 per file; the release carries a single list.
rm -f -- "$OUTPUT_DIR"/*.sha256
(
  cd -- "$OUTPUT_DIR"
  shopt -s nullglob
  artifacts=(bc250-control-center[-_]"$VERSION"*)
  if ((${#artifacts[@]})); then
    sha256sum -- "${artifacts[@]}" > SHA256SUMS.txt
  fi
)

echo
echo "== Files to attach to the GitHub release v$VERSION =="
(cd -- "$OUTPUT_DIR" && ls -1 bc250-control-center[-_]"$VERSION"* SHA256SUMS.txt 2>/dev/null | sed 's/^/  /')
if ((${#missing[@]})); then
  echo
  echo "Not built: ${missing[*]}" >&2
  exit 1
fi
echo
echo "All four packages were built."
