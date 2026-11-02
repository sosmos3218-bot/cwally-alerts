# 추세 모니터

코스피, 코스닥 보통주를 장마감 종가로 보는 일일 점검입니다. 52주 고점 돌파와 7주에서 12주 베이스, 이평 정배열로 후보를 고르고, 보유 종목은 손절, 패닉, 이평 이탈, 클라이맥스 순서로 판단합니다. 알림은 꺼져 있고 스케줄도 없습니다.

## 설치

저장소 루트에서 실행합니다. 버전은 고정되어 있습니다.

```bash
python3 -m pip install -r trend_monitor/requirements.txt
```

## 실행

데모는 네트워크를 쓰지 않습니다. 출력마다 DEMO 가 붙습니다. 실매매 신호가 아닙니다.

```bash
python3 -m trend_monitor --demo
```

실데이터 기준일. 날짜는 `YYYY-MM-DD` 또는 `YYYYMMDD` 입니다. 비우면 서울 시각으로 최근 마감 거래일입니다. 15:30 이전이면 멈춥니다.

```bash
python3 -m trend_monitor --date 2026-11-02
```

백테스트는 약 2년치 실데이터입니다. 첫 실행은 종목별 일봉을 받아 `trend_monitor/cache/` 에 저장합니다. 같은 범위를 다시 돌리면 캐시를 씁니다.

```bash
python3 -m trend_monitor.backtest --end 2026-11-02
```

테스트:

```bash
python3 -m pytest trend_monitor/tests -q
```

손절 방식은 `--stop-mode pct` 또는 `--stop-mode atr` 입니다. `-7.5%` 처럼 빼기 기호로 시작하면 옵션으로 오인되므로 쓰지 않습니다.

## 결과 파일

| 파일 | 내용 |
| --- | --- |
| `trend_monitor/output/latest.json` | 방금 실행. 대시보드용 스키마 |
| `trend_monitor/output/YYYY-MM-DD.json` | 같은 내용의 날짜 파일 |
| `trend_monitor/output/YYYY-MM-DD.md` | 한글 리포트 |
| `trend_monitor/output/YYYY-MM-DD-holdings.csv` | 보유 신호 |
| `trend_monitor/output/YYYY-MM-DD-candidates.csv` | 신규 후보 |
| `trend_monitor/output/backtest-summary.json` | 백테스트 요약 |
| `trend_monitor/output/sample-no-holdings.json` | 보유를 비운 예시 |

JSON 최상위 키는 `schema_version`, `generated_at`, `trade_date`, `demo`, `params`, `data_status`, `holdings`, `candidates`, `risk` 입니다. `generated_at` 은 `+09:00` 입니다.

EPS 를 다시 보려면 같은 명령을 한 번 더 실행하면 됩니다. 일봉 캐시가 있으면 시세는 다시 받지 않고, 키가 있을 때만 EPS 를 채웁니다. 후보의 `eps_yoy_pct` 가 숫자면 계산된 값이고, 없으면 `null` (unknown) 입니다.

## 보유

예시 칸은 `trend_monitor/holdings.example.csv` 입니다. 실제 보유는 `trend_monitor/holdings.csv` 에 둡니다. 이 저장소는 비공개이지만 보유 수량은 민감하므로 `holdings.csv` 는 gitignore 입니다. 공개로 바뀌어도 수량이 커밋되지 않습니다.

다른 경로를 쓰려면 환경변수 `TREND_HOLDINGS_PATH` 에 파일 경로를 넣습니다. 경로가 없는데 변수가 있으면 멈추고, 빈 가격으로 채우지 않습니다.

`entry_atr14` 가 비어 있으면 매수일까지의 일봉으로 ATR 을 계산해 그 값으로 고정합니다. 매도 판단에 오늘의 ATR 을 다시 넣지 않습니다.

클라이맥스나 전량 매도 뒤 잔량 제안은 출력에만 있습니다. 상태 파일에 남기려면 같은 명령에 `--apply-state` 를 붙입니다. 플래그가 없으면 `trend_monitor/state/positions.json` 을 쓰지 않습니다.

관심 종목 파일은 `trend_monitor/watchlist.example.csv` 입니다. 헤더만 있고 종목은 비어 있습니다. 종목을 넣으려면 `watchlist.csv` 로 복사합니다. 저장소가 비공개여도 개인 목록이 커밋되지 않도록 gitignore 입니다. 환경변수 `TREND_WATCHLIST_PATH` 로 다른 경로를 줄 수 있습니다. 파일이 있으면 종가가 피벗 이상인지, 손절 이하인지를 리포트와 `data_status.watchlist` 에 적습니다. 시세가 없으면 확인 불가로 적고 가격을 만들지 않습니다.

시장 폭은 시가총액 상위 200종목(우선주, 스팩, ETF, ETN 제외) 가운데 종가가 50일선보다 높은 비율입니다. JSON `risk.breadth_pct_above_ma50` 에 넣습니다. 기본 30% 미만이면 신규 수량을 절반으로 줄이고 리포트에 표시합니다. 계산할 종목이 없으면 값은 null 이고 수량은 줄이지 않습니다.

후보와 관심 종목의 외국인, 기관 순매수는 네이버 모바일 `m.stock.naver.com` 의 trend API 로 최근 5일과 10일을 더합니다. 조회가 실패하면 숫자는 null 이고 문장은 수급 확인 불가입니다. 실행은 멈추지 않습니다.

## OpenDART

분기 EPS 전년비는 환경변수 `OPENDART_API_KEY` 가 있을 때만 계산합니다. 키가 없으면 아래 문장을 출력하고 가격 조건은 그대로 돌며, EPS 는 unknown 입니다.

