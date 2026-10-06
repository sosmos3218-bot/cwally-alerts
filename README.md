# cwally-alerts

포트폴리오 + 수급 ntfy 허브. 종목마다 저장소를 만들지 않습니다. 규칙은 `AGENTS.md`.

## 구독

| 용도 | topic | 웹 |
|---|---|---|
| 보유/매매/수급 | `cwally-port` | https://ntfy.sh/cwally-port |
| Kikii/서버 | `cwally-ops` | https://ntfy.sh/cwally-ops |

제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[CLARITY]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]`.

## 스케줄

| 슬롯 | KST | 잡 |
|---|---|---|
| port morning | 평일 09:00 | steel + alux + clarity + hce + … (morning 번들) |
| port afternoon | 평일 15:20 | square + silicon2 + … (afternoon 번들) |
| flow | 평일 15:40 | KOSPI 수급 + secu_flow + kosbrk + … |
| portlight | 일요일 21:00 | portlight |

옮긴 레포 워크플로 중지: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy`, `grok-workspace` KOSPI flow.
