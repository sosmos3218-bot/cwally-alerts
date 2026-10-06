#!/usr/bin/env python3
"""Register one cron-job.org oneshot. Does not print the API key or auth header."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.cron-job.org"
TEMPLATE_JOB = "8589295"
DISPATCH_URL = "https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches"
BODY = "{\"ref\":\"main\",\"inputs\":{\"job\":\"oneshot\"}}"


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    raise SystemExit(1)


def call(method: str, url: str, key: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
    except urllib.error.HTTPError as exc:
        # Body may contain the key if the server echoes headers. Do not print it.
        fail(f"cron-job.org HTTP {exc.code}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fail("cron-job.org response was not JSON")
    return parsed if isinstance(parsed, dict) else {}


def parse_kst(text: str, fmt: str) -> datetime:
    try:
        return datetime.strptime(text.strip(), fmt)
    except ValueError:
        fail(f"bad time {text!r}, expected {fmt}")
    raise AssertionError


def main() -> int:
    key = os.environ.get("CRONJOB_API_KEY") or ""
    title = os.environ.get("CRONJOB_TITLE") or ""
    kst = os.environ.get("CRONJOB_KST") or ""
    expires = os.environ.get("CRONJOB_EXPIRES_KST") or ""
    if not key:
        fail("CRONJOB_API_KEY secret is missing")
    if not title.strip():
        fail("title is empty")
    at = parse_kst(kst, "%Y-%m-%d %H:%M")
    exp = parse_kst(expires, "%Y-%m-%d %H:%M:%S")
    if exp <= at:
        fail("expires_kst must be after kst")
    detail = call("GET", f"{API}/jobs/{TEMPLATE_JOB}", key)
    job = detail.get("jobDetails") or {}
    headers = (job.get("extendedData") or {}).get("headers") or {}
    if not headers.get("Authorization"):
        fail("template job has no Authorization header")
    payload = {
        "job": {
            "url": DISPATCH_URL,
            "enabled": True,
            "title": title.strip()[:128],
            "saveResponses": True,
            "requestMethod": 1,
            "requestTimeout": 30,
            "redirectSuccess": False,
            "folderId": 0,
            "schedule": {
                "timezone": "Asia/Seoul",
                "expiresAt": int(exp.strftime("%Y%m%d%H%M%S")),
                "hours": [at.hour],
                "mdays": [at.day],
                "minutes": [at.minute],
                "months": [at.month],
                "wdays": [-1],
            },
            "extendedData": {"headers": headers, "body": BODY},
        }
    }
    created = call("PUT", f"{API}/jobs", key, payload)
    job_id = created.get("jobId")
    if not job_id:
        fail("create response had no jobId")
    print(f"jobId={job_id} kst={at.strftime('%Y-%m-%d %H:%M')} expires={exp.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
