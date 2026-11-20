# 정시 실행: 외부 cron → GitHub `workflow_dispatch`

GitHub `schedule` 이벤트는 몇 시간씩 늦게 도착한다 (2026-09~10 실측, KST).

| 슬롯 | 예정 | 실제 도착 |
|---|---|---|
| morning | 09:00 | 11:42 ~ 13:31 |
| afternoon | 15:20 | 20:38 ~ 22:57 |
| flow | 15:40 | 21:00 ~ 다음날 00:13 |
| trend | 15:40 | 2026-11-03 은 큐에 잡히지 않음 (0 runs) |

정시 실행은 외부 cron 서비스가 `workflow_dispatch`를 호출해서 맡고, GitHub cron은 **백업**으로 남긴다.
새 GitHub cron 시간은 추가하지 않는다. (이 프로젝트에는 별도 서버가 없다.)

## 외부 cron 후보

- **Vercel Cron (cwally-dashboard)**: 팀 `sosmos3218-bots-projects`가 Hobby 플랜이라 쓰지 않는다.
  Hobby는 하루 1회, 정밀도 "해당 시각의 1시간 안(±59분)"이라 09:00/15:20/15:40 정시가 보장되지 않는다
  (https://vercel.com/docs/cron-jobs/usage-and-pricing, 2026-10-06 확인). Pro로 올리면 분 단위 정밀도가 된다.
- **cron-job.org (대안)**: 무료, 분 단위. 사용자 가입이 필요하다. 아래 REST 호출을 그대로 등록한다.

## 호출 (슬롯별, 평일, KST)

| 슬롯 | KST | UTC | 워크플로 파일 | inputs |
|---|---|---|---|---|
| morning | 09:00 | 00:00 | `port-alerts.yml` | `{"job":"morning","oneoff_mode":"auto"}` |
| afternoon | 15:20 | 06:20 | `port-alerts.yml` | `{"job":"afternoon","oneoff_mode":"auto"}` |
| flow | 15:40 | 06:40 | `flow-alerts.yml` | 없음 |
| trend | 15:40 | 06:40 | `trend-monitor.yml` | 없음 (반복 잡 "cwally trend-monitor (15:40 KST)", `action=register_trend`. 잡 번호는 머지 후 등록할 때 생김) |
| 0090b0 | 15:41 | 06:41 | `port-alerts.yml` | `{"job":"0090b0","oneoff_mode":"auto"}` |
| k203780 (장 마감 뒤, k079900 같이) | 19:00 | 10:00 | `port-alerts.yml` | `{"job":"k203780","oneoff_mode":"auto"}` (반복 잡 8698629 "cwally port after-close (19:00 KST)", 2026-10-22 18:07 승인, 18:29 승인으로 19:00 이동) |
| mktpulse | 거래일 11:00 (~11/05, 2026-10-26부터 하루 1회) | 02:00 | `port-alerts.yml` | `{"job":"oneshot"}` (반복 잡 8705009 "cwally mktpulse 11 KST weekdays", picker가 `configs/mktpulse.json` `oneshot_kst`에서 고름, 2026-10-22 승인, 2026-10-26 승인으로 11시만) |
| tslaspcx | 매일 07:30 | 22:30 (전날) | `port-alerts.yml` | `{"job":"tslaspcx","oneoff_mode":"auto"}` (반복 잡 8711128 "cwally tslaspcx daily (07:30 KST)", 2026-10-24 12:28 승인) |
| tsla | 미국 정규장 30분 (09:30-16:00 뉴욕, 평일) | `*/30 13-21 * * 1-5` 백업 (코드가 뉴욕 시각으로 거름) | `port-alerts.yml` | `{"job":"tsla","oneoff_mode":"auto"}` (cron-job.org `action=register_tsla`, 시간대 America/New_York, 분 0과 30, 시 9-16, 월-금. 잡 번호는 머지 후 등록) |
| portlight (poly, ghtrend, isareb, polyinv, XRP watch 같이) | 일요일 21:00 | 12:00 | `port-alerts.yml` | `{"job":"portlight"}` (반복 잡 8723329 "cwally port portlight (Sun 21:00 KST)", 2026-10-26 10:48 승인, 11:59 확인, configs/portlight.json `cron_approved`) |

```
POST https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Body: {"ref":"main","inputs":{"job":"morning","oneoff_mode":"auto"}}
```

성공 응답은 `204 No Content`. morning/afternoon/0090b0 는 `oneoff_mode=auto` 를 넣거나 생략한다(pt20u: 워크플로 기본값이 auto). 테스트 dry 는 반드시 `oneoff_mode=dry` 를 넣는다.
 `gh`가 있으면 `gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=morning`과 같다.

토큰: fine-grained PAT, 저장소 `cwally-alerts`만, **Actions: Read and write** (Metadata read는 자동). 다른 권한은 주지 않는다.

## 반복 원샷 트리거 (mktpulse, 2026-10-22 승인, 2026-10-26 하루 1회로 변경)

- cron-job.org 반복 잡 1개 8705009 "cwally mktpulse 11 KST weekdays": 시간대 Asia/Seoul, 분 0, 시 11, 월~금, 만료 2026-11-05 11:59:59.
  2026-10-26 사용자 채팅 승인("데일리로", 11:00 선택)으로 `cronjob-register.yml` `action=update_pulse`가 옛 "cwally mktpulse 10/11/13/15 KST weekdays"(시 10, 11, 13, 15, 만료 15:59:59)를 이렇게 바꿈. URL, body, 헤더는 그대로.
  2026-10-23 15:00 KST `cronjob-register.yml` `action=register_pulse`로 만듦(첫 실행 2026-10-26 10:00 KST). 10/22, 10/23 장중 슬롯은 이 잡이 없어서 빠졌다.
  상태 확인은 `action=list`(읽기 전용, GET /jobs 1번). `register_pulse`는 같은 제목(옛 제목 포함)이나 같은 시(11, 또는 옛 10, 11, 13, 15) 평일 반복 dispatch 잡이 있으면 skip.
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"oneshot"}}`.
- 무엇을 보낼지는 picker가 정한다. `oneshot_kst`에 없는 시각(휴장일, 11/05 이후)이면 `job=skip`.
- 같은 시각의 다른 원샷도 이 잡 하나로 같이 돈다. `cronjob_register.py`는 같은 시각에 같은 URL 잡이 있으면 새 원샷을 만들지 않는다(skip).
- GitHub schedule 백업 cron은 두지 않는다(장중 알림이라 2~8시간 늦은 실행은 의미가 없다). 늦게 온 실행은 스크립트 stale guard(45분)로 조용히 끝난다.
- 이 반복 잡은 `cronjob-register.yml` delete 모드로 지워지지 않는다(원샷 검사와 제목 검사에 걸림). 콘솔에서 직접 끄거나 지운다.
- 지난 슬롯에 GitHub fallback cron이 없으면 picker는 `::warning::missed, no fallback cron: KEY`를 찍는다. mktpulse auto skip은 `::notice::mktpulse skip: <이유>`와 state `skips`(최근 30개)로 남는다.

## 장 마감 뒤 19:00 반복 잡 (k203780, 2026-10-22 18:07 승인, 18:29 승인으로 19:00 이동)

- cron-job.org 반복 잡 1개 8698629 "cwally port after-close (19:00 KST)": 시간대 Asia/Seoul, 분 0, 시 19, 월~금, 만료 없음.
  처음엔 18:00이었지만 18:00에는 그날 기준가(NAV)가 아직 없어 19:00으로 옮김(`action=update_slot`, 이 잡의 제목과 시간만 바꿈).
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"k203780","oneoff_mode":"auto"}}`.
  `oneoff_mode=auto`가 없으면 기본값 `dry`로 돌아 발송이 안 되므로 꼭 넣는다.
