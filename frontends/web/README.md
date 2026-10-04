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

- `GET /api/telemetry|system|components|profiles|quick-access|metrics`
  (인증: `X-Auth: <token>` 헤더 또는 `?token=`)
- `GET /api/write` → 501 (의도적: 쓰기 미노출)
- 캐시: telemetry 2s, profiles 5s, 그 외 30s

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

## Phase 2 candidates

- 제어 API: allow-list된 polkit action만 중개 (fan-pwm 프로파일 적용, CU 상태)
- in-process `dispatch_safe` 호출로 CLI subprocess 대체 (불필요한 프로세스 생성 제거)
- metrics 히스토리 그래프 (CLI `metrics --json` 활용)
