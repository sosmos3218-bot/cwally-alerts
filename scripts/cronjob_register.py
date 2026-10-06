#!/usr/bin/env python3
"""Register ONE one-shot time on cron-job.org (port-alerts.yml dispatch with {"job":"oneshot"}).

Run by .github/workflows/cronjob-register.yml (workflow_dispatch only, approved 2026-10-06).
One run registers one KST time. Three times = three runs.

env:
  CRONJOB_API_KEY      cron-job.org API key (GitHub Actions secret, never printed)
  CRONJOB_TITLE        job title, e.g. "cwally oneshot 03:35 KST (Oct 8) fedodds"
  CRONJOB_KST          "YYYY-MM-DD HH:MM" (KST), must be a `oneshot_kst` entry run by the picker
  CRONJOB_EXPIRES_KST  "YYYY-MM-DD HH:MM:SS" (KST), same year, not before CRONJOB_KST
  CRONJOB_DRY_RUN      "1" = run every check but do not create the job

Checks (stop before any write):
  1. inputs: formats, CRONJOB_KST in the future, expiry >= time, same year
  2. CRONJOB_KST is a `oneshot_kst` occurrence handled by oneshot_pick (not a slot time)
  3. template job TEMPLATE_JOB_ID (the recurring "cwally port morning (09:00 KST)" job, which is
     never removed): URL is the port-alerts.yml dispatch, POST, body is the morning body,
     Authorization header present. Only its headers are copied; the new job's body is always
     the fixed oneshot body. (A one-shot job is not used as the template because finished
     one-shots get cleaned up.)
  4. duplicate: an existing job with the same URL that already fires at that KST time, or the
     same title -> skip (exit 10)

exit 0 = created (or dry run passed), 10 = already registered (skip),
     2 = bad input, 3 = template check failed, 4 = cron-job.org API error, 1 = crash.
Output is the job ID and the KST time only. Response bodies and header values are never printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oneshot_pick import occurrences  # noqa: E402

KST = timezone(timedelta(hours=9))
API = "https://api.cron-job.org"
TEMPLATE_JOB_ID = 8588894  # cwally port morning (09:00 KST)
DISPATCH_URL = "https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches"
ONESHOT_BODY = {"ref": "main", "inputs": {"job": "oneshot"}}
TEMPLATE_BODY = {"ref": "main", "inputs": {"job": "morning"}}
POST = 1  # cron-job.org RequestMethod
TIMEZONE = "Asia/Seoul"

SKIP = 10
BAD_INPUT = 2
BAD_TEMPLATE = 3
API_ERROR = 4


class Stop(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def parse_inputs(now: datetime) -> tuple[str, datetime, datetime]:
    title = env("CRONJOB_TITLE")
    kst_raw = env("CRONJOB_KST")
    exp_raw = env("CRONJOB_EXPIRES_KST")
    if not title or len(title) > 200:
        raise Stop(BAD_INPUT, "title is empty or longer than 200 chars")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", kst_raw):
        raise Stop(BAD_INPUT, f"kst must be YYYY-MM-DD HH:MM, got {kst_raw!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", exp_raw):
        raise Stop(BAD_INPUT, f"expires_kst must be YYYY-MM-DD HH:MM:SS, got {exp_raw!r}")
    try:
        at = datetime.strptime(kst_raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        exp = datetime.strptime(exp_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError as exc:
        raise Stop(BAD_INPUT, f"invalid date: {exc}")
    if at <= now:
        raise Stop(BAD_INPUT, f"kst {kst_raw} is not in the future (now {now:%Y-%m-%d %H:%M} KST)")
    if exp < at:
        raise Stop(BAD_INPUT, "expires_kst is before kst")
    if exp.year != at.year:
        # the schedule is "every year on day/month at hh:mm"; the expiry is the only year guard
        raise Stop(BAD_INPUT, "expires_kst must be in the same year as kst")
    return title, at, exp


def check_oneshot_kst(at: datetime) -> list[str]:
    stamp = at.strftime("%Y-%m-%d %H:%M")
    hits = [o for o in occurrences() if o["at"] == stamp]
    if not hits:
        raise Stop(BAD_INPUT, f"{stamp} is not an oneshot_kst entry in configs/*.json")
    picked = [o["name"] for o in hits if o["via"] == "oneshot"]
    if not picked:
        raise Stop(BAD_INPUT, f"{stamp} is run by the port slot, not the one-shot picker; nothing to register")
    return picked


def call(method: str, path: str, key: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in (1, 2):
        req = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "cwally-cronjob-register",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                time.sleep(10)
                continue
            # status only; the body could echo request data
            raise Stop(API_ERROR, f"cron-job.org {method} {path} -> HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Stop(API_ERROR, f"cron-job.org {method} {path} -> {type(exc).__name__}")
    raise Stop(API_ERROR, f"cron-job.org {method} {path} -> rate limited")


def load_template(key: str) -> dict:
    job = call("GET", f"/jobs/{TEMPLATE_JOB_ID}", key).get("jobDetails") or {}
    ext = job.get("extendedData") or {}
    headers = ext.get("headers") or {}
    try:
        body = json.loads(ext.get("body") or "null")
    except json.JSONDecodeError:
        body = None
    problems = []
    if job.get("url") != DISPATCH_URL:
        problems.append("url is not the port-alerts.yml dispatch")
    if job.get("requestMethod") != POST:
        problems.append("method is not POST")
    if body != TEMPLATE_BODY:
        problems.append("body is not the morning body (wrong template job?)")
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not str(auth).strip():
        problems.append("no Authorization header")
    if problems:
        raise Stop(BAD_TEMPLATE, f"template job {TEMPLATE_JOB_ID}: " + "; ".join(problems))
    # The copied GitHub token is not an Actions secret, so register it with the runner's masker in
    # case anything ever echoes it. Workflow commands are consumed by the runner, not shown.
    auth = str(auth).strip()
    print(f"::add-mask::{auth}")
    if " " in auth:
        print(f"::add-mask::{auth.split(' ', 1)[1]}")
    return job


def fires_at(sched: dict, at: datetime) -> bool:
    def has(field: str, value: int) -> bool:
        vals = sched.get(field) or []
        return -1 in vals or value in vals

    if (sched.get("timezone") or "UTC") != TIMEZONE:
        return False  # all cwally jobs use Asia/Seoul; other zones are not compared
    exp = int(sched.get("expiresAt") or 0)
    if exp and exp < int(at.strftime("%Y%m%d%H%M%S")):
        return False
    wday = (at.weekday() + 1) % 7  # cron-job.org: 0 = Sunday
    return (has("minutes", at.minute) and has("hours", at.hour) and has("mdays", at.day)
            and has("months", at.month) and has("wdays", wday))


def find_duplicate(key: str, title: str, at: datetime) -> int | None:
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not risking a duplicate")
    for j in res.get("jobs") or []:
        if not j.get("enabled"):
            continue
        same_url = (j.get("url") or "").rstrip("/") == DISPATCH_URL
        if (j.get("title") or "").strip() == title or (same_url and fires_at(j.get("schedule") or {}, at)):
            return int(j.get("jobId") or 0)
    return None


def build_job(template: dict, title: str, at: datetime, exp: datetime) -> dict:
    job = {
        "enabled": True,
        "title": title,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": {
            "timezone": TIMEZONE,
            "expiresAt": int(exp.strftime("%Y%m%d%H%M%S")),
            "minutes": [at.minute],
            "hours": [at.hour],
            "mdays": [at.day],
            "months": [at.month],
            "wdays": [-1],
        },
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(ONESHOT_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    return job


def main() -> int:
    now = datetime.now(KST)
    try:
        title, at, exp = parse_inputs(now)
        names = check_oneshot_kst(at)
        key = env("CRONJOB_API_KEY")
        if not key:
            raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
        template = load_template(key)
        dup = find_duplicate(key, title, at)
        stamp = at.strftime("%Y-%m-%d %H:%M")
        if dup is not None:
            print(f"skip: job {dup} already fires at {stamp} KST ({','.join(names)})")
            return SKIP
        job = build_job(template, title, at, exp)
        if env("CRONJOB_DRY_RUN") == "1":
            print(f"dry run ok: would create {stamp} KST ({','.join(names)}), expires {exp:%Y-%m-%d %H:%M:%S} KST")
            return 0
        job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
        if not job_id:
            raise Stop(API_ERROR, "cron-job.org returned no jobId")
        print(f"job_id={job_id} kst={stamp} expires={exp:%Y-%m-%d %H:%M:%S} names={','.join(names)}")
        return 0
    except Stop as stop:
        print(f"::error::{stop}")
        return stop.code


if __name__ == "__main__":
    raise SystemExit(main())
