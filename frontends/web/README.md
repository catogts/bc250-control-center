# BC250 Control Center — web frontend

Stdlib-only HTTP frontend for a BC-250: a JSON API plus a single-page dashboard
that reaches the same information the desktop and the headless CLI show, and can
perform the same hardware writes — but only through the typed, root-owned
privileged helpers the desktop uses. No new Python dependency, no generic
command runner, no root for reads.

    frontends/web/server.py        API + static file server (http.server)
    frontends/web/static/index.html  the dashboard (plain JS, dark theme)
    frontends/web/deploy/          installer, systemd user unit, sudoers template

## Run

The one-command install on the device (package already installed):

    $ bash frontends/web/deploy/install-web.sh

It creates the token file, the exact-path sudoers rule for the installing
account (rendered through `visudo -cf`), the systemd user unit and linger,
then prints the URL and the token. Re-running it is safe; `--rotate-token`
replaces the token. To try it without installing anything:

    BC250_WEB_TOKEN=$(openssl rand -hex 16) BC250_WEB_PORT=8088 \
      BC250_WEB_ENABLE_WRITE=1 python3 frontends/web/server.py

Open `http://<device-ip>:8088`. If a token is set, paste it into the field in
the page header (kept in `localStorage`, never in the URL for writes).

| env | default | meaning |
| --- | --- | --- |
| `BC250_WEB_BIND` | `0.0.0.0` | listen address; `127.0.0.1` for a reverse proxy |
| `BC250_WEB_PORT` | `8089` | listen port (the deployed service uses `8088`) |
| `BC250_WEB_TOKEN` | empty | when set, every `/api/` call needs it; writes need it |
| `BC250_WEB_CLI` | `bc250-control-center-cli` | CLI used for the CLI-backed reads |
| `BC250_WEB_ENABLE_WRITE` | `0` | `1` unlocks the seven `POST` endpoints |

## Auth and the write gate

* Reads accept `X-Auth: <token>` **or** `?token=<token>`.
* Writes accept the **header only**. A token that leaked into a link, a log line
  or a `Referer` cannot mutate hardware.
* Writes additionally require `BC250_WEB_ENABLE_WRITE=1` **and** a non-empty
  token. With no token configured the server is read-only by construction —
  "authenticated" is the whole authorization model for a network listener.
* Writes require `confirm: true` in the body and `expected_state` (below).
* `GET /api/health` is the one unauthenticated route (liveness for systemd).

## Reads

CLI-backed (`bc250-control-center-cli --json <cmd> [fixed args]`) — the argument
list is fixed by the server, the request body is never consulted:

| endpoint | CLI arguments | cache |
| --- | --- | --- |
| `/api/telemetry` | — | 2 s |
| `/api/system` | — | 30 s |
| `/api/components` | — | 30 s |
| `/api/quick-access` | — | 30 s |
| `/api/integrations` | `--runtime` | 60 s |
| `/api/release-gates` | — | 120 s |
| `/api/recovery` | `list` | 30 s |
| `/api/metrics` | `list --limit 50` | 10 s |

Local reads (no subprocess, no root — world-readable state and config only):

| endpoint | source |
| --- | --- |
| `/api/fans` | `nct668x` sysfs: duty, enable, RPM, label, loaded sensor drivers |
| `/api/gpu` | `/etc/cyan-skillfish-governor-smu/config.toml` (parsed + commented-key scan) and the governor unit state |
| `/api/cpu` | `/run/bc250-control-center/cpu-live-state.json` + `/etc/bc250-smu-oc.conf` |
| `/api/cu` | `/run/bc250-control-center/cu-live-state.json`, re-validated rather than trusted |
| `/api/capabilities` | version, contract source, auth/write state, helper install status, all domain limits |
| `/api/audit` | the in-memory log of every write attempt, including refusals |
| `/api/profiles` | a fresh `profiles export` bundle (config + profile sections), hashed over content only |

A non-zero CLI exit does not hide the payload: `integrations` answers `3` when a
manifest has a problem, and that body is still what the operator wants to read
(it is passed through with `cli_exit_code`).

