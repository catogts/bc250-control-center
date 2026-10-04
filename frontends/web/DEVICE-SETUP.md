# BC-250 device setup runbook (agent-executable)

End-to-end provisioning of a BC-250 / AMD "Strix Halo" device so the web
dashboard (`frontends/web/server.py`) reaches parity with Desktop Mode:
fans, GPU governor/SMU, CPU OC (QAM), and the live CU/WGP manager.
Written for an automation agent: every step is a command, and every step
has a verification marker. Nothing here is secret; generate your own
token, never commit one.

Applies to: Bazzite (and similar immutable RPM desktops) running the
`bc250-control-center` RPM, accessed as an unprivileged user over SSH.
Assumes the user is in a systemd user session (`loginctl` shows one).

## 0. Baseline

```bash
# app + layered packages (immutable: rpm-ostree; reboot once after layering)
rpm-ostree install ./bc250-control-center-<ver>.noarch.rpm umr stress
# cyan-skillfish-governor-smu ships with the app packaging on this OS family
```

Verify: `rpm -q bc250-control-center umr` and
`ls /usr/libexec/bc250-control-center/` shows `bc250-fan-pwm-helper`,
`bc250-governor-config-helper`, `bc250-cpu-smu-helper`, `bc250-cu-helper`.

## 1. Web server

```bash
# as the dashboard user
mkdir -p ~/bc250-web2 && cp -r frontends/web/static ~/bc250-web2/ && cp frontends/web/server.py ~/bc250-web2/
cat > ~/.config/bc250-web.env <<EOF   # 0600; token: openssl rand -hex 16
BC250_WEB_TOKEN=<generated>
BC250_WEB_ENABLE_WRITE=1
EOF
chmod 600 ~/.config/bc250-web.env
install -Dm644 frontends/web/deploy/bc250-web.service ~/.config/systemd/user/bc250-web.service
systemctl --user daemon-reload && systemctl --user enable --now bc250-web
```

Sudoers (root): render `frontends/web/deploy/bc250-web.sudoers.template`
with the real username and install to `/etc/sudoers.d/bc250-web` (0440,
`visudo -cf` first). The rules must stay **exact-path** NOPASSWD entries
for the four helpers only - never a wildcard command.

Verify: `curl -I http://<ip>:8088/` -> `200`; every `/api/*` without
`X-Auth: <token>` -> `401 {"error":"unauthorized"}` (this is correct for a
bare browser URL - the UI sends the header, reads also accept `?token=`).
Writes accept the header only, by design.

## 2. Fans (manual PWM needs the out-of-tree driver)

```bash
packaging/common/os-scripts/bazzite/prepare-dependencies.sh   # builds nct6687d
packaging/common/os-scripts/bazzite/prepare-fan-pwm.sh        # unlocks manual mode
```

Verify: `GET /api/fans` -> `manual_pwm_ready: true`, 8+ channels with
`duty` values, `sensor_drivers` contains `nct6687d` (the in-tree `nct6683`
binding reports an EC mode and refuses manual duty - if writes are
rejected with that note, step 2 did not run).

## 3. GPU governor / SMU

Config lives at `/etc/cyan-skillfish-governor-smu/config.toml`; writes go
through `bc250-governor-config-helper` (sudoers rule from step 1).

Verify: `GET /api/gpu` -> `available: true`, `service.active: "active"`.

## 4. CPU OC (QAM) - volatile per boot unless you opt in

The helper is `bc250-cpu-smu-helper`; verbs: `apply-live`,
`install-boot`, `disable-boot`, `detect-qam <mhz> <vid> <temp>`,
`qam-status`.

**Agent rule: never launch long device jobs with `setsid`/`nohup` over
SSH.** systemd kills the whole session cgroup when SSH exits (the detect
run dies mid-measurement with SIGTERM). Use transient units instead:

```bash
systemd-run --user --unit=bc250-qam-detect \
  sudo -n /usr/libexec/bc250-control-center/bc250-cpu-smu-helper detect-qam 3800 1250 90
journalctl --user -u bc250-qam-detect -f     # watch; takes minutes
```

Verify: `sudo -n .../bc250-cpu-smu-helper qam-status` ->
`runtime_snapshot_published: true`, and `GET /api/cpu` ->
`live.available: true`, `observed.current_mhz` near the detected value.
The OC is volatile this boot; `install-boot` persists it (operator
decision, not a default). Reboot resets to stock.

## 5. CU backend staging (Bazzite = generic path)

