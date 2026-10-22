# 정시 실행: 외부 cron → GitHub `workflow_dispatch`

GitHub `schedule` 이벤트는 몇 시간씩 늦게 도착한다 (2026-09~10 실측, KST).

| 슬롯 | 예정 | 실제 도착 |
|---|---|---|
| morning | 09:00 | 11:42 ~ 13:31 |
| afternoon | 15:20 | 20:38 ~ 22:57 |
| flow | 15:40 | 21:00 ~ 다음날 00:13 |

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
| morning | 09:00 | 00:00 | `port-alerts.yml` | `{"job":"morning"}` |
| afternoon | 15:20 | 06:20 | `port-alerts.yml` | `{"job":"afternoon"}` |
| flow | 15:40 | 06:40 | `flow-alerts.yml` | 없음 |
| 0090b0 | 15:41 | 06:41 | `port-alerts.yml` | `{"job":"0090b0"}` |
| k203780 (장 마감 뒤, k079900 같이) | 18:00 | 09:00 | `port-alerts.yml` | `{"job":"k203780","oneoff_mode":"auto"}` (반복 잡 "cwally port after-close (18:00 KST)", 2026-10-22 18:07 승인) |
| mktpulse | 거래일 10:00, 11:00, 13:00, 15:00 (~11/05) | 01:00, 02:00, 04:00, 06:00 | `port-alerts.yml` | `{"job":"oneshot"}` (picker가 `configs/mktpulse.json` `oneshot_kst`에서 고름, 2026-10-22 승인) |

```
POST https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Body: {"ref":"main","inputs":{"job":"morning"}}
```

성공 응답은 `204 No Content`. `gh`가 있으면 `gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=morning`과 같다.

토큰: fine-grained PAT, 저장소 `cwally-alerts`만, **Actions: Read and write** (Metadata read는 자동). 다른 권한은 주지 않는다.

## 반복 원샷 트리거 (mktpulse, 2026-10-22 승인)

- cron-job.org 반복 잡 1개: 시간대 Asia/Seoul, 분 0, 시 10, 11, 13, 15, 월~금, 만료 2026-11-05 15:59:59.
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"oneshot"}}`.
- 무엇을 보낼지는 picker가 정한다. `oneshot_kst`에 없는 시각(휴장일, 11/05 이후)이면 `job=skip`.
- 같은 시각의 다른 원샷도 이 잡 하나로 같이 돈다. `cronjob_register.py`는 같은 시각에 같은 URL 잡이 있으면 새 원샷을 만들지 않는다(skip).
- GitHub schedule 백업 cron은 두지 않는다(장중 알림이라 2~8시간 늦은 실행은 의미가 없다). 늦게 온 실행은 스크립트 stale guard(45분)로 조용히 끝난다.
- 이 반복 잡은 `cronjob-register.yml` delete 모드로 지워지지 않는다(원샷 검사에 걸림). 콘솔에서 직접 끄거나 지운다.

## 장 마감 뒤 18:00 반복 잡 (k203780, 2026-10-22 18:07 승인)

- cron-job.org 반복 잡 1개 "cwally port after-close (18:00 KST)": 시간대 Asia/Seoul, 분 0, 시 18, 월~금, 만료 없음.
  URL과 헤더는 템플릿 잡 8588894(morning)와 같고 body만 `{"ref":"main","inputs":{"job":"k203780","oneoff_mode":"auto"}}`.
  `oneoff_mode=auto`가 없으면 기본값 `dry`로 돌아 발송이 안 되므로 꼭 넣는다.
- 만든 방법: `cronjob-register.yml` `action=register_slot` (이 잡 하나만 만들 수 있음, 같은 제목이나 같은 시각 잡이 있으면 skip).
- GitHub cron `0 9 * * 1-5`는 백업으로 남긴다. 늦게 온 백업은 k203780 state `last_sent_session`(같은 거래일 1번)과
  k079900 state(`armed`/`fired_day`, `seen_acptno`)로 아무것도 다시 보내지 않는다. 두 실행은 concurrency 그룹으로 순서대로 돌고,
  뒤 실행은 앞 실행이 push한 state를 checkout한다. 휴장일 평일에도 잡은 돌지만 스크립트가 조용히 끝난다.
- 끄기: 콘솔에서 직접(삭제 모드는 원샷만 지움).

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
- 스크립트들도 대부분 자체 하루 1회 dedupe(state)를 갖고 있어 이중 안전장치가 된다.

## 휴장일

평일 cron은 KRX 휴장일에도 돈다. 장마감 수급 스크립트(`flow_monitor`, `secu_flow`)는 `scripts/kr_calendar.py`로 휴장일을 건너뛴다.
`KR_HOLIDAYS`는 매년 12월에 다음 해 목록으로 갱신한다 (2026-10-09 한글날 포함).
