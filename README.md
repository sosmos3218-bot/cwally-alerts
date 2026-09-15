# cwally-alerts

포트폴리오 ntfy 허브. 종목마다 저장소·토픽을 만들지 않습니다.

## 구독 (하나만)

- topic: `cwally-port`
- 웹: https://ntfy.sh/cwally-port
- 제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]`

이전 토픽(`cwally-steel-p60-2026-x8k4q`, `square-sell-cwally-x7k29`, `cwally-alux-475580-2026-k3q`) 구독 해제 가능.

## 스켈줄 (고정)

| 슬롯 | UTC | KST | 잡 |
|---|---|---|---|
| port morning | 00:00 월–금 | 09:00 | steel + alux |
| port afternoon | 06:20 월–금 | 15:20 | square |
| flow | (미이전) | 15:40 | KOSPI 수급 — 아직 미편입 |

## 새 알림 추가 규칙

1. 새 레포 만들지 말 것.
2. `configs/<name>.json` + `scripts/<name>_monitor.py` + `state/<name>.json` 만 추가.
3. ntfy topic은 `cwally-port` 고정. 제목에 `[NAME]` prefix.
4. cron을 늘리지 말 것. morning/afternoon 슬롯에 job만 붙임.
5. Telegram Alpha Catch와 같은 신호 중복 금지.

옮긴 레포: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy` — 워크플로 중지됨.
