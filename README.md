# cwally-alerts

포트폴리오 + 수급 ntfy 허브. 종목마다 저장소를 만들지 않습니다. 규칙은 `AGENTS.md`.

## 구독

| 용도 | topic | 웹 |
|---|---|---|
| 보유/매매/수급 | `cwally-port` | https://ntfy.sh/cwally-port |
| 운영/시스템 (워크플로 실패, 데이터 조회 실패) | `cwally-ops` | https://ntfy.sh/cwally-ops |

제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]` `[0005D0]` `[0192T0]` `[091160]` `[278240]` `[2UB]` `[377990]` `[489000]` `[ADDSTOP]` `[AEO]` `[BRK10]` `[BTC90]` `[BTCOCT]` `[CL]` `[CPI]` `[DHR]` `[DRAM27]` `[GENPACT]` `[GOOGL]` `[ISRG]` `[KOSBRK]` `[NKE]` `[NOV3]` `[OKLO]` `[PATH]` `[PCOR]` `[PFFD]` `[PLTR]` `[PROTEC]` `[SEMREV]` `[SHINWON]` `[SKC]` `[SMH]` `[ZS]` `[0090B0]` `[BTCBOX]` `[XRPBOX]` `[DKQ3]` `[KRBANK]` `[SNDK]` `[SPCX]` `[TSMC]` `[232140]` `[PORTLIGHT]` `[BURRY]` `[ETH]` `[RATES]` `[FOMC]` `[LGIT]` `[NVDA]` `[OPS]`.

## 스케줄

| 슬롯 | KST | 잡 |
|---|---|---|
| port morning | 평일 09:00 | steel + alux + hce + … (morning 번들) |
| port afternoon | 평일 15:20 | square + silicon2 + … (afternoon 번들) |
| flow | 평일 15:40 | KOSPI 수급 + secu_flow + kosbrk + … |
| portlight | 일요일 21:00 | portlight |
| k203780 | KRX 거래일 19:00 (cron-job.org 정시 + GitHub 백업) | TIGER 미국나스닥바이오 일일 요약 `[203780]` (+ k079900 장 마감 뒤 점검) |
| k079900 | 평일 09:00 / 15:20 / 19:00 (기존 슬롯에 부착) | 전진건설로봇 가격 구간 + 정기보고서 영업이익률, 이벤트만 `[079900]` |
| krhc | 그 주 마지막 KRX 거래일 19:00 (k203780 슬롯에 부착, 빠진 주는 다음 09:00/19:00에 1번) | 헬스케어 ETF 3종(143860/227540/248270) 주간 등락 + 괴리율, 원/달러, 셀트리온/삼성바이오로직스 주간 비교 `[KR-HC]` |
| krhcfile | 평일 09:00 / 15:20 / 19:00 (기존 슬롯에 부착) | 셀트리온/삼성바이오로직스 정기보고서 분기 단독 영업이익률, 전년 같은 분기/직전 분기 비교, 이벤트만 `[KR-HC-RPT]` |
| krrate | KRX 거래일 19:00 (k203780 슬롯에 부착, 빠진 날은 다음 19:00에 따라잡기) | 국고10년 종가 4.35% 이상(4.30% 미만 재무장), 회사채 AA- 3년 - 국고3년 스프레드 20거래일 전 대비 20bp 초과 확대(10bp 미만 재무장), 금통위 날 저녁 요약, 근원물가(농산물및석유류제외) 새 달 수치 `[KR-RATE]` |
| xrpetf | 일요일 21:00 (portlight) + 평일 09:00 따라잡기 | 미국 현물 XRP ETF 주간 순유입 4천만 달러 이상일 때만 `[XRP-ETF]` |
| xrpexch | 일요일 21:00 (portlight) + 평일 09:00 따라잡기 | 거래소 XRP 보유량(공개 라벨 지갑 합계, 대리 지표) 4주 연속 감소 또는 주간 +2% 이상일 때만 `[XRP-EXCH]`. 지갑 목록은 분기(1/4/7/10월) 첫 일요일 실행에서 자동 갱신, 결과는 `cwally-ops` |
| xrpescrow | 평일 09:00 (morning, 다음 달 첫 평일 확정 + 3일 이후 월중 80% 이상 미리) | 리플 에스크로 재잠금률 80% 이상 또는 70% 미만일 때만 `[XRP-ESCROW]` |

옮긴 레포 워크플로 중지: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy`, `grok-workspace` KOSPI flow.

정시 실행(외부 cron → workflow_dispatch)과 중복 방지: `docs/ontime-dispatch.md`
