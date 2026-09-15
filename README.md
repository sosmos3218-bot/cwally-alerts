# cwally-alerts

포트폴리오 + 수급 ntfy 허브. 종목마다 저장소를 만들지 않습니다. 규칙은 `AGENTS.md`.

## 구독

| 용도 | topic | 웹 |
|---|---|---|
| 보유/매매 | `cwally-port` | https://ntfy.sh/cwally-port |
| 수급 | `cwally-flow` | https://ntfy.sh/cwally-flow |

제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]`

## 스케줄

| 슬롯 | KST | 잡 |
|---|---|---|
| port morning | 평일 09:00 | steel + alux |
| port afternoon | 평일 15:20 | square |
| flow | 평일 15:40 | KOSPI 수급 |

옮긴 레포 워크플로 중지: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy`, `grok-workspace` KOSPI flow.
