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

```
POST https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Body: {"ref":"main","inputs":{"job":"morning"}}
```

성공 응답은 `204 No Content`. `gh`가 있으면 `gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=morning`과 같다.

토큰: fine-grained PAT, 저장소 `cwally-alerts`만, **Actions: Read and write** (Metadata read는 자동). 다른 권한은 주지 않는다.

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
