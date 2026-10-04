# BC250 web frontend (phase 1)

Read-only web API + dashboard wrapping the headless CLI
(`bc250-control-center-cli --json <cmd>`), which mirrors the application
layer's `dispatch_safe` table. Hardware writes stay out of reach on purpose:
they keep going through the typed privileged helpers (`privileged/helpers/`)
and polkit actions, as in the desktop frontend.

## Run (on the BC-250, package installed)

    BC250_WEB_TOKEN=$(openssl rand -hex 16) BC250_WEB_PORT=8089 \
      python3 frontends/web/server.py

Open http://<device-ip>:8089 (token을 설정했으면 페이지 하단에 입력).

## Endpoints

- `GET /api/telemetry|system|components|quick-access|metrics|fans`
  (인증: `X-Auth: <token>` 헤더 또는 `?token=`)
- `POST /api/fan` → 팬 제어 (token + BC250_WEB_ENABLE_WRITE=1 + sudoers 룰)
- 캐시: telemetry 2s, fans 3s, 그 외 30s

## systemd user unit (예시)

`~/.config/systemd/user/bc250-web.service`

    [Unit]
    Description=BC250 web frontend
    After=network-online.target

    [Service]
    Environment=BC250_WEB_PORT=8089
    EnvironmentFile=%h/.config/bc250-web.env   # BC250_WEB_TOKEN=...
    ExecStart=/usr/bin/python3 %h/bc250-web/server.py
    Restart=on-failure

    [Install]
    WantedBy=default.target

## Phase 2: fan control (implemented, hardware-gated)

Plumbing is complete and verified end-to-end:
`POST /api/fan {op:set|auto, channel:1..12, value:0..255}` -> token auth ->
`sudo -n /usr/libexec/bc250-control-center/bc250-fan-pwm-helper` (exact-path
NOPASSWD rule in `/etc/sudoers.d/bc250-web`) -> stdin session (`<ch> <val>`,
`AUTO <ch>`, `EXIT`) -> typed helper validation (root-only, BC-250 identity,
ranges, hardware lock). Writes also require `BC250_WEB_ENABLE_WRITE=1`.

`GET /api/fans` reads nct668x sysfs directly (duty/enable/RPM/label), no root.

Measured on the target device (Bazzite 43, in-tree sensor driver):
fan writes are rejected by the helper itself with
"PWM 1 manual-mode read-back was '99'" - the shipped nct6686 reports
proprietary EC mode 99 and the in-tree driver does not support manual PWM.
The desktop application installs the out-of-tree nct6687d driver during
"Prepare dependencies"; until that is installed, fan control fails closed
here too (verified: sysfs state stays untouched after a rejected write).

## Phase 3 candidates

- fan control goes live once the nct6687d dependency is prepared
- in-process `dispatch_safe` instead of CLI subprocess (drop process spawn)
- metrics history graph (CLI `metrics list --json` is already exposed)
