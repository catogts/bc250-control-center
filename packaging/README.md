# builders

These scripts create reviewable artifacts only; they never install packages,
modify the host, publish a release, or invoke hardware helpers.

- `scripts/build-tarball.sh [output-dir]` creates a deterministic source archive.
- `scripts/build-local-pkg.sh [output-dir]` creates an Arch-compatible local package.
- `scripts/build-rpm.sh [output-dir]` creates the single noarch RPM used by
  Fedora, Nobara and Bazzite/Fedora Atomic.
- `scripts/build-deb.sh [output-dir]` creates the Ubuntu/Debian `all` package.
- `scripts/build-release.sh [output-dir]` runs all four into `dist/<version>/`
  and writes `SHA256SUMS.txt`: the files a GitHub release carries. Without
  `dpkg-deb` it runs only that tool in a local `debian:trixie` podman image.

## Publishing a release

1. Bump `VERSION`, the `version` of `integrations/decky/bc250-quick-access/package.json`
   and add a `<release>` to the AppStream metainfo (the builders refuse a mismatch).
2. `bash packaging/scripts/build-release.sh`
3. Commit and push to `main`, then create the GitHub release `v<version>` and
   attach every file in `dist/<version>/`. The application's update check
   reads `VERSION` on `main`, so publish the release right after the push.
4. `bash packaging/arch/aur/publish-aur.sh` prepares the AUR update of
   `bc250-control-center-git` (pkgver from the pushed `main`, `.SRCINFO`, commit);
   `--push` publishes it. It needs an SSH key registered on the AUR account.

Install the Debian artifact with
`sudo apt install ./bc250-control-center_*.deb`; APT resolves the PyQt6, Qt SVG,
psutil and polkit runtime packages. `dpkg -i` alone does not download missing
dependencies. If it leaves an installation unconfigured, run
`sudo apt --fix-broken install`.

Package upgrades remove the retired 1.18 `mvc/` application tree after the
new payload is installed. Package removal never deletes files from user home
directories; use the local uninstaller's explicit `--purge-user-data` option
when that cleanup is wanted.

The optional FSR4 V3 action is not a package dependency. On Fedora 44, Debian,
Ubuntu and their derivatives, Control Center installs missing source-build
tools with DNF or APT only after the user requests that action, builds the
official upstream `v3` branch in its Fedora 44 container with rootless Podman,
and installs only a per-user Vulkan ICD for explicitly selected games. Fedora
requires the repaired GFX1013 boot to be active first. The matching small
libdrm runtime is kept inside that private directory so distribution libdrm
versions are not replaced. It never replaces system Mesa or libdrm.

Once the runtime is validated, the FSR4 card exposes a compact copy button for
the per-game Steam launch option. The copied value uses `$HOME` instead of an
account name and therefore works unchanged for every user. Source-built
Debian/Ubuntu and Bazzite runtimes include their private `LD_LIBRARY_PATH`;
Arch/CachyOS prebuilt runtimes need only `VK_DRIVER_FILES`.
