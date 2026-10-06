# cwally-alerts

포트폴리오 + 수급 ntfy 허브. 종목마다 저장소를 만들지 않습니다. 규칙은 `AGENTS.md`.

## 구독

| 용도 | topic | 웹 |
|---|---|---|
| 보유/매매/수급 | `cwally-port` | https://ntfy.sh/cwally-port |
| 운영/시스템 (워크플로 실패, 데이터 조회 실패) | `cwally-ops` | https://ntfy.sh/cwally-ops |

제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]` `[0005D0]` `[0192T0]` `[091160]` `[278240]` `[2UB]` `[377990]` `[489000]` `[ADDSTOP]` `[AEO]` `[BRK10]` `[BTC90]` `[BTCOCT]` `[CL]` `[CPI]` `[DHR]` `[DRAM27]` `[GENPACT]` `[GOOGL]` `[ISRG]` `[KOSBRK]` `[NKE]` `[NOV3]` `[OKLO]` `[PATH]` `[PCOR]` `[PFFD]` `[PLTR]` `[PROTEC]` `[SEMREV]` `[SHINWON]` `[SKC]` `[SMH]` `[ZS]` `[0090B0]` `[BTCBOX]` `[XRPBOX]` `[DKQ3]` `[KRBANK]` `[SNDK]` `[SPCX]` `[TSMC]` `[232140]` `[PORTLIGHT]` `[BURRY]` `[ETH]` `[RATES]` `[FOMC]` `[LGIT]` `[OPS]`.

## 스케줄

| 슬롯 | KST | 잡 |
|---|---|---|
| port morning | 평일 09:00 | steel + alux + hce + … (morning 번들) |
| port afternoon | 평일 15:20 | square + silicon2 + … (afternoon 번들) |
| flow | 평일 15:40 | KOSPI 수급 + secu_flow + kosbrk + … |
| portlight | 일요일 21:00 | portlight |

옮긴 레포 워크플로 중지: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy`, `grok-workspace` KOSPI flow.

정시 실행(외부 cron → workflow_dispatch)과 중복 방지: `docs/ontime-dispatch.md`