- 만든 방법: `cronjob-register.yml` `action=register_slot` (이 잡 하나만 만들 수 있음, 같은 제목이나 같은 시각 잡이 있으면 skip).
- GitHub cron `0 10 * * 1-5`(19:00 KST)는 백업으로 남긴다. 늦게 온 백업은 k203780 state `last_sent_session`(같은 거래일 1번)과
  k079900 state(`armed`/`fired_day`, `seen_acptno`)로 아무것도 다시 보내지 않는다. 두 실행은 concurrency 그룹으로 순서대로 돌고,
  뒤 실행은 앞 실행이 push한 state를 checkout한다. 휴장일 평일에도 잡은 돌지만 스크립트가 조용히 끝난다.
- 끄기: 콘솔에서 직접(삭제 모드는 원샷만 지움).

## Tesla SpaceX 합병 매일 07:30 반복 잡 (tslaspcx, 2026-10-24 12:28 승인)

- cron-job.org 반복 잡 1개 8711128 "cwally tslaspcx daily (07:30 KST)": 시간대 Asia/Seoul, 분 30, 시 7, 매일(주말 포함), 만료 없음.
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"tslaspcx","oneoff_mode":"auto"}}`.
- 만든 방법: `cronjob-register.yml` `action=register_tslaspcx` (이 잡 하나만, 같은 제목이나 같은 07:30 dispatch 잡이 있으면 skip).
- GitHub cron `30 22 * * *`(07:30 KST)는 백업. 늦게 온 백업은 `state/tslaspcx.json`의 `seen` 접수번호와 예측시장 무장 상태(`pm.move_armed`, `pm.levels`)로
  아무것도 다시 보내지 않는다. 두 실행은 concurrency 그룹으로 순서대로 돌고 checkout 은 `ref: main`이라 앞 실행이 push한 state를 본다.
- `job=tslaspcx` 수동 dispatch 는 `oneoff_mode=auto`가 아니면 dry(selftest + dry, 발송/state 없음).
- 끄기: 콘솔에서 직접(삭제 모드는 원샷만 지움).

## 일요일 21:00 portlight 반복 잡 (2026-10-26 10:48 승인)

승인 기록: configs/portlight.json `cron_approved` 2026-10-26, `cron_approval_text` (11:59 KST 채팅 확인 문구), `cronjob_id` 8723329.

- cron-job.org 반복 잡 1개 8723329 "cwally port portlight (Sun 21:00 KST)": 시간대 Asia/Seoul, 분 0, 시 21, 일요일(wdays [0]), 만료 없음.
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"portlight"}}`.
  portlight 실행 단계는 `oneoff_mode`를 읽지 않고 늘 `auto`로 돌므로 `oneoff_mode`는 넣지 않는다.
