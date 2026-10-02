#!/usr/bin/env python3
"""박병창 쇼츠 바뀌는 조건. 매수/매도 신호가 아님.

EXIT_REVIEW: HY OAS 수준 또는 4주 확대, IG OAS 4주 확대,
또는 (실적 추정 하향 AND PER 4주 하락)이 확인될 때.
DELAY: 기준금리 상단 인하와 실적 추정 상향이 같이 확인될 때만.
조회 실패와 이력 부족은 조건 충족이 아니다.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "burry.json"
STATE_PATH = ROOT / "state" / "burry.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_text(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    base = dict(default)
    base.update(data)
    return base


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_fred(text: str) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if not raw or raw == ".":
            continue
        try:
            rows.append((parts[0].strip(), float(raw)))
        except ValueError:
            continue
    return rows


def value_on_or_before(rows: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    picked = None
    for d, v in rows:
        if d <= day:
            picked = (d, v)
        else:
            break
    return picked


def latest(rows: list[tuple[str, float]]) -> tuple[str, float] | None:
    return rows[-1] if rows else None


def bp(now: float, past: float) -> float:
    return (now - past) * 100.0


def parse_fred_txt(text: str) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0][:4].isdigit():
            continue
        raw = parts[1].strip()
        if raw == ".":
            continue
        try:
            rows.append((parts[0], float(raw)))
        except ValueError:
            continue
    return rows


def fetch_fred(csv_url: str, txt_url: str, timeout: int) -> tuple[list[tuple[str, float]], str]:
    errors: list[str] = []
    for label, url, parser in (("csv", csv_url, parse_fred), ("txt", txt_url, parse_fred_txt)):
        try:
            rows = parser(fetch_text(url, timeout))
            if rows:
                return rows, label
            errors.append(f"{label} 빈 응답")
        except Exception as exc:
            errors.append(f"{label} {exc}")
    raise RuntimeError("; ".join(errors))


def multpl_pe(url: str, timeout: int) -> dict:
    text = fetch_text(url, timeout)
    rows = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})[\s\S]{0,80}?([0-9]+\.[0-9]+)", text)
    if not rows:
        raise RuntimeError("multpl PER 표를 못 읽음")
    month = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    parsed = []
    for mon, day, year, value in rows:
        parsed.append((f"{int(year):04d}-{month[mon]:02d}-{int(day):02d}", float(value)))
    parsed.sort()
    latest = parsed[-1]
    return {"forward_pe": None, "trailing_pe": latest[1], "pe_date": latest[0], "eps_revision": "확인불가", "src": "multpl_trailing", "history": parsed[-6:]}
