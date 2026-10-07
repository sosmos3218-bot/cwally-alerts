#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ALLOWED = {"cwally-port", "cwally-ops"}
KIND_DEFAULT = {"port": "cwally-port", "flow": "cwally-port", "ops": "cwally-ops"}


def resolve_topic(kind: str = "port", configured: str | None = None) -> str:
    if kind == "ops":
        return "cwally-ops"
    return "cwally-port"


# 조회/수집/파싱 실패 안내는 매매 신호가 아니므로 cwally-ops로 보낸다 (AGENTS.md "토픽").
OPS_TITLE_RE = re.compile(r"조회\s?실패|수집\s?실패|파싱\s?실패|해석\s?불가|수급 누락|확인 불가|숫자 미확인|미게시")


def route_topic(topic: str, title: str | None) -> str:
    if title and OPS_TITLE_RE.search(title):
        return "cwally-ops"
    return topic if topic in ALLOWED else "cwally-port"


# 알림 종료 시점 안내 (AGENTS.md "Alert end line", 2026-10-07 승인).
# configs/<name>.json 의 ends_kst ("YYYY-MM-DD HH:MM" KST 또는 null)와 ends_note 를 읽어
# 본문 맨 끝에 "꺼지는 때: ..." 한 줄을 붙인다. name 은 실행 중인 스크립트 파일명
# (<name>_monitor.py)에서 얻는다. 칸이 없거나 읽기에 실패하면 아무것도 붙이지 않는다(발송은 계속).
CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"
ENDS_PREFIX = "꺼지는 때:"
_NAME_SUFFIXES = ("_monitor", "_impl", "_fund")


def alert_name() -> str | None:
    env = os.environ.get("CWALLY_ALERT_NAME")
    if env:
        return env.strip()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    for suf in _NAME_SUFFIXES:
        if stem.endswith(suf) and len(stem) > len(suf):
            return stem[: -len(suf)]
    return None


def ends_line(name: str | None = None) -> str | None:
    try:
        name = name or alert_name()
        if not name:
            return None
        cfg = json.loads((CONFIGS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or "ends_kst" not in cfg:
            return None
        end = cfg.get("ends_kst")
        note = str(cfg.get("ends_note") or "").strip()
        head = f"{str(end).strip()} KST" if end else "종료일 없음"
        return f"{ENDS_PREFIX} {head}" + (f" ({note})" if note else "")
    except Exception as exc:  # 종료 안내 실패가 알림 발송을 막으면 안 된다
        print(f"ends_line skipped: {exc}", file=sys.stderr)
        return None


def append_ends(message: str, name: str | None = None) -> str:
    if ENDS_PREFIX in (message or ""):
        return message
    line = ends_line(name)
    if not line:
        return message
    return f"{message.rstrip()}\n{line}" if message else line


def resolve_server(configured: str | None = None) -> str:
    return configured or os.environ.get("NTFY_SERVER") or "https://ntfy.sh"


def publish(topic: str, title: str, message: str, priority: int = 3, tags: list[str] | None = None, server: str | None = None) -> None:
    topic = route_topic(topic, title)
    message = append_ends(message)
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags or ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        (server or resolve_server()).rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())
