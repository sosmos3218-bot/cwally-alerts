"""ntfy 요약. 실행 1회에 요약 1통.

토픽은 cwally-port 로 고정합니다. 서버는 https://ntfy.sh 입니다.
저장소에 ntfy 토큰 시크릿은 없고, scripts/ntfy_util.publish 를 그대로 씁니다.
제목에 관련 종목코드와 이름이 들어갑니다. 해당 종목이 없으면 KOSPI 가 들어갑니다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from trend_monitor.config import Config

_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")

_BAD = ("·", "・", "—", "–")


def assert_plain_korean(text: str) -> None:
    for mark in _BAD:
        if mark in text:
            raise ValueError(f"금지 문자가 있습니다: {mark}")


def _md(trade_date: str) -> str:
    parts = str(trade_date).split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[1]):02d}/{int(parts[2]):02d}"
    return str(trade_date)


def _breadth_text(payload: dict) -> str:
    risk = payload.get("risk") or {}
    value = risk.get("breadth_pct_above_ma50")
    if value is None:
        return "없음"
    return f"{float(value):.1f}%"


def _named(rows: list[dict]) -> list[str]:
    out = []
    seen = set()
    for row in rows:
        ticker = str(row.get("ticker") or "")
        name = str(row.get("name") or "").strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(f"{ticker} {name}".strip())
    return out


def build_title(payload: dict) -> str:
    sells = [row for row in payload.get("holdings") or [] if row.get("signal") != "hold"]
    fresh = [row for row in payload.get("candidates") or [] if row.get("status") == "ok"]
    named = _named(sells + fresh)
    if not named:
        title = (
            f"[TREND] KOSPI 추세점검 {_md(payload.get('trade_date') or '')}: "
            f"후보 0, 시장폭 {_breadth_text(payload)}"
        )
    else:
        title = "[TREND] " + ", ".join(named)
    assert_plain_korean(title)
    return title


def build_body(payload: dict, cfg: Config) -> str:
    sells = [row for row in payload.get("holdings") or [] if row.get("signal") != "hold"]
    fresh = [row for row in payload.get("candidates") or [] if row.get("status") == "ok"]
    breadth = (payload.get("risk") or {}).get("breadth_pct_above_ma50")
    halved = breadth is not None and float(breadth) < cfg.breadth_min_pct
    label = {
        "stop": "손절",
        "ma_break": f"{cfg.ma_window}일선 이탈",
        "climax_half": "클라이맥스 50%",
        "panic": "패닉",
    }
    lines = []
    if payload.get("demo"):
        lines.append("DEMO. 실매매 신호가 아닙니다.")
    lines.append("매도")
    if not payload.get("holdings"):
        lines.append("보유 없음.")
    elif not sells:
        lines.append("보유 매도 신호 없음.")
    for row in sells:
        kind = label.get(row.get("signal"), row.get("signal"))
        lines.append(
            f"{row['ticker']} {row.get('name') or ''} {kind}, 매도 {row.get('sell_qty')}주, "
            f"종가 {row.get('close')}, 방어선 {row.get('defense_line')}."
        )
    lines.append("신규")
    if not fresh:
        lines.append("신규 후보 없음.")
    for row in fresh:
        extra = ""
        if halved:
            extra = " 시장폭 30% 미만이라 1R 수량을 절반으로 줄였습니다."
        lines.append(
            f"{row['ticker']} {row.get('name') or ''} 진입 {row.get('close')}, "
            f"손절 {row.get('stop')}, 1R {row.get('qty')}주.{extra}"
        )
    lines.append(f"시장폭 {_breadth_text(payload)}. 시가총액 상위 종목 중 50일선 위 비율입니다.")
    end = "종료일 없음" if not cfg.ends_kst else f"{cfg.ends_kst} KST"
    note = f" ({cfg.ends_note})" if cfg.ends_note else ""
    lines.append(f"꺼지는 때: {end}{note}")
    body = "\n".join(lines)
    assert_plain_korean(body)
    return body


def build_message(payload: dict, cfg: Config) -> tuple[str, str]:
    return build_title(payload), build_body(payload, cfg)


def delay_message(trade_date: str, detail: str) -> tuple[str, str]:
    title = f"[TREND] KOSPI 시세 지연 {_md(trade_date)}"
    body = "\n".join(
        [
            f"기준일 {trade_date} 의 마지막 봉이 그 날짜가 아닙니다.",
            detail,
            "이 메시지는 매매 신호가 아닙니다. 봉 날짜가 기준일과 같아지기 전에는 추세 요약을 보내지 않습니다.",
            "꺼지는 때: 종료일 없음 (평일 15:40 KST. 휴장일은 알림 없음.)",
        ]
    )
    assert_plain_korean(title)
    assert_plain_korean(body)
    return title, body


def maybe_publish(payload: dict, cfg: Config, allow_send: bool = False, sender=None) -> dict:
    """allow_send 가 아니면 네트워크를 호출하지 않습니다."""
    title, body = build_message(payload, cfg)
    if not allow_send:
        return {"sent": False, "reason": "발송하지 않음", "title": title, "body": body}
    if not cfg.ntfy_enabled:
        return {"sent": False, "reason": "ntfy 가 꺼져 있습니다.", "title": title, "body": body}
    topic = "cwally-port"
    if sender is None:
        sender = _repo_publish
    sender(topic, title, body, cfg.ntfy_priority, cfg)
    return {"sent": True, "reason": "", "title": title, "body": body}


def previous_breadth(trade_date: str, output_dir) -> tuple[str, float] | None:
    """기준일보다 앞선 가장 가까운 날짜 JSON 의 시장폭. 데모와 값 없는 파일은 건너뜁니다."""
    files = []
    for path in Path(output_dir).glob("*.json"):
        match = _DATE_FILE.match(path.name)
        if match and match.group(1) < str(trade_date):
            files.append((match.group(1), path))
    for day, path in sorted(files, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("demo"):
            continue
        value = (data.get("risk") or {}).get("breadth_pct_above_ma50")
        if value is None:
            continue
        return day, float(value)
    return None


def recovery_message(payload: dict, cfg: Config, prev: tuple[str, float] | None) -> tuple[str, str] | None:
    """직전 값이 기준 미만이고 오늘 값이 기준 이상일 때만 만듭니다."""
    if payload.get("demo") or prev is None:
        return None
    value = (payload.get("risk") or {}).get("breadth_pct_above_ma50")
    if value is None:
        return None
    now_pct = float(value)
    prev_day, prev_pct = prev
    limit = float(cfg.breadth_min_pct)
    if not (prev_pct < limit <= now_pct):
        return None
    day = payload.get("trade_date") or ""
    title = (
        f"[TREND] KOSPI 시장폭 {limit:g}% 회복 {_md(day)}: "
        f"{now_pct:.1f}% ({_md(prev_day)} {prev_pct:.1f}%)"
    )
    end = "종료일 없음" if not cfg.ends_kst else f"{cfg.ends_kst} KST"
    note = f" ({cfg.ends_note})" if cfg.ends_note else ""
    body = "\n".join(
        [
            f"시가총액 상위 {int(cfg.breadth_top_n)}종목 중 {int(cfg.breadth_ma_window)}일선 위 비율이 "
            f"{_md(prev_day)} {prev_pct:.1f}%에서 {_md(day)} {now_pct:.1f}%로 {limit:g}% 이상이 됐습니다.",
            "신규 수량 절반 규칙이 풀리는 구간입니다. 매매 신호가 아니라 시장 국면 확인용입니다.",
            "반등이 하루짜리인지는 다음 거래일에도 이 값이 유지되는지로 확인하세요.",
            f"꺼지는 때: {end}{note}",
        ]
    )
    assert_plain_korean(title)
    assert_plain_korean(body)
    return title, body


def maybe_publish_recovery(
    payload: dict, cfg: Config, output_dir, allow_send: bool = False, sender=None
) -> dict | None:
    """회복이 아니면 None. 회복이면 1통. allow_send 가 아니면 네트워크를 호출하지 않습니다."""
    prev = previous_breadth(payload.get("trade_date") or "", output_dir)
    message = recovery_message(payload, cfg, prev)
    if message is None:
        return None
    title, body = message
    if not allow_send:
        return {"sent": False, "reason": "발송하지 않음", "title": title, "body": body}
    if not cfg.ntfy_enabled:
        return {"sent": False, "reason": "ntfy 가 꺼져 있습니다.", "title": title, "body": body}
    if sender is None:
        sender = _repo_publish
    sender("cwally-port", title, body, cfg.ntfy_priority, cfg)
    return {"sent": True, "reason": "", "title": title, "body": body}


def publish_delay(trade_date: str, detail: str, allow_send: bool = False, sender=None) -> dict:
    title, body = delay_message(trade_date, detail)
    if not allow_send:
        return {"sent": False, "reason": "발송하지 않음", "title": title, "body": body}
    if sender is None:
        sender = _repo_publish
    sender("cwally-ops", title, body, 3, Config())
    return {"sent": True, "reason": "", "title": title, "body": body}


def _repo_publish(topic: str, title: str, body: str, priority: int, cfg: Config) -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.ntfy_util import publish

    publish(
        topic,
        title,
        body,
        priority=priority,
        tags=["chart_with_upwards_trend"],
        server="https://ntfy.sh",
        config={
            "name": "trend",
            "title_prefix": "[TREND]",
            "ends_kst": cfg.ends_kst,
            "ends_note": cfg.ends_note,
        },
        append_end=False,
    )