## Writes

`POST /api/<kind>` with a JSON body. Every write is
`gate → typed validation → stale check → sudo -n <exact helper path> → audit`.

| endpoint | body | helper argv |
| --- | --- | --- |
| `/api/fan` | `{op:"set", channel:1..12, value:0..255}` or `percent:0..100`; `{op:"auto", channel}`; `{op:"preset", channel, preset:"quiet\|balanced\|cooling\|maximum"}` | stdin session to `bc250-fan-pwm-helper`: `"<ch> <duty>"` / `"AUTO <ch>"`, then `EXIT` |
| `/api/gpu` | `{action:"set-frequency-range", min_mhz, max_mhz}` · `set-frequency-floor` · `clear-frequency-range` · `set-high-points {enabled:bool}` · `set-voltage-level {level:0..6}` · `set-custom-voltages {voltages:{"2000":1050}}` · `ensure-telemetry {fix_frequency:bool}` · `set-metrics-fix {enabled:bool}` · `set-compatibility {set_method,usage_method,fix_metrics,fix_frequency}` | `bc250-governor-config-helper` with its own action spellings: `set-cyan-voltage-level`, `set-cyan-custom-voltages`, `ensure-cyan-telemetry`, `set-cyan-metrics-fix`, `set-cyan-compatibility smu busy-flag 1 0` |
| `/api/cu` | `{op:"set", cu:24..40 step 2, persist:bool}` · `{op:"table", masks:[m0,m1,m2,m3]}` · `{op:"enable-all"}` · `{op:"stock-dispatch"}` | `bc250-cu-helper --yes enable-wgp …` / `--yes disable-wgp …` / `batch '<json>'` / `--yes write-service-table` / `--yes enable all` / `--yes stock-dispatch` |
| `/api/cpu` | `{action:"apply-live"|"apply-qam-scale"|"install-boot", frequency, scale, temperature}` · `{action:"disable-boot"}` | `bc250-cpu-smu-helper apply-live 3800 -20 90` … |

The profiles surface runs the CLI's own `profiles` verb instead of a root
helper (it writes the same user-owned app data the desktop writes), and the
server adds its own guarantees on top of the repository's:

| endpoint | body | behaviour |
| --- | --- | --- |
| `/api/profiles-export` | `{}` | stages `profiles export` under the server's own file name, returns it byte-exact (`bundle_text`, `sha256`) for the browser to download |
| `/api/profiles-preview` | `{bundle_text: "<file contents>"}` (preferred) or `{bundle: {…}}` | writes one 0600 stage file with a **server-generated** name, runs `profiles preview`, returns `preview_id` + checksum + an old→new `diff` against the live config |
| `/api/profiles-import` | `{preview_id, checksum}` | re-previews the staged file, `hmac.compare_digest`s the checksum against what the modal displayed, then runs `profiles import --yes`; the repository backs up the current config first, and the stage file is deleted so **one preview can never import twice** |

An import cannot name a path, only a `preview_id` this server issued, and
`checksum` must match - what the modal showed is what lands, once.

`bundle_text` is preferred because a bundle's own `sha256` covers its exact
bytes: a browser that parses the file and re-sends the object turns `1.0` into
`1`, and the CLI then rejects a bundle the desktop itself wrote (measured - a
`profiles export` file re-serialised through `JSON.parse`/`JSON.stringify` fails
its own checksum). The server stages `bundle_text` verbatim and validates it
afterwards, so the file the operator picked is the file that is checked, shown
and imported. The object form is kept for programmatic clients and is
re-serialised server-side. Bodies on the three profiles routes may reach
`2 × MAX_BUNDLE_BYTES + 8192` (JSON escaping of a large string expands); the
real bound, `MAX_BUNDLE_BYTES` (2 MiB), is enforced on the decoded bundle.

Server-side domain limits (measured against `bc250cc.shared.contract`, so a
request that the helper would refuse never reaches `sudo`):

