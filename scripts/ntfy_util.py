#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import urllib.request

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


def resolve_server(configured: str | None = None) -> str:
    return configured or os.environ.get("NTFY_SERVER") or "https://ntfy.sh"


def publish(topic: str, title: str, message: str, priority: int = 3, tags: list[str] | None = None, server: str | None = None) -> None:
    topic = route_topic(topic, title)
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