`OPENDART_API_KEY 가 없습니다. EPS 전년비는 unknown 으로 두고 가격 조건만 계산합니다.`

키를 GitHub Actions 에 넣으려면 저장소 Settings, Secrets and variables, Actions 에서 이름 `OPENDART_API_KEY` 로 추가합니다. 워크플로 `trend-monitor` 가 `secrets.OPENDART_API_KEY` 를 넘깁니다. 키 값은 로그에 찍지 않습니다. 저장소 파일에 키를 적지 마세요.

로컬에서 EPS 까지 보려면:

```bash
OPENDART_API_KEY='발급받은키' python3 -m trend_monitor --date 2026-11-02
```

계산은 고유번호(`corpCode.xml`)와 분기 재무제표입니다. 2분기 이후는 누적 기본주당순이익에서 직전 누적을 빼 분기를 나눕니다. 전년이 음수여도 `(이번-직전)/절대값` 이라 적자 축소와 흑자전환은 양수입니다. 전년이 0이면 unknown 입니다. 응답은 `trend_monitor/cache/dart/` 에 저장하고 호출 사이에 0.25초를 둡니다.

## 규칙 요약

1. 초기 손절은 돌파가의 -7.5% 또는 돌파가에서 진입 ATR 의 k배(기본 2)를 뺀 값입니다. 종목 CSV 의 `stop_mode` 가 `pct` 또는 `atr` 입니다.
2. 평단 대비 최대 수익이 20% 이상인 뒤, 시가 대비 몸통 -4.5% 이하이고 고가-저가 범위가 오늘을 뺀 ATR14 의 2배 이상이면 잔량의 50%입니다.
3. 최대 수익 8% 이상이거나 클라이맥스를 이미 했으면, 종가가 이평(기본 20일, 설정으로 50일) 아래면 잔량 전부입니다.
4. 우선순위는 패닉, 손절, 이평 이탈, 클라이맥스입니다. 클라이맥스 봉이 이평 아래면 전량입니다.
5. 패닉은 종가가 돌파가 대비 -12% 이하일 때입니다. 장중 저가는 보지 않습니다.
6. 클라이맥스 다음 날 방어선은 평단과 이평 중 높은 값입니다.
7. 신규는 종가가 직전 252거래일 고점 이상, 그 고점까지 베이스가 35일에서 60일, 베이스 깊이 10%에서 35%, 종가 > 50일 > 150일 > 200일, 20일 평균 거래대금 50억 원 이상입니다.
8. 종가와 돌파가 거리가 5%를 넘으면 추격이라 수량을 0으로 둡니다. 손절 거리가 10%를 넘어도 제외합니다.
9. 수량은 계좌 1% 위험을 주당 위험으로 나눈 뒤 버립니다. 한 종목 20%, 보유와 신규를 합친 열린 위험 6%, 현금을 넘지 않습니다. 가격은 KRX 호가단위로 반올림합니다(0.5는 위로).

거래대금은 종가 곱하기 거래량입니다. FinanceDataReader 0.9 에서 KRX 종목코드의 기본 경로는 네이버 일봉입니다. data.krx.co.kr 직접 조회는 로그인 세션 없이 JSON 이 아니어서 쓰지 않습니다. `KRX_ID` 와 `KRX_PW` 가 있을 때만 `--source pykrx` 를 쓸 수 있고, 실패하면 멈추며 다른 가격으로 바꾸지 않습니다.

업종 폭은 `config.yaml` 의 `sector_breadth_enabled` 가 false 입니다. true 로 두고 그룹에 종목이 없거나 시세를 못 읽으면 클라이맥스를 보지 않고 경고합니다.

## 백테스트

신호는 그날 종가까지로만 만들고, 매수는 다음 거래일 시가, 매도는 청산 신호 다음 거래일 시가입니다. 20일 이평 청산과 50일 이평 청산을 같은 진입으로 비교합니다. 현재 상장 종목만 받아 상장폐지 종목이 없습니다. 수수료와 세금은 없습니다. 숫자는 `trend_monitor/output/backtest-summary.json` 과 PR 본문에 있습니다.

## 알림과 스케줄

요약 문안은 `trend_monitor/ntfy_summary.py` 에 있습니다. 제목은 `[TREND]` 로 시작하고 관련 종목코드를 넣습니다. 종목이 없으면 `KOSPI` 를 넣습니다. 설정 `ntfy_enabled` 는 false 이고, 실행 명령은 발송 함수를 호출하지 않습니다. 토픽은 `cwally-port` 만 허용합니다.

워크플로는 `.github/workflows/trend-monitor.yml` 이고 `workflow_dispatch` 만 있습니다. 스케줄 키는 없습니다.

평일 15:40 KST 를 나중에 붙일 때:

1. 이 저장소 규칙상 새 반복 시각은 채팅 승인이 있어야 합니다. 승인 없이 cron 을 넣지 마세요.
2. GitHub `schedule` 은 몇 시간 늦습니다. 정시가 필요하면 기존 방식처럼 cron-job.org 가 이 워크플로를 `workflow_dispatch` 로 호출하는 편이 맞습니다.
3. 시각을 UTC 로 바꾸면 15:40 KST 는 06:40 UTC 이고, 평일 식은 `40 6 * * 1-5` 입니다. 이 식을 워크플로에 지금 넣지 않습니다.
4. 승인이 난 뒤에만 `on:` 아래에 `schedule` 을 추가하고, `configs` 에 `cron_approved` 를 남기는 기존 절차를 따르세요. 매시 반복은 금지입니다.

수동 실행은 Actions 탭에서 `trend-monitor` 를 고르고 Run workflow 입니다. 날짜를 비우면 서울 기준 최근 마감일입니다.