`bc250-steamos-game-helper.staged_cu_runtime()` picks the backend by
distro family: native SteamOS uses `/usr/libexec/bc250-control-center/`
(script + UMR database); everything else (Bazzite, CachyOS) uses the
generic staged file and the **packaged** umr database. Do not create the
SteamOS paths on Bazzite - they will never be consulted.

```bash
# user-owned checkout (where the desktop app also looks)
RT="$HOME/.local/share/bc250-control-center/ResourceTools"
git clone --depth 1 https://github.com/WinnieLV/bc250-cu-live-manager "$RT/bc250-cu-live-manager"
# root-owned staged backend (exact trust rules: regular file, root:root,
# no group/world write, no symlinks, parent dir owned by root)
sudo install -d -m 0755 -o root -g root /var/lib/bc250-control-center
sudo install -m 0755 -o root -g root "$RT/bc250-cu-live-manager/bc250-cu-live-manager.sh" \
     /var/lib/bc250-control-center/bc250-cu-live-manager
```

Verify: `sudo -n /usr/libexec/bc250-control-center/bc250-cu-helper status`
prints the ASCII dashboard: `ASIC : cyan_skillfish.gfx1013`,
`active_cu_number=24`, four `SEn.SHn` rows, `CUs active & routed : 24/40`.
Then `GET /api/cu` -> `dashboard.active/total/rows` populated, all three
`prerequisites` true.

## 6. Final checklist

Run all from the LAN (or the device's loopback) with the token header:

| probe | expected marker |
|---|---|
| `curl -sI http://<ip>:8088/ -o /dev/null -w '%{http_code}'` | `200` |
| `curl -s http://<ip>:8088/api/fans -H 'X-Auth: <tok>'` | `manual_pwm_ready: true` |
| `... /api/gpu` | `available: true`, `service.active` |
| `... /api/cpu` | `live.available: true` after step 4, `observed.current_mhz` |
| `... /api/cu` | `dashboard.active` 24, `rows` length 4, `write_enabled: true` |
| `... /api/capabilities` | all four helpers `installed: true` |
| `POST /api/cu {"op":"set","cu":24,"confirm":true,"expected_state":"<fresh _state_hash>"}` | `outcome: unchanged` (proves the full write pipeline with no hardware change) |
| `journalctl --user -u bc250-web -n 50` | no tracebacks |

A CU apply that actually moves registers (`cu` != current, or
`enable-all`) writes through `bc250-cu-helper` and re-verifies with a
fresh `status`; watch `GET /api/audit` for `outcome: applied` and the
helper exit code.

## 7. Design rules an agent must keep

* **Two-factor write gate**: token (`X-Auth` header only) *and*
  `BC250_WEB_ENABLE_WRITE=1`. Removing either closes writes; do not
  widen sudoers to compensate for a 401 - 401 means "no token on this
  request", not "sudo broken".
* **Confirm-before-write**: every write carries `expected_state` (the
  `_state_hash` of the read the operator actually saw). If a write fails
  409, re-read and re-confirm; never replay a stale hash.
* **CU confirm-basis order**: Quick Access snapshot
  (`/run/bc250-control-center/cu-live-state.json`, published only by
  game-mode/desktop changes) first, else the staged manager's four
  verified dashboard rows (direct SPI register readback). Never guess
  masks from a partially parsed table; the 409 refusal is the feature.
* **Volatile by default**: CPU OC and CU routing do not survive reboot.
  `install-boot` / `persist` / `write-service-table` change `/etc` and
  install boot services - only on explicit operator request.
* **Long jobs = transient units**: `systemd-run --user --unit=<name>`;
  `setsid`/`nohup` die with the SSH session cgroup.
* **Bazzite is not native SteamOS**: generic CU backend path, packaged
  umr database, no `/usr/libexec` UMR staging.
* Headless SSH has **no polkit agent**; that is why the web server uses
  exact-path sudoers rules instead of the desktop's graphical auth. Keep
  both lists in sync when helpers gain verbs.

## 8. Known non-problems

* `live: missing (No such file or directory)` for CU on a fresh boot is
  the truth, not an error - the snapshot file appears after the first
  real CU change; the dashboard tile shows `backend live` meanwhile.
* Opening `/api/...` directly in a browser gives `401` (no header); use
  the UI or add `?token=`.
* Fan counts/limits come from `bc250cc.shared.contract`; if the package
  version changes them, the web frontend follows automatically - do not
  fork the numbers.
