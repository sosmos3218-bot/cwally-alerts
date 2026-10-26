# cwally-alerts

포트폴리오 + 수급 ntfy 허브. 종목마다 저장소를 만들지 않습니다. 규칙은 `AGENTS.md`.

## 구독

| 용도 | topic | 웹 |
|---|---|---|
| 보유/매매/수급 | `cwally-port` | https://ntfy.sh/cwally-port |
| 운영/시스템 (워크플로 실패, 데이터 조회 실패) | `cwally-ops` | https://ntfy.sh/cwally-ops |

제목 prefix: `[STEEL]` `[SQUARE]` `[ALUX]` `[FLOW]` `[HCE]` `[SILICON2]` `[LG]` `[IREN]` `[COVER0198]` `[0220WL]` `[USTSY]` `[SGYN]` `[YENCARRY]` `[CCREV]` `[KSHIP]` `[PORT]` `[KRWFX]` `[SEC]` `[GVAL]` `[455850]` `[0005D0]` `[0192T0]` `[091160]` `[278240]` `[2UB]` `[377990]` `[489000]` `[ADDSTOP]` `[AEO]` `[BRK10]` `[BTC90]` `[BTCOCT]` `[CL]` `[CPI]` `[DHR]` `[DRAM27]` `[GENPACT]` `[GOOGL]` `[ISRG]` `[KOSBRK]` `[NKE]` `[NOV3]` `[OKLO]` `[PATH]` `[PCOR]` `[PFFD]` `[PLTR]` `[PROTEC]` `[SEMREV]` `[SHINWON]` `[SKC]` `[SMH]` `[ZS]` `[0090B0]` `[BTCBOX]` `[XRPBOX]` `[DKQ3]` `[KRBANK]` `[SNDK]` `[SPCX]` `[TSMC]` `[232140]` `[PORTLIGHT]` `[BURRY]` `[ETH]` `[RATES]` `[FOMC]` `[LGIT]` `[NVDA]` `[OPS]` `[PCOR-Q3]` `[PCOR-PX]` `[FIG-PX]` `[FIG-EARN]`.

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
| usdiv | 분기 첫 KRX 거래일 19:00 (1/4/7/10월, k203780 슬롯에 부착, 빠지면 그 분기 첫 5거래일 안 다음 실행) | 429000 TIGER 미국S&P500배당귀족 vs 458730 TIGER 미국배당다우존스: 1년/3년 총수익률과 일별 MDD(무수정 종가 + 분배락일 분배금 직접 계산), 총보수/실부담, 12개월 분배금, 교체 검토 신호 `[US-DIV]` |
| usbond | 평일 19:00 (k203780 슬롯에 부착, 조건 1-3 은 KRX 거래일만, FOMC 다음 날 요약은 결정일 다음 KST 날짜) | IEF(미국채 7-10년, 환헤지 없음) 판단 보조: 2차 분할 가격(10년 기준+25bp 또는 IEF 기준 -1.7%, 3M/6M USD 기대 수익 R = 0.0505h - 6.84dy + 0.5x56xdy²), 손익분기 환율(원/달러 1,310 미만 p3, 1,294 미만 p4, 1,320 위 재무장), 역상관 체제(10거래일 전 대비 10년 +25bp 초과와 원/달러 -2% 미만이 10거래일 연속), FOMC 다음 날 요약(1차 분할 조건 포함). FRED/Treasury.gov, Stooq/Yahoo, ECOS/네이버. FOMC 날짜는 federalreserve.gov 검증, 검증 안 된 주장(점도표 19명 중 16명, 한은 7/8월 인상)은 안 씀 `[US-BOND]` |
| lgesfor | KRX 거래일 19:00 (k203780 슬롯에 부착, 데이터 거래일 기준 중복 방지, 지분율 반영이 늦으면 다음 19:00 에 판정) | LG에너지솔루션(373220) 외국인 지분율(네이버 일별 외국인 보유율): 5.50% 미만 `경고선 이탈`(우선순위 4), 5.45% 미만 `추가 이탈`(5), 이탈 뒤 5.55% 이상 `해제`(3)와 두 선 재무장. 상태가 바뀔 때만 발송. 본문: 기준일, 지분율, 전일/5거래일 전 대비, 두 선까지 거리, 외국인 연속 순매수/순매도. `job=lgesfor` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry. 제목 `[LGESFOR]`, 종료일 없음 |
| tslaspcx | 매일 07:30 (cron-job.org 정시 잡 + GitHub `30 22 * * *` 백업, state 로 중복 방지) | Tesla SpaceX 합병 감시: SEC EDGAR(Tesla 0001318605, SpaceX 0001181412) 새 8-K, 425, S-4, 합병 위임장, 공개매수, DEFA14A 에서 상대 회사 이름이 합병 단어와 같이 나오면 발췌(exchange ratio, premium, per share, merger agreement, special committee)와 링크(확정 신호 p5, 그 외 p4), 위원회 해산/협상 종료 문구는 무산 알림 p4, Polymarket 2027년 말 공식 발표 확률 7일 10%p 변화나 50/80% 통과(3%p 히스테리시스), Kalshi 참고 표시. 검증 안 된 주장(의결권 82/85%, 운영 목표 면제)은 안 씀 `[TSLA-SPCX]` |
| brief | 정기 일정 없음 (`job=brief` 수동 dispatch, `oneoff_mode=auto` 일 때만 발송) | 예약 비서가 쓴 일회성 요약 `outbox/<YYYYMMDD>-<slug>.json` 발송, 4096바이트 넘으면 (1/n) 나눔, 파일+내용 해시로 중복 방지 `[BRIEF]` `[EARN]` |
| xrpetf | 일요일 21:00 (portlight) + 평일 09:00 따라잡기 | 미국 현물 XRP ETF 주간 순유입 4천만 달러 이상일 때만 `[XRP-ETF]` |
| xrpexch | 일요일 21:00 (portlight) + 평일 09:00 따라잡기 | 거래소 XRP 보유량(공개 라벨 지갑 합계, 대리 지표) 4주 연속 감소 또는 주간 +2% 이상일 때만 `[XRP-EXCH]`. 지갑 목록은 분기(1/4/7/10월) 첫 일요일 실행에서 자동 갱신, 결과는 `cwally-ops` |
| xrpescrow | 평일 09:00 (morning, 다음 달 첫 평일 확정 + 3일 이후 월중 80% 이상 미리) | 리플 에스크로 재잠금률 80% 이상 또는 70% 미만일 때만 `[XRP-ESCROW]` |
| ratesbell | 평일 09:00 (morning 번들, 새 cron 없음, 2026-12-31까지) | 미국 10년물(야후 ^TNX, x10 표기면 /10 보정)과 30년 모기지(프레디맥 PMMS): 10년 5.50% 이상 또는 모기지 7.50% 2주 연속이면 경계, 10년 5.00% 미만이면 되돌림, 판정이 바뀔 때와 월요일 주간 요약. 2026-10-24 점검 레벨: 전 미국장 10년물 종가가 5.15% 위로 올라서면(직전 5.15% 이하, 10월 하순 박스 상단 재돌파) `상향 돌파` 1번, 4.85% 아래로 내려가면(직전 4.85% 이상) `하향 이탈` 1번. 밴드(ABOVE/INSIDE/BELOW)가 바뀔 때만 보내고, 재무장은 0.03%p 완충(ABOVE는 종가 5.12% 이하, BELOW는 4.88% 이상에서 조용히 INSIDE 복귀, 그 사이는 직전 밴드 유지, 반대편으로 바로 건너뛰면 발송), 같은 날 판정 변경과 겹치면 한 메시지에 붙임. 밴드는 `state/ratesbell.json` `ten_band` (10/23 종가 4.988% INSIDE로 초기화) `[RATES]` |
| pcorq3 | 11/4 09:00, 11/5 08:00, 11/5 09:00, 11/5 18:00, 11/6 09:00 (morning 번들 + 은퇴한 pcor 의 one-shot 11/5 08:00/18:00 인수, 새 cron 없음, 2026만) | Procore 3분기 판정 1번: 8-K EX-99.1 (미 동부 11/3 날짜, procore.com 보도자료 대체 경로)에서 FY27 매출 가이던스 성장률과 25% non-GAAP 영업이익률 목표 유지 여부, 새 조달을 읽어 `증액`(13% 이상 + 목표 유지) / `보류`(11~13% + 목표 유지, 추가는 46달러 아래만) / `청산`(10% 미만, 목표 하향, 새 대규모 전환사채/증자. 2026-08-06 발행 완료된 2031 만기 9.5억 달러 전환사채는 제외) / `확인 필요`(10~11% 또는 숫자 못 읽음). 참고 줄(판정 미반영): FCF, 10만 달러 고객, 총매출 유지율, DroneDeploy 기여분, 2027 문구, 데이터센터 문장(옛 pcor 체크리스트). 실적은 11/3 미국장 마감 후(11/4 06:05 KST 전후). 위험요인/전망 문구는 판정 전에 제거. 조회 실패/미게시는 `cwally-ops`. `job=pcorq3` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry `[PCOR-Q3]` (2026-10-26 `pcor` `[PCOR]` 통합, pcor 는 disabled) |
| pcorpx | 평일 09:00 (morning 번들, 새 cron 없음, 2026-12-31까지) | Procore 미국 완료 종가: 48달러 이하 `3차 분할 구간 진입`(p3), 46달러 미만 `46달러 지지선 이탈`(p4). 선마다 1번만, 1% 완충 재무장(48.48 / 46.46 위 종가). 데이터 거래일 중복 방지, 10/23 종가 63.00으로 초기화. 실제 52주 저점은 38.03(2026-06-25)이라 46은 계획선으로만 표기. `job=pcorpx` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry `[PCOR-PX]` |
| figpx | 평일 09:00 (morning 번들, 새 cron 없음, 2027-10-31까지) | Figma(FIG, NYSE) 미국 완료 종가: 22달러 이하 `매수 검토 구간 진입`(p4, $20~22 분할매수 검토, 사업 전망 유지 전제), 16.6달러 이하 `약세 시나리오 가치 도달`(p5, NDR 125% 미만 등 매수 논리 재점검), 28.4달러 이상 `기본 시나리오 가치 도달`(p3, 12개월 기본 가치). 구간에 들어갈 때 1번만, 종가가 구간을 벗어나면 조용히 재무장(완충 없음). 데이터 거래일 중복 방지, 10/23 종가 24.64로 초기화. Yahoo chart v8. `job=figpx` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry `[FIG-PX]` |
| figearn | 2026-11-04 09:00 (morning 번들) / 2026-11-05 08:00 (원샷, 기존 08:00 cron-job.org job=oneshot, 11/5 09:00 morning 이 따라잡기) | Figma 3분기 실적(11/4 미국 장 마감 후) 전날 알림 1번과 다음 날 아침 체크리스트 1번(매출 가이던스 373~375백만달러, 연간 가이던스 유지/상향, NDR 135% 이상, GAAP 매출총이익률 85% 안팎, SBC/매출 30% 이하, NDR 125% 미만이면 재검토). 연도/날짜 가드, state 로 1번만. `job=figearn` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry `[FIG-EARN]` |

옮긴 레포 워크플로 중지: `steel-portfolio-ntfy`, `square-sell-alert`, `alux-q3-ntfy`, `grok-workspace` KOSPI flow.

정시 실행(외부 cron → workflow_dispatch)과 중복 방지: `docs/ontime-dispatch.md`
