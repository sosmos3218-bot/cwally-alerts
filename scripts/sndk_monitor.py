#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "sndk.json"
STATE_PATH = ROOT / "state" / "sndk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int = 3) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def ir_headline(url: str) -> tuple[bool, str]:
    try:
        html = fetch(url)
    except Exception as exc:
        return False, str(exc)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    hit = re.search(
        r"(fiscal first quarter|Q1 FY ?2027|first quarter of fiscal 2027|Reports Fiscal First Quarter)(.{0,160})",
        text,
        re.I,
    )
    if hit:
        return True, hit.group(0)[:180].strip()
    return True, ""


def body_common(stamp: str, phase: str) -> list[str]:
    return [
        f"{stamp} | PORT | SNDK 체크 {phase}",
        "이게 뭐 알림: 샌디스크(SNDK) 1분기 가이던스 103~108억달러 점검. 매수·매도 신호가 아닙니다. 삼성전자·SK하이닉스는 스위치 3개가 켜지기 전엔 따로 봅니다.",
    ]
