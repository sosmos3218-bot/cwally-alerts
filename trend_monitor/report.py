"""JSON, 마크다운, CSV 출력.

대시보드가 읽는 JSON 의 최상위 키는 고정입니다.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trend_monitor.errors import TrendDataError

SEOUL = ZoneInfo("Asia/Seoul")

TOP_KEYS = [
    "schema_version",
    "generated_at",
    "trade_date",
    "demo",
    "params",
    "data_status",
    "holdings",
    "candidates",
    "risk",
]
HOLDING_KEYS = [
    "ticker",
    "name",
    "close",
    "avg_price",
    "return_pct",
    "stop",
    "ma",
    "defense_line",
    "signal",
    "sell_qty",
    "reason",
]
CANDIDATE_KEYS = [
    "ticker",
    "name",
    "market",
    "close",
    "high52",
    "breakout_price",
    "distance_pct",
    "atr14",
    "base_days",
    "base_depth_pct",
    "eps_yoy_pct",
    "stop",
    "qty",
    "weight_pct",
    "target_3r",
    "status",
    "reason",
    "foreign_net_5d",
    "foreign_net_10d",
    "institution_net_5d",
    "institution_net_10d",
]
RISK_KEYS = ["total_open_risk_pct", "cash_after", "exposure_pct", "breadth_pct_above_ma50"]
WATCH_KEYS = [
    "ticker",
    "name",
    "close",
    "pivot_price",
    "stop_price",
    "pivot_break",
    "stop_hit",
    "status",
    "foreign_net_5d",
    "foreign_net_10d",
    "institution_net_5d",
    "institution_net_10d",
    "reason",
]
SIGNALS = {"hold", "stop", "ma_break", "climax_half", "panic"}
STATUSES = {"ok", "chase", "excluded"}


def validate_payload(payload: dict) -> None:
    if list(payload.keys()) != TOP_KEYS:
        raise TrendDataError(f"JSON 최상위 키가 스키마와 다릅니다: {list(payload.keys())}")
    if payload["schema_version"] != 1:
        raise TrendDataError("schema_version 은 1 입니다.")
    if not str(payload["generated_at"]).endswith("+09:00"):
        raise TrendDataError("generated_at 은 +09:00 이어야 합니다.")
    if not isinstance(payload["demo"], bool):
        raise TrendDataError("demo 는 bool 입니다.")
    status = payload["data_status"]
    if not isinstance(status, dict) or "source" not in status or "ok" not in status or "warnings" not in status:
        raise TrendDataError("data_status 키가 부족합니다.")
    for row in payload["holdings"]:
        if list(row.keys()) != HOLDING_KEYS:
            raise TrendDataError(f"보유 키가 스키마와 다릅니다: {list(row.keys())}")
        if row["signal"] not in SIGNALS:
            raise TrendDataError(f"알 수 없는 매도 신호: {row['signal']}")
    for row in payload["candidates"]:
        if list(row.keys()) != CANDIDATE_KEYS:
            raise TrendDataError(f"후보 키가 스키마와 다릅니다: {list(row.keys())}")
        if row["status"] not in STATUSES:
            raise TrendDataError(f"알 수 없는 후보 상태: {row['status']}")
    if list(payload["risk"].keys()) != RISK_KEYS:
        raise TrendDataError(f"risk 키가 스키마와 다릅니다: {list(payload['risk'].keys())}")


def stamp(clock: datetime | None = None) -> str:
    now = clock or datetime.now(SEOUL)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SEOUL)
    return now.astimezone(SEOUL).isoformat(timespec="seconds")


def write_outputs(payload: dict, output_dir: Path) -> dict[str, Path]:
    validate_payload(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    day = payload["trade_date"]
    latest = output_dir / "latest.json"
    dated = output_dir / f"{day}.json"
    md_path = output_dir / f"{day}.md"
    hold_csv = output_dir / f"{day}-holdings.csv"
    cand_csv = output_dir / f"{day}-candidates.csv"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    latest.write_text(text, encoding="utf-8")
    dated.write_text(text, encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    _write_csv(hold_csv, payload["holdings"], HOLDING_KEYS, payload["demo"])
    _write_csv(cand_csv, payload["candidates"], CANDIDATE_KEYS, payload["demo"])
    watch_csv = output_dir / f"{day}-watchlist.csv"
    _write_csv(watch_csv, payload["data_status"].get("watchlist") or [], WATCH_KEYS, payload["demo"])
    return {
        "latest": latest,
        "dated": dated,
        "markdown": md_path,
        "holdings_csv": hold_csv,
        "candidates_csv": cand_csv,
        "watchlist_csv": watch_csv,
    }


def _write_csv(path: Path, rows: list[dict], keys: list[str], demo: bool) -> None:
    fields = ["demo", *keys]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            item = {"demo": "DEMO" if demo else "LIVE"}
            item.update({k: row[k] for k in keys})
            writer.writerow(item)


def render_markdown(payload: dict) -> str:
    demo = payload["demo"]
    head = "DEMO 실매매 신호가 아닙니다. " if demo else ""
    day = payload["trade_date"]
    risk = payload["risk"]
    holdings = payload["holdings"]
    candidates = payload["candidates"]
    actions = [h for h in holdings if h["signal"] != "hold"]
    oks = [c for c in candidates if c["status"] == "ok"]
    lines = [
        f"# {head}추세 모니터 {day}",
        "",
        "이게 뭐 알림: 장마감 종가 기준 보유 점검과 52주 신고가 돌파 후보입니다.",
        f"지금 결과/상태: 보유 {len(holdings)}종목, 매도 신호 {len(actions)}건, 신규 적정 {len(oks)}건.",
        "지금 할 일: 매도 신호가 있으면 다음 거래일 시가에 수량을 줄이고, 신규는 적정 상태만 수량을 봅니다.",
        "나중에: 클라이맥스 절반 매도 뒤 방어선은 평단과 이평 중 높은 값입니다. 상태는 --apply-state 로만 저장합니다.",
        "익절/줄일 때: 손절, 패닉, 이평 이탈은 잔량 전량. 클라이맥스는 잔량의 절반.",
        "하지 말 것: 장중 저가로 패닉을 판단하지 않습니다. 추격(돌파가 대비 5% 초과)은 수량에서 뺍니다. 미수와 신용은 쓰지 않습니다.",
        "스케줄은 없습니다. 이 파일을 직접 실행할 때만 갱신됩니다.",
        "",
        f"출처: {payload['data_status']['source']}",
    ]
    for warning in payload["data_status"]["warnings"]:
        lines.append(f"경고: {warning}")
    lines.extend(
        [
            "",
            f"총 열린 위험 {risk['total_open_risk_pct']}%, 매수 후 현금 {risk['cash_after']}, 노출 {risk['exposure_pct']}%.",
            f"시장 폭(시가총액 상위, 50일선 위): {risk.get('breadth_pct_above_ma50')}%.",
            "",
            "## 보유",
            "",
            "| 종목 | 종가 | 수익률 | 손절 | 이평 | 방어선 | 신호 | 매도수량 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not holdings:
        lines.append("| (없음) |  |  |  |  |  |  |  |")
    for row in holdings:
        lines.append(
            f"| {row['name']} {row['ticker']} | {row['close']} | {row['return_pct']}% | {row['stop']} | {row['ma']} | {row['defense_line']} | {row['signal']} | {row['sell_qty']} |"
        )
        lines.append(f"|  | 이유: {row['reason']} |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## 신규 후보",
            "",
            "| 종목 | 시장 | 종가 | 돌파가 | 거리 | 베이스 | 손절 | 수량 | 비중 | 3R | 상태 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not candidates:
        lines.append("| (없음) |  |  |  |  |  |  |  |  |  |  |")
    for row in candidates:
        eps = "unknown" if row["eps_yoy_pct"] is None else f"{row['eps_yoy_pct']}%"
        lines.append(
            f"| {row['name']} {row['ticker']} | {row['market']} | {row['close']} | {row['breakout_price']} | {row['distance_pct']}% | {row['base_days']}일 {row['base_depth_pct']}% | {row['stop']} | {row['qty']} | {row['weight_pct']}% | {row['target_3r']} | {row['status']} |"
        )
        lines.append(f"|  | EPS {eps}. {row['reason']} |  |  |  |  |  |  |  |  |  |")
    watches = payload["data_status"].get("watchlist") or []
    lines.extend(
        [
            "",
            "## 관심 종목",
            "",
            "| 종목 | 종가 | 피벗 | 손절 | 상태 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if not watches:
        lines.append("| (없음) |  |  |  |  |")
    for row in watches:
        lines.append(
            f"| {row['name']} {row['ticker']} | {row['close']} | {row['pivot_price']} | {row['stop_price']} | {row['status']} |"
        )
        lines.append(f"|  | {row['reason']} |  |  |  |")
    lines.append("")
    return "\n".join(lines)
