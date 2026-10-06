# 정시 실행: Kikii 서버 cron → `gh workflow run`

GitHub `schedule` 이벤트는 몇 시간씩 늦게 도착한다 (2026-09~10 실측, KST).

| 슬롯 | 예정 | 실제 도착 |
|---|---|---|
| morning | 09:00 | 11:42 ~ 13:31 |
| afternoon | 15:20 | 20:38 ~ 22:57 |
| flow | 15:40 | 21:00 ~ 다음날 00:13 |

그래서 정시 실행은 외부 서버(Kikii)의 cron이 맡고, GitHub cron은 **백업**으로 남긴다.
외부 트리거가 먼저 돌면 `scripts/slot_guard.py`가 `state/slot_runs.json`에 그날 슬롯을 기록하고,
늦게 도착한 GitHub cron은 그 기록을 보고 건너뛴다 (`JOB=skip`, flow는 `Run flow` 생략).
새 GitHub cron 시간은 추가하지 않는다.

## 1. 토큰

`gh`가 workflow_dispatch를 호출할 수 있는 토큰이 필요하다. 둘 중 하나:

- **Fine-grained PAT (권장)**: Resource owner `sosmos3218-bot`, Repository access → Only select repositories → `cwally-alerts`.
  Repository permissions → **Actions: Read and write** (Metadata: Read-only는 자동). 다른 권한은 주지 않는다.
- Classic PAT: `repo` + `workflow` scope.

서버에는 파일로만 둔다 (예: `/etc/cwally/gh_token`, `chmod 600`). 저장소나 ntfy 메시지에 넣지 않는다.

## 2. 명령

```bash
export GH_TOKEN="$(cat /etc/cwally/gh_token)"
gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=morning     # 09:00
gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=afternoon   # 15:20
gh workflow run cwally-flow-alerts -R sosmos3218-bot/cwally-alerts                    # 15:40
gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=0090b0      # 15:40
```

`gh`가 없으면 REST로 같은 호출을 할 수 있다:

```bash
curl -fsS -X POST -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches \
  -d '{"ref":"main","inputs":{"job":"morning"}}'
```

## 3. crontab (`crontab -e`, 서버 시간대와 무관하게 KST로 고정)

```cron
CRON_TZ=Asia/Seoul
GH_TOKEN_FILE=/etc/cwally/gh_token
0 9 * * 1-5   GH_TOKEN=$(cat $GH_TOKEN_FILE) gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=morning   >> /var/log/cwally-dispatch.log 2>&1
20 15 * * 1-5 GH_TOKEN=$(cat $GH_TOKEN_FILE) gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=afternoon >> /var/log/cwally-dispatch.log 2>&1
40 15 * * 1-5 GH_TOKEN=$(cat $GH_TOKEN_FILE) gh workflow run cwally-flow-alerts -R sosmos3218-bot/cwally-alerts                  >> /var/log/cwally-dispatch.log 2>&1
41 15 * * 1-5 GH_TOKEN=$(cat $GH_TOKEN_FILE) gh workflow run cwally-port-alerts -R sosmos3218-bot/cwally-alerts -f job=0090b0    >> /var/log/cwally-dispatch.log 2>&1
```

- `CRON_TZ`는 cronie(대부분의 RHEL/Ubuntu cron)에서 지원된다. 지원하지 않는 cron이면 서버 TZ가 UTC일 때 `0 0`, `20 6`, `40 6`, `41 6`(UTC)으로 바꾼다.
- 0090b0은 port-alerts의 `cwally-port-main` concurrency 그룹에서 afternoon 뒤에 줄을 선다. 1분 늦춰도 결과는 같다.
- 확인: `gh run list -R sosmos3218-bot/cwally-alerts -L 5` 에서 event가 `workflow_dispatch`인 실행이 정시에 생기는지 본다.
- 실패 시 워크플로가 `cwally-ops`로 `[OPS] 워크플로 실패` 알림을 보낸다. dispatch 자체가 실패하면(토큰 만료 등) 로그 파일만 남으므로, 서버 쪽에서 실패 시 `cwally-ops`로 알리고 싶다면 명령 끝에 `|| curl -d "dispatch 실패" https://ntfy.sh/cwally-ops` 를 붙인다.

## 4. 휴장일

평일 cron은 KRX 휴장일에도 돈다. 장마감 수급 스크립트(`flow_monitor`, `secu_flow`)는 `scripts/kr_calendar.py`로 휴장일을 건너뛴다.
`KR_HOLIDAYS`는 매년 12월에 다음 해 목록으로 갱신한다 (2026-10-09 한글날 포함).
