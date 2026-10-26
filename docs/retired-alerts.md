# Retired alerts

Alerts removed from this repo after every oneshot_kst / end date had passed. The files (configs, scripts,
state) stay in git history: `git log --all -- configs/<name>.json` and `git show <commit>^:configs/<name>.json`.
Sent messages stay in state/archive.json (until rotated) and in cwally-ntfy-archive.

## 2026-10-26 cleanup (branch cleanup/ntfy-2026-10-26)

Removed: config, scripts/<name>_monitor.py, state file, port-alerts.yml schedule crons, Decide branch, Run
step, workflow_dispatch job option / mode input, AGENTS.md title prefix (only when no other alert uses it).
cron-job.org one-shot jobs for these times are listed in the PR body (deleted separately, not by this PR).

| name | prefix | added | oneshot window (KST) | what it did |
|---|---|---|---|---|
| 0144m0 | `[0144M0]` | 2026-10-07 | 2026-10-07 16:20 ~ 2026-10-07 16:20 | KODEX 미국드론UAM TOP10 |
| bankrate1014 | `[BANKRATE]` | 2026-10-07 | 2026-10-14 08:00 ~ 2026-10-14 08:00 | 10/13 미국 JPM, WFC, 씨티, 골드만삭스 실적과 10/13 10년물 종가(5.31% 재돌파 여부)로 시나리오 A~D를 판정하고, 10... |
| cpi1014 | `[CPI]` | 2026-10-03 | 2026-10-14 22:45 ~ 2026-10-15 09:10 | 10월 14일 밤 미국 9월 CPI를 헤드라인, 근원, 에너지, 주거로 나누고 10월 동결이 깨지는 수치와 12월 인상 확률 변화 조건을 정리하... |
| dearattr | `[DEAR]` | 2026-10-12 | 2026-10-13 09:30 ~ 2026-10-14 09:30 | 디어유 |
| fedodds | `[FOMC]` | 2026-10-06 | 2026-10-08 03:35 ~ 2026-10-15 09:25 | 9월 의사록과 9월 CPI 이후 CME 연방기금 선물로 10월 동결 77~85%와 12월 인상 가격이 유지되는지 판정하는 2026년 일회 알림입... |
| fomc1007 | `[FOMC]` | 2026-10-05 | 2026-10-08 03:20 ~ 2026-10-08 09:10 | 9월 FOMC 의사록에서 추가 인상, 유가, 고용 둔화 문장을 세어 매파와 비둘기 비중을 나누고 다우, 반도체, 2년물 영향을 판단하는 2026... |
| isrgq3 | `[ISRG]` | 2026-10-06 | 2026-10-21 05:40 ~ 2026-10-21 08:00 | 인튜이티브 서지컬 |
| k227540 | `[227540]` | 2026-10-20 | 2026-10-21 16:40 ~ 2026-10-23 15:52 | 삼성바이오 3Q 실적 + 227540 1차 매수 판정 |
| kosreb | `[KOSREB]` | 2026-10-14 | 2026-10-14 23:10 ~ 2026-10-15 09:20 | 키움 10/14 리포트 반등 시나리오를 9월 CPI 결과, 14일 코스피 마감과 외국인 수급, 반도체 선행 PER, 27~28년 컨센 방향으로 ... |
| sapq3 | `[SAPQ3]` | 2026-10-19 | 2026-10-22 07:10 ~ 2026-10-22 09:10 | SAP Q3 실적 후 조건부 매수 재평가 |
| semicrit | `[SEMCRIT]` | 2026-10-12 | 2026-10-13 09:05 ~ 2026-10-15 09:05 | 2026-10-09 영상(오픈AI 매출 혼선, 순환금융, 솔리다임, 외국인 SK하이닉스 매도) 기반 보수적 관찰. 자동 매수/매도 아님. |
| sena | `[SENA]` | 2026-10-12 | 2026-10-21 09:05 ~ 2026-10-21 09:05 | 삼성E&A |
| tsmc1015 | `[TSMC]` | 2026-10-05 | 2026-10-15 16:30 ~ 2026-10-15 17:10 | TSMC 3분기 보도자료와 콜만 보고 달러 매출, 매출총이익률, 4분기 가이던스, 2026-2027 CAPEX, 웨이퍼 가격을 추출해 472달러... |
| usd240 | `[USD240]` | 2026-10-07 | 2026-10-07 16:05 ~ 2026-10-13 16:05 | 261240 KODEX 미국달러선물 규칙을 10월물 종가로 판정하는 2026년 일회 알림입니다. |
| ustsy10 | `[USTSY]` | 2026-09-27 | 2026-10-02 00:30 ~ 2026-10-09 02:15 | 10월 1일 재무부 10년, 30년 입찰 공고의 확정 규모, CUSIP, WI 개시와, 10/6 3년, 10/7 10년, 10/8 30년 결과가... |
| ktb148070 | `[KTB148070]` | 2026-10-20 | 2026-10-22 15:30 ~ 2026-10-22 15:30 | 금통위 후 148070 분할매수 판정. 한은 결정 판정 함수는 scripts/bok_mpc.py 로 이동(krrate 사용) |
