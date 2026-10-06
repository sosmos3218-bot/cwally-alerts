#!/usr/bin/env python3
"""Post a workflow-failure notice to cwally-ops (never cwally-port).

usage: python scripts/ops_notify.py "<workflow>" "<job/slot>"
Reads GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_RUN_ID for the run link.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))


def main() -> int:
    wf = sys.argv[1] if len(sys.argv) > 1 else "workflow"
    job = sys.argv[2] if len(sys.argv) > 2 else "?"
    url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", "sosmos3218-bot/cwally-alerts"),
        os.environ.get("GITHUB_RUN_ID", ""),
    )
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    msg = "\n".join([
        f"{now} | OPS | {wf} / {job}",
        "이게 뭐 알림: 알림 워크플로 실행 중 스크립트가 실패했습니다. 매매 신호가 아닙니다.",
        "지금 상태: 실패한 스크립트만 빠졌고 나머지 스크립트와 state 저장은 계속 진행했습니다.",
        f"지금 할 일: 로그에서 ::error:: 줄을 확인하세요. {url}",
    ])
    try:
        publish("cwally-ops", f"[OPS] 워크플로 실패 | {wf} {job}", msg, priority=3, tags=["warning"])
    except Exception as exc:  # never fail the job because the notice failed
        print(f"ops notify failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