- 만든 방법: `cronjob-register.yml` `action=register_portlight` (이 잡 하나만, 같은 제목이나 같은 일요일 21:00 dispatch 잡이 있으면 skip).
- GitHub cron `0 12 * * 0`은 백업. 보통 월요일 새벽에 늦게 와서 `not_sunday`로 조용히 끝난다. 일요일 안에 오면
  ISO 주 키(`%G-W%V`: portlight/poly `weekly_sent_week`, ghtrend `last_week`, polyinv `weekly_sent`)와 isareb `last_sent_month`로 다시 보내지 않는다.
- 끄기: 콘솔에서 직접(삭제 모드는 원샷만 지움).

## TSLA 정규장 가격 (2026-10-29 승인, 2026-11-20 1시간으로 축소)

승인 기록: configs/tsla.json `cron_approved` 2026-10-29. 하루 종일 매시 cron 이 아니라, 미국 정규장 창만 본다.
2026-11-20: 비공개 저장소 무료 분 한도 때문에 30분에서 1시간으로 줄였다. 단계마다 뉴욕 날짜에 한 번만 보내므로, 그 시각에도 가격이 선 아래면 같은 날 잡힌다. 09:30-10:00 사이 잠깐 닿았다가 10:00 전에 회복하면 놓친다. GRAB 도 같은 슬롯이다.