| surface | accepted |
| --- | --- |
| CPU frequency | 3100–4200 MHz, multiple of 50 |
| CPU scale | −50…0 |
| CPU temperature | 70–90 °C |
| CPU VID | estimated VID must stay ≤ 1325 mV (`contract.estimated_vid`) |
| CU count | 24…40, even; a `table` must total one of those |
| GPU frequency | 500–2400 MHz with `min ≤ max` (a floor may be set to 0 = no floor) |
| GPU voltage | 600–1210 mV, level 0…6 |
| Fan | channel 1…12, duty 0…255 |

The request never contributes a token that has not been type-checked first, and
`argv` is built by the planner, not by the client.

### Confirmation and the stale check

`GET /api/fans|gpu|cpu|cu` stamps `_state_hash` on its own response: the SHA-256
(16 hex) of just the part an operator actually confirmed — duties, the governor
file contents and mtime, the CPU profile, the CU table. Volatile fields (RPM,
timestamps) are excluded so an unrelated fan tick does not invalidate a write.

A `POST` must echo that hash as `expected_state` (for profiles writes the hash
of the config+profiles content from `/api/profiles`). If the device moved in the
meantime — Desktop Mode, Game Mode, a second operator, a thermal policy — the
write stops with **409** and the panel re-reads instead of applying a change
nobody confirmed. An optional `expected` object of dotted paths
(`{"channels.3.duty": 128}`) gives a more specific message.

`confirm: true` is required as well; the UI only ever sets it from the modal that
shows `old → new` for every value about to change.

### Refusals are results

A helper that says no is answering a question, not crashing. Its sentence is
returned verbatim with `outcome: "rejected"` over HTTP 200, e.g.:

    ERR PWM 1 manual-mode read-back was '99'
    AMD BC-250 hardware identity was not detected.

`/api/audit` keeps the last attempts of every kind — applied, unchanged,
rejected by the helper, refused by the server, and stale — so "who tried to
route 40 CUs at 22:10" is answerable.

## Privilege: exact-path sudoers

The desktop reaches the helpers through polkit; a headless listener has no graph-
ical agent, so it uses `sudo -n` against a rule that names each helper by abso-
lute path. Nothing else about the web process is privileged.

`deploy/install-web.sh` renders `deploy/bc250-web.sudoers.template` for the in-
stalling account, validates it with `visudo -cf` and installs it mode 0440:

Notes:

* The fan helper takes no arguments, so its rule has no `*`.
* The other three self-validate their arguments as root (typed actions, BC-250
  DMI identity, ranges, hardware locks). The `*` grants the helper's own
  argument surface, not a shell: `sudo` here can never run anything but that one
  binary, and `Defaults !visiblepw` plus the helper's own checks are the limit.
* Tighten further by enumerating actions if your threat model wants it, e.g.
  `... bc250-cpu-smu-helper apply-live`, `... bc250-cpu-smu-helper disable-boot`
  as separate entries. Every action the web needs is a fixed first token, so this
  is a mechanical change.
* To scope by group instead of account, replace `@USER@` with `%bc250web` and
  add the service account to that group.
* `visudo` is not optional: a syntax error in `/etc/sudoers.d/` breaks `sudo`
  system-wide.
* Reads need none of this. If sudoers is missing, the page still works and every
  write comes back with a `hint` naming this file.

## systemd user service

`deploy/bc250-web.service`, installed to `~/.config/systemd/user/bc250-web.service`:

