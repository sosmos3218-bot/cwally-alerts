#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.request

ALLOWED = {"cwally-port", "cwally-flow", "cwally-ops"}
KIND_DEFAULT = {"port": "cwally-port", "flow": "cwally-flow", "ops": "cwally-ops"}


def resolve_topic(kind: str = "port", configured: str | None = None) -> str:
    default = KIND_DEFAULT.get(kind, "cwally-port")
    wanted = default
    for cand in (configured, os.environ.get("NTFY_TOPIC"), default):
        if cand in ALLOWED:
            wanted = cand
            break
    if kind == "port":
        return "cwally-port"
    if kind == "flow":
        return "cwally-flow"
    if kind == "ops":
        return "cwally-ops"
    return wanted if wanted in ALLOWED else default


def resolve_server(configured: str | None = None) -> str:
    return configured or os.environ.get("NTFY_SERVER") or "https://ntfy.sh"


def publish(topic: str, title: str, message: str, priority: int = 3, tags: list[str] | None = None, server: str | None = None) -> None:
    if topic not in ALLOWED:
        topic = "cwally-port"
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