- 정시: cron-job.org 반복 잡 1개 "cwally tsla US regular 30min (NY)" (제목은 그대로). 시간대 America/New_York (서머타임은 그 시간대가 맞춤), 분 0, 시 10부터 16, 월-금, 만료 없음(`expiresAt` 0, `ends_kst` null).
  10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00. body 는 `{"ref":"main","inputs":{"job":"tsla","oneoff_mode":"auto"}}`.
- 적용: 머지 후 `cronjob-register.yml` `action=register_tsla`. dry_run 으로 먼저 확인한다. 같은 제목이나 예전 30분 일정이 있으면 스케줄만 PATCH 하고, 이미 1시간이면 skip (exit 10). URL, body, 헤더는 바꾸지 않는다. 이 워크플로를 실행하기 전에는 예전 30분 잡이 그대로 돈다.
- GitHub `*/30` 백업은 없다. 스케줄이 몇 시간 늦게 오면 09:30-16:00 문 밖에서 조용히 끝나고, 그래도 러너는 1분이 청구된다. cron-job.org 가 하루 통째로 죽으면 그 뉴욕 날짜의 가격 알림은 다음 세션으로 넘어간다.
- 중복: `state/tsla.json` 의 `sent.s310`, `sent.s300` 에 뉴욕 날짜. 단계마다 그날 한 번. 300달러 이하면 310 안내는 같은 실행에서 보내지 않고 그날 보낸 것으로 기록한다.
- `job=tsla` 수동 dispatch 는 `oneoff_mode=auto` 가 아니면 dry(selftest 와 가격 305, 295 출력, 발송과 state 없음).
- 끄기: 콘솔에서 직접(삭제 모드는 원샷만 지움).

## 추세 점검 평일 15:40 반복 잡 (trend-monitor, 2026-11-03)

2026-11-03 15:40 KST 에 `trend-monitor.yml` 의 GitHub schedule 이 큐에 잡히지 않았다 (runs 0). 같은 시각 port-alerts 는 돌았다. GitHub cron `40 6 * * 1-5` 는 늦은 백업으로 둔다.

- 정시: cron-job.org 반복 잡 1개 "cwally trend-monitor (15:40 KST)". 시간대 Asia/Seoul, 분 40, 시 15, 월~금(wdays 1~5), 만료 없음(`expiresAt` 0).
  URL 은 `trend-monitor.yml` 의 workflow_dispatch 이다. port-alerts.yml, flow-alerts.yml 과 주소가 다르다.
  헤더는 템플릿 잡 8588894(morning)에서 복사하고, body 는 `{"ref":"main","inputs":{}}` (date, demo, dry_run, allow_partial, force 는 워크플로 기본값).
- 만든 방법: 머지 후 Actions 에서 워크플로 `cwally-cronjob-register` 를 고르고 `action=register_trend` 로 실행한다. `dry_run` 을 켜면 만들기 전에 검사만 한다.
  같은 제목이 이미 있으면(꺼져 있어도) skip 한다. 같은 trend-monitor.yml 주소가 평일 15:40 일정이거나, 다음 평일 15:40 에 울리면 skip 한다 (exit 10, 워크플로는 성공).
  15:40 flow-alerts 잡과 15:41 port-alerts(0090b0) 잡은 URL 이 달라서 건너뛰지 않고, 고치지도 않는다. 이 액션은 새 잡만 PUT 하고 기존 잡을 덮어쓰지 않는다.
- 중복: concurrency 그룹 `cwally-trend-monitor` 라서 두 번째 실행은 첫 실행이 끝난 뒤에 시작한다.
  시작 전에 origin/main 의 `state/slots/trend.json` 을 본다. 같은 KST 날이면 스크립트를 돌리지 않고 끝난다.
  `trend_monitor/output/YYYY-MM-DD.json` (서울 날짜)이 이미 main 에 있어도 끝난다.
  실제 실행(스케줄, 또는 date/demo/dry_run 없이 온 dispatch)은 스크립트보다 먼저 슬롯을 표시한다. 실패해도 그 표시를 push 해서 늦은 cron 이 같은 날 요약을 다시 보내지 않는다.
  demo, dry_run, 날짜를 직접 넣은 실행은 슬롯을 표시하지 않고 소모하지도 않는다. 같은 날 다시 돌리려면 dispatch 입력 `force=true`.