```ini
[Unit]
Description=BC250 Control Center web dashboard
After=network-online.target

[Service]
Type=simple
Environment=BC250_WEB_BIND=0.0.0.0
Environment=BC250_WEB_PORT=8088
Environment=BC250_WEB_ENABLE_WRITE=1
Environment=BC250_WEB_CLI=bc250-control-center-cli
EnvironmentFile=%h/.config/bc250-web.env
ExecStart=/usr/bin/python3 %h/.local/share/bc250-web/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

This is the shape field-verified end to end on a BC-250: all reads, the four
write surfaces and the profiles round-trip. `NoNewPrivileges` and
`ProtectSystem` are deliberately unset - writes go through setuid `sudo -n`
(a hardened sandbox breaks exactly that) and profiles import rewrites the
user's own `$HOME/.config/bc250-control-center`. For a read-only deployment
(`BC250_WEB_ENABLE_WRITE=0`, no sudo at all) you can add `ProtectSystem=strict`
plus `ReadWritePaths` for the staging and profile directories; that form has
not been field-verified for imports.

    $ bash frontends/web/deploy/install-web.sh   # unit + token + sudoers + linger
    $ systemctl --user status bc250-web
    $ journalctl --user -u bc250-web -f

## Deliberately not exposed

* **Arbitrary CLI arguments.** `READ_COMMANDS` fixes the read argument list;
  only `recovery list`, `integrations --runtime` and `metrics list --limit 50`
  exist, and the profiles verbs take only server-generated stage names.
* **A generic root runner.** Four absolute helper paths, chosen by endpoint.
* **Root reads.** Nothing a token-less observer can see requires elevation.
* **Recovery apply / uninstall / dependency prepare.** Those are multi-minute,
  filesystem-mutating operations whose safety depends on the interactive
  desktop's preflight; the web reports their state (`/api/recovery`,
  `/api/release-gates`) rather than triggering them.

## Limits

* Token comparison is plain string equality over a plain-text listener: put it
  behind a TLS-terminating proxy (`BC250_WEB_BIND=127.0.0.1`) for anything beyond
  a trusted LAN.
* The audit trail is in-memory (`deque(maxlen=200)`) and dies with the process.
* One token, no per-user accounts; anyone with it can write when
  `BC250_WEB_ENABLE_WRITE=1`.
* Fan control needs the out-of-tree `nct6687d` driver. With the in-tree driver,
  the helper refuses every manual duty cycle (`read-back was '99'`) — measured on
  the target device — and `/api/fans` says so in `note`. `sensor_drivers` lists
  `nct6687d` when that driver is the one bound to the chip: the platform binding
  is the signal, because both forks ship the module under the name `nct6687`
  (the in-tree one binds this chip as `nct6683`).

## Verification

Static and behavioural checks that do not need the device:

    $ python3 -m py_compile frontends/web/server.py
    $ BC250_WEB_BIND=127.0.0.1 BC250_WEB_PORT=8099 python3 frontends/web/server.py

That covers compile, auth rejection, the write gate and the panel rendering.

On the device, in order:

1. `curl -s -o - -w '%{http_code}\n' http://127.0.0.1:8088/api/telemetry` → `401`.
2. `curl -s -H "X-Auth: $TOKEN" http://127.0.0.1:8088/api/capabilities` → helpers
   `installed: true`, `write_enabled: true`, `contract_source` naming
   `bc250cc.shared.contract`.
3. `sudo -n /usr/libexec/bc250-control-center/bc250-fan-pwm-helper </dev/null` →
   `READY` / `BYE` (proves the sudoers rule without touching hardware).
4. A fan write with a real `expected_state`, then `journalctl --user -u bc250-web`:
   the helper's sentence should appear in `/api/audit` verbatim.
5. A CU `set` one step from live (e.g. 32 → 34), then re-read `/api/cu` and
   confirm `active_cus` moved and the boot table is unchanged unless `persist`
   was set.
6. A CPU `apply-live`, then `cat /run/bc250-control-center/cpu-live-state.json`.
7. Profiles round-trip: export the bundle, preview it, import that same bundle
   (a no-op content-wise), and confirm `ls ~/.config/bc250-control-center/backups/`
   gained a `profile-before-import-*.json` and re-importing the consumed
   `preview_id` answers 409.
8. Deliberately stale: read, change the state from Desktop Mode, then post with
   the old hash → `409` and no helper invocation in the audit trail.

## Contract

Numeric limits come from `bc250cc.shared.contract` when the package is importable
and from a pinned mirror in `server.py` otherwise (the helpers themselves run
`python3 -I` and read the same generated copy). `/api/capabilities` reports which
source is live, so a drifted frontend is visible rather than silent.