- 끄기: 콘솔에서 직접. `action=delete` 는 원샷 제목만 지우므로 이 반복 잡은 거절한다.

## 원샷 (`job=oneshot`)

원샷도 외부 cron으로 정시에 돌린다. 원샷 시각마다 일회성 job을 하나 등록하고, 내용은 모두 같다.

```
POST https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches
Body: {"ref":"main","inputs":{"job":"oneshot"}}
```

- `Decide jobs`가 `scripts/oneshot_pick.py pick`을 부른다. `configs/*.json`의 `oneshot_kst` 중
  `예정 시각 <= 지금 < 예정 시각 + 60분`이고 `state/slots/oneshot.json`에 없는 것만 고른다. 없으면 `job=skip`(발송 없음).
- 고른 원샷은 실행 **전에** `<name>@<YYYY-MM-DD HH:MM>` 키로 기록한다(slot_guard와 같은 원칙).
  스크립트가 실패하면 ops 알림이 가고, 늦게 온 GitHub cron은 기록을 보고 건너뛴다. 같은 원샷이 두 번 발송되는 것보다 낫다.
- GitHub 원샷 cron은 백업이다. `check-cron`이 기록을 보고 이미 정시에 돈 원샷이면 `job=skip`, 아니면 실행하고 기록한다.
  picker가 고장 나도(종료 코드 10/0/3 외) 백업 cron은 기록 없이 그대로 실행된다.
- 09:00 / 15:20 원샷 중 그 슬롯 묶음에 이미 들어 있는 알림(`schedule_kst`에 그 시각이 있음)은 슬롯이 처리하므로 고르지 않는다.
- 등록할 시각 목록: `python scripts/oneshot_pick.py plan --from 2026-10-06 --to 2026-10-31` (`oneshot` 줄만 등록).
- 기본 인자는 `auto`(oklo `event`, hce `daily`). 다른 인자는 config의 `oneshot_args`에 시각별로 둔다(예: yencarry `boj_press`).
- 확인용 수동 실행: 원샷이 없는 시각에 `job=oneshot`을 dispatch하면 `due=[]`로 skip되어야 한다.

## 중복 방지 (slot_guard)

- 슬롯 실행이 시작되면 `scripts/slot_guard.py mark <slot>`이 `state/slots/<slot>.json`에 KST 날짜를 쓰고 Persist가 push한다.
- `Decide jobs`(flow는 `Slot guard`)는 **origin/main**의 그 파일을 읽어, 같은 KST 날짜에 이미 돈 슬롯이면 건너뛴다.
  늦게 온 GitHub cron과, 실수로 두 번 들어온 dispatch 모두 해당한다.
- 두 워크플로 모두 concurrency 그룹이 있어서 두 번째 실행은 첫 실행이 push를 마친 뒤 시작한다.
- 슬롯 파일은 슬롯마다 따로라서 port-alerts와 flow-alerts의 state push가 서로 충돌하지 않는다.
- 같은 날 일부러 다시 돌리려면: port-alerts는 `job=all` 또는 개별 job id, flow-alerts는 `force=true`.
- trend-monitor 도 같다. 슬롯 파일은 `state/slots/trend.json` 이고, 서울 날짜의 `trend_monitor/output/YYYY-MM-DD.json` 이 main 에 있으면 그것도 건너뛴다. 다시 돌리려면 `force=true`.
- 스크립트들도 대부분 자체 하루 1회 dedupe(state)를 갖고 있어 이중 안전장치가 된다.

## 휴장일

평일 cron은 KRX 휴장일에도 돈다. 장마감 수급 스크립트(`flow_monitor`, `secu_flow`)는 `scripts/kr_calendar.py`로 휴장일을 건너뛴다.
`KR_HOLIDAYS`는 매년 12월에 다음 해 목록으로 갱신한다 (2026-10-09 한글날 포함).
