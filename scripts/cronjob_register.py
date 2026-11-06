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

Delete mode (approved in chat 2026-10-19), CRONJOB_ACTION=delete:
  CRONJOB_JOB_IDS      comma-separated cron-job.org job ids, e.g. "8675032,8675034"
  CRONJOB_DRY_RUN      "1" = list what would be deleted, delete nothing
  One GET /jobs, then every id must be a one-shot trigger made by this script: url is the
  port-alerts.yml dispatch, POST, title "cwally oneshot HH:MM KST (Mon D) <name>", a single
  fixed minute/hour/day/month with an expiry, and not the template job. If any id fails, nothing
  is deleted (exit 3). Then DELETE /jobs/<id> one by one; each deleted id is logged.

Slot mode (approved in chat 2026-10-22 18:07 KST), CRONJOB_ACTION=register_slot:
  Registers the ONE fixed recurring after-close job (SLOT_* constants below): title
  "cwally port after-close (18:00 KST)", Asia/Seoul, minute 0, hour 18, Mon-Fri, no expiry, body
  {"ref":"main","inputs":{"job":"k203780","oneoff_mode":"auto"}}, headers copied from the template job
  (same as one-shots). No other recurring job can be made with this mode. Skips (exit 10) if an enabled
  job with the same title, or any enabled port-alerts.yml dispatch job firing at the next weekday 18:00
  KST, already exists. CRONJOB_DRY_RUN=1 runs every check without creating.
  Moved to 19:00 KST (approved in chat 2026-10-22 18:29 KST); the constants below now say 19:00.

Slot update, CRONJOB_ACTION=update_slot (approved in chat 2026-10-22 18:29 KST):
  PATCHes only job SLOT_JOB_ID (title + schedule to the SLOT_* constants). URL, body, headers and auth
  are left as they are. Refuses unless that job still dispatches port-alerts.yml with SLOT_BODY and has
  the old or new slot title. Skips (exit 10) if it is already at the target. CRONJOB_DRY_RUN=1 checks only.

List mode, CRONJOB_ACTION=list (read-only, approved in chat 2026-10-23):
  One GET /jobs. Prints jobId, title, enabled, schedule (timezone, hours, minutes, wdays, mdays,
  months, expiresAt), url, lastStatus, lastExecution, nextExecution. Never prints headers, bodies or keys.

Pulse update, CRONJOB_ACTION=update_pulse (approved in chat 2026-10-26, mktpulse once a day at 11:00 KST):
  PATCHes only job PULSE_JOB_ID (title + schedule to PULSE_TITLE / PULSE_SCHEDULE: hour 11 Mon-Fri,
  expiresAt PULSE_EXPIRES). URL, body, headers and auth are left as they are. Refuses unless that job still
  dispatches port-alerts.yml with the oneshot body and has the old or new pulse title. Skips (exit 10) if it
  is already at the target. CRONJOB_DRY_RUN=1 checks only.

Pulse mode, CRONJOB_ACTION=register_pulse (mktpulse recurring job, approved in chat 2026-10-22 09:43,
  configs/mktpulse.json cron_approved; implemented 2026-10-23):
  Creates the ONE recurring job PULSE_TITLE: Asia/Seoul, minute 0, hours 10, 11, 13, 15, Mon-Fri,
  expiresAt PULSE_EXPIRES, body {"ref":"main","inputs":{"job":"oneshot"}}, headers copied from the
  template job. Skips (exit 10) if a job with PULSE_TITLE exists, or a port-alerts.yml dispatch job
  with the same hours (10, 11, 13, 15 weekdays) already exists. Its title does not match the one-shot
  title pattern, and delete refuses it explicitly. CRONJOB_DRY_RUN=1 checks only.

TSLA-SPCX mode, CRONJOB_ACTION=register_tslaspcx (approved in chat 2026-10-24 12:28 KST, configs/tslaspcx.json):
  Creates the ONE recurring job TSLASPCX_TITLE: Asia/Seoul, minute 30, hour 7, every day, no expiry, body
  {"ref":"main","inputs":{"job":"tslaspcx","oneoff_mode":"auto"}}, headers copied from the template job.
  Skips (exit 10) if a job with TSLASPCX_TITLE exists, or an enabled port-alerts.yml dispatch job already
  fires at the next 07:30 KST. Delete mode refuses it (not a one-shot title). CRONJOB_DRY_RUN=1 checks only.

TSLA price mode, CRONJOB_ACTION=register_tsla (approved 2026-10-29, configs/tsla.json cron_approved):
  Creates the ONE recurring job TSLA_TITLE: America/New_York, minutes 0 and 30, hours 9-16, Mon-Fri,
  expiresAt 0 (no end date; configs/tsla.json ends_kst is null), body
  {"ref":"main","inputs":{"job":"tsla","oneoff_mode":"auto"}}.
  09:00 and 16:30 ticks are outside the cash session; tsla_monitor.py stays silent. DST follows the NY timezone.
  Skips (exit 10) if that title or the same NY 30-minute schedule already exists. Delete mode refuses it.

Trend mode, CRONJOB_ACTION=register_trend (2026-11-03, docs/ontime-dispatch.md):
  Creates the ONE recurring job TREND_TITLE: Asia/Seoul, minute 40, hour 15, Mon-Fri, no expiry, body
  {"ref":"main","inputs":{}}, URL is the trend-monitor.yml dispatch (not port-alerts, not flow-alerts).
  Headers are copied from the morning template job. Does not PATCH any existing job.
  Skips (exit 10) if TREND_TITLE already exists (enabled or not), or if a trend-monitor.yml job
  already has the weekday 15:40 schedule or would fire at the next weekday 15:40. A port-alerts or
  flow-alerts job at 15:40 is a different URL and is left alone. Delete mode refuses it.

exit 0 = created / deleted (or dry run passed), 10 = already registered (skip),
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
AFTERNOON_JOB_ID = 0  # set when known; fix_slot_oneoff patches oneoff_mode=auto
B0_JOB_ID = 0  # 0090b0 15:41 KST; set when known
DISPATCH_URL = "https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/port-alerts.yml/dispatches"
ONESHOT_BODY = {"ref": "main", "inputs": {"job": "oneshot", "oneoff_mode": "auto"}}
TEMPLATE_BODY = {"ref": "main", "inputs": {"job": "morning", "oneoff_mode": "auto"}}
POST = 1  # cron-job.org RequestMethod
SLOT_JOB_ID = 8698629  # created 2026-10-22 by register_slot
SLOT_HOUR = 19
SLOT_TITLE = "cwally port after-close (19:00 KST)"
SLOT_OLD_TITLES = ("cwally port after-close (18:00 KST)",)
SLOT_BODY = {"ref": "main", "inputs": {"job": "k203780", "oneoff_mode": "auto"}}
SLOT_SCHEDULE = {"timezone": "Asia/Seoul", "expiresAt": 0, "minutes": [0], "hours": [SLOT_HOUR],
                 "mdays": [-1], "months": [-1], "wdays": [1, 2, 3, 4, 5]}
TIMEZONE = "Asia/Seoul"
PULSE_JOB_ID = 8705009  # created 2026-10-23 15:00 KST by register_pulse
# 2026-10-26 (chat approval): once a day at 11:00 KST only; job 8705009 is moved by action=update_pulse.
PULSE_TITLE = "cwally mktpulse 11 KST weekdays"
PULSE_OLD_TITLES = ("cwally mktpulse 10/11/13/15 KST weekdays",)
PULSE_HOURS = [11]
PULSE_OLD_HOURS = [10, 11, 13, 15]
PULSE_EXPIRES = 20261119115959  # configs/mktpulse.json ends_kst 2026-11-19 (last slot 11:00; extended from 11/5, chat 2026-11-04)
PULSE_SCHEDULE = {"timezone": "Asia/Seoul", "expiresAt": PULSE_EXPIRES, "minutes": [0], "hours": PULSE_HOURS,
                  "mdays": [-1], "months": [-1], "wdays": [1, 2, 3, 4, 5]}
TSLASPCX_JOB_ID = 8711128  # created 2026-10-24 12:38 KST by register_tslaspcx
TSLASPCX_TITLE = "cwally tslaspcx daily (07:30 KST)"
TSLASPCX_BODY = {"ref": "main", "inputs": {"job": "tslaspcx", "oneoff_mode": "auto"}}
TSLASPCX_SCHEDULE = {"timezone": "Asia/Seoul", "expiresAt": 0, "minutes": [30], "hours": [7],
                     "mdays": [-1], "months": [-1], "wdays": [-1]}
TSLA_TITLE = "cwally tsla US regular 30min (NY)"
TSLA_BODY = {"ref": "main", "inputs": {"job": "tsla", "oneoff_mode": "auto"}}
TSLA_SCHEDULE = {"timezone": "America/New_York", "expiresAt": 0, "minutes": [0, 30],
                 "hours": [9, 10, 11, 12, 13, 14, 15, 16], "mdays": [-1], "months": [-1],
                 "wdays": [1, 2, 3, 4, 5]}
PORTLIGHT_TITLE = "cwally port portlight (Sun 21:00 KST)"
PORTLIGHT_BODY = {"ref": "main", "inputs": {"job": "portlight", "oneoff_mode": "auto"}}
PORTLIGHT_SCHEDULE = {"timezone": "Asia/Seoul", "expiresAt": 0, "minutes": [0], "hours": [21],
                      "mdays": [-1], "months": [-1], "wdays": [0]}
TREND_URL = "https://api.github.com/repos/sosmos3218-bot/cwally-alerts/actions/workflows/trend-monitor.yml/dispatches"
TREND_TITLE = "cwally trend-monitor (15:40 KST)"
TREND_BODY = {"ref": "main", "inputs": {}}
TREND_SCHEDULE = {"timezone": "Asia/Seoul", "expiresAt": 0, "minutes": [40], "hours": [15],
                  "mdays": [-1], "months": [-1], "wdays": [1, 2, 3, 4, 5]}

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
    # Default: fail fast on 429 (pt21u). Set CRONJOB_RETRY_429=1 only for non-urgent register flows.
    retries = (1, 2, 3) if env("CRONJOB_RETRY_429") == "1" else (1,)
    for attempt in retries:
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
            if exc.code == 429:
                ra = exc.headers.get("Retry-After") if exc.headers else None
                hint = f" Retry-After={ra}" if ra else " (no Retry-After header)"
                if attempt != retries[-1]:
                    time.sleep(40 * attempt)
                    continue
                raise Stop(API_ERROR, f"cron-job.org {method} {path} -> HTTP 429{hint}")
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


ONESHOT_TITLE = re.compile(r"cwally oneshot \d{2}:\d{2} KST \([A-Z][a-z]{2} \d{1,2}\) [A-Za-z0-9_-]+")


def parse_job_ids() -> list[int]:
    raw = env("CRONJOB_JOB_IDS")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise Stop(BAD_INPUT, "job_ids is empty (comma-separated cron-job.org job ids)")
    if len(parts) > 30:
        raise Stop(BAD_INPUT, "too many job ids (max 30 per run)")
    if any(not re.fullmatch(r"\d{1,12}", p) for p in parts):
        raise Stop(BAD_INPUT, f"job_ids must be digits separated by commas, got {raw!r}")
    ids = list(dict.fromkeys(int(p) for p in parts))
    if TEMPLATE_JOB_ID in ids:
        raise Stop(BAD_INPUT, f"refusing: {TEMPLATE_JOB_ID} is the morning template job")
    return ids


def oneshot_problems(j: dict) -> list[str]:
    problems = []
    title = (j.get("title") or "").strip()
    sched = j.get("schedule") or {}
    if (j.get("url") or "").rstrip("/") != DISPATCH_URL:
        problems.append("url is not the port-alerts.yml dispatch")
    if j.get("requestMethod") != POST:
        problems.append("method is not POST")
    if not ONESHOT_TITLE.fullmatch(title):
        problems.append("title is not 'cwally oneshot HH:MM KST (Mon D) <name>'")
    for field in ("minutes", "hours", "mdays", "months"):
        vals = sched.get(field) or []
        if len(vals) != 1 or vals[0] < 0:
            problems.append(f"schedule {field} is not one fixed value (recurring?)")
    if not int(sched.get("expiresAt") or 0):
        problems.append("schedule has no expiry (not a one-shot)")
    return problems


def run_delete() -> int:
    ids = parse_job_ids()
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not deleting")
    by_id = {int(j.get("jobId") or 0): j for j in res.get("jobs") or []}
    refused = []
    for jid in ids:
        j = by_id.get(jid)
        if j is None:
            refused.append(f"{jid}: not found in this account")
            continue
        problems = oneshot_problems(j)
        title_now = (j.get("title") or "").strip()
        if title_now in (PULSE_TITLE,) + PULSE_OLD_TITLES + (TSLA_TITLE, TSLASPCX_TITLE, PORTLIGHT_TITLE, TREND_TITLE) or jid in (SLOT_JOB_ID, PULSE_JOB_ID):
            problems.append("recurring job (mktpulse, after-close, tslaspcx, portlight, tsla, or trend-monitor); never deleted here")
        if problems:
            refused.append(f"{jid} ({(j.get('title') or '').strip()[:80]}): " + "; ".join(problems))
    if refused:
        for r in refused:
            print(f"refuse: {r}")
        raise Stop(BAD_TEMPLATE, f"{len(refused)} of {len(ids)} job ids are not one-shot triggers of this repo; nothing deleted")
    dry = env("CRONJOB_DRY_RUN") == "1"
    for jid in ids:
        j = by_id[jid]
        title = (j.get("title") or "").strip()
        state = "enabled" if j.get("enabled") else "disabled"
        exp = (j.get("schedule") or {}).get("expiresAt")
        if dry:
            print(f"would delete: job_id={jid} title={title!r} {state} expires={exp}")
            continue
        call("DELETE", f"/jobs/{jid}", key)
        print(f"deleted: job_id={jid} title={title!r} {state} expires={exp}")
        time.sleep(0.3)  # API limit 5 req/s
    print(f"{'dry run ok: would delete' if dry else 'deleted'} {len(ids)} job(s)")
    return 0


def next_weekday_slot(now: datetime) -> datetime:
    d = now.replace(hour=SLOT_HOUR, minute=0, second=0, microsecond=0)
    if d <= now:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def run_register_slot(now: datetime) -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    template = load_template(key)
    nxt = next_weekday_slot(now)
    dup = find_duplicate(key, SLOT_TITLE, nxt)
    if dup is not None:
        print(f"skip: job {dup} already fires at {nxt:%Y-%m-%d %H:%M} KST or has title {SLOT_TITLE!r}")
        return SKIP
    job = {
        "enabled": True,
        "title": SLOT_TITLE,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(SLOT_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(SLOT_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {SLOT_TITLE!r} Mon-Fri {SLOT_HOUR}:00 Asia/Seoul, body {json.dumps(SLOT_BODY)}, first {nxt:%Y-%m-%d %H:%M} KST")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    print_slot(key, int(job_id))
    return 0


def slot_body_ok(got: dict) -> bool:
    try:
        return json.loads((got.get("extendedData") or {}).get("body") or "null") == SLOT_BODY
    except ValueError:
        return False


def print_slot(key: str, job_id: int) -> dict:
    got = call("GET", f"/jobs/{job_id}", key).get("jobDetails") or {}
    sc = got.get("schedule") or {}
    nx = got.get("nextExecution")
    nx_txt = datetime.fromtimestamp(int(nx), KST).strftime("%Y-%m-%d %H:%M KST") if nx else "n/a"
    print(f"job_id={job_id} title={got.get('title')!r} enabled={got.get('enabled')} tz={sc.get('timezone')} "
          f"hours={sc.get('hours')} minutes={sc.get('minutes')} wdays={sc.get('wdays')} expiresAt={sc.get('expiresAt')} "
          f"url_ok={(got.get('url') or '').rstrip('/') == DISPATCH_URL} next_execution={nx_txt} body_ok={slot_body_ok(got)}")
    return got


def run_update_slot(now: datetime) -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    print("before:")
    got = print_slot(key, SLOT_JOB_ID)
    title = (got.get("title") or "").strip()
    if (got.get("url") or "").rstrip("/") != DISPATCH_URL or not slot_body_ok(got):
        raise Stop(BAD_INPUT, f"job {SLOT_JOB_ID} is not the port-alerts k203780 slot job; not touching it")
    if title not in (SLOT_TITLE,) + SLOT_OLD_TITLES:
        raise Stop(BAD_INPUT, f"job {SLOT_JOB_ID} has unexpected title {title!r}; not touching it")
    sc = got.get("schedule") or {}
    if title == SLOT_TITLE and all(sc.get(k) == v for k, v in SLOT_SCHEDULE.items()):
        print("skip: already at target")
        return SKIP
    nxt = next_weekday_slot(now)
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not risking a duplicate")
    for j in res.get("jobs") or []:
        if int(j.get("jobId") or 0) == SLOT_JOB_ID or not j.get("enabled"):
            continue
        if (j.get("title") or "").strip() == SLOT_TITLE or (
                (j.get("url") or "").rstrip("/") == DISPATCH_URL and fires_at(j.get("schedule") or {}, nxt)):
            print(f"skip: job {j.get('jobId')} already has title {SLOT_TITLE!r} or fires at {nxt:%Y-%m-%d %H:%M} KST")
            return SKIP
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would set job {SLOT_JOB_ID} to {SLOT_TITLE!r} Mon-Fri {SLOT_HOUR}:00 Asia/Seoul, first {nxt:%Y-%m-%d %H:%M} KST")
        return 0
    call("PATCH", f"/jobs/{SLOT_JOB_ID}", key, {"job": {"title": SLOT_TITLE, "schedule": dict(SLOT_SCHEDULE)}})
    print("after:")
    print_slot(key, SLOT_JOB_ID)
    return 0


def fmt_ts(v) -> str:
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        return "n/a"
    return datetime.fromtimestamp(v, KST).strftime("%Y-%m-%d %H:%M:%S KST") if v > 0 else "n/a"


def list_jobs(key: str) -> list[dict]:
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        print("::warning::cron-job.org job list is incomplete (someFailed)")
    return res.get("jobs") or []


def print_job(j: dict) -> None:
    sc = j.get("schedule") or {}
    print(f"job_id={j.get('jobId')} title={(j.get('title') or '').strip()!r} enabled={j.get('enabled')} "
          f"tz={sc.get('timezone')} hours={sc.get('hours')} minutes={sc.get('minutes')} wdays={sc.get('wdays')} "
          f"mdays={sc.get('mdays')} months={sc.get('months')} expiresAt={sc.get('expiresAt')} "
          f"url={(j.get('url') or '').strip()} lastStatus={j.get('lastStatus')} "
          f"lastExecution={fmt_ts(j.get('lastExecution'))} nextExecution={fmt_ts(j.get('nextExecution'))}")


def run_list() -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    jobs = list_jobs(key)
    for j in sorted(jobs, key=lambda x: int(x.get("jobId") or 0)):
        print_job(j)
    pulse = [j for j in jobs if (j.get("title") or "").strip() in (PULSE_TITLE,) + PULSE_OLD_TITLES or is_pulse_like(j)]
    print(f"total={len(jobs)} mktpulse_jobs={[j.get('jobId') for j in pulse]}")
    return 0


def is_pulse_like(j: dict) -> bool:
    sc = j.get("schedule") or {}
    # recurring weekday dispatch only (a one-shot at 11:xx has fixed mdays/months, so it never matches)
    return ((j.get("url") or "").rstrip("/") == DISPATCH_URL
            and sorted(int(h) for h in (sc.get("hours") or [])) in (PULSE_HOURS, PULSE_OLD_HOURS)
            and list(sc.get("mdays") or []) == [-1] and list(sc.get("months") or []) == [-1]
            and sorted(int(w) for w in (sc.get("wdays") or [])) == [1, 2, 3, 4, 5])


def pulse_body_ok(got: dict) -> bool:
    try:
        return json.loads((got.get("extendedData") or {}).get("body") or "null") == ONESHOT_BODY
    except ValueError:
        return False


def run_update_pulse(now: datetime) -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    print("before:")
    got = call("GET", f"/jobs/{PULSE_JOB_ID}", key).get("jobDetails") or {}
    print_job(got)
    title = (got.get("title") or "").strip()
    if (got.get("url") or "").rstrip("/") != DISPATCH_URL or not pulse_body_ok(got):
        raise Stop(BAD_INPUT, f"job {PULSE_JOB_ID} is not the mktpulse oneshot pulse job; not touching it")
    if title not in (PULSE_TITLE,) + PULSE_OLD_TITLES:
        raise Stop(BAD_INPUT, f"job {PULSE_JOB_ID} has unexpected title {title!r}; not touching it")
    sc = got.get("schedule") or {}
    if title == PULSE_TITLE and all(sc.get(k) == v for k, v in PULSE_SCHEDULE.items()):
        print("skip: already at target")
        return SKIP
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would set job {PULSE_JOB_ID} to {PULSE_TITLE!r} Mon-Fri {PULSE_HOURS}:00 Asia/Seoul, expiresAt {PULSE_EXPIRES}")
        return 0
    call("PATCH", f"/jobs/{PULSE_JOB_ID}", key, {"job": {"title": PULSE_TITLE, "schedule": dict(PULSE_SCHEDULE)}})
    print("after:")
    print_job(call("GET", f"/jobs/{PULSE_JOB_ID}", key).get("jobDetails") or {})
    return 0


def run_register_pulse(now: datetime) -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    if now >= datetime.strptime(str(PULSE_EXPIRES), "%Y%m%d%H%M%S").replace(tzinfo=KST):
        raise Stop(BAD_INPUT, "pulse expiry is in the past; nothing to register")
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not risking a duplicate")
    for j in res.get("jobs") or []:
        if (j.get("title") or "").strip() in (PULSE_TITLE,) + PULSE_OLD_TITLES or is_pulse_like(j):
            print("skip: mktpulse job already exists:")
            print_job(j)
            return SKIP
    template = load_template(key)
    job = {
        "enabled": True,
        "title": PULSE_TITLE,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(PULSE_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(ONESHOT_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {PULSE_TITLE!r} Mon-Fri {PULSE_HOURS}:00 Asia/Seoul, "
              f"body {json.dumps(ONESHOT_BODY)}, expiresAt {PULSE_EXPIRES}")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    got = call("GET", f"/jobs/{int(job_id)}", key).get("jobDetails") or {}
    try:
        body_ok = json.loads((got.get("extendedData") or {}).get("body") or "null") == ONESHOT_BODY
    except ValueError:
        body_ok = False
    print("created:")
    print_job(got)
    print(f"body_ok={body_ok}")
    return 0


def run_register_tslaspcx(now: datetime) -> int:
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    template = load_template(key)
    nxt = now.replace(hour=7, minute=30, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    dup = find_duplicate(key, TSLASPCX_TITLE, nxt)
    if dup is not None:
        print(f"skip: job {dup} already fires at {nxt:%Y-%m-%d %H:%M} KST or has title {TSLASPCX_TITLE!r}")
        return SKIP
    job = {
        "enabled": True,
        "title": TSLASPCX_TITLE,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(TSLASPCX_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(TSLASPCX_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {TSLASPCX_TITLE!r} every day 07:30 Asia/Seoul, body {json.dumps(TSLASPCX_BODY)}, first {nxt:%Y-%m-%d %H:%M} KST")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    got = call("GET", f"/jobs/{int(job_id)}", key).get("jobDetails") or {}
    try:
        body_ok = json.loads((got.get("extendedData") or {}).get("body") or "null") == TSLASPCX_BODY
    except ValueError:
        body_ok = False
    print("created:")
    print_job(got)
    print(f"body_ok={body_ok}")
    return 0


def is_tsla_like(j: dict) -> bool:
    sc = j.get("schedule") or {}
    hours = sorted(int(h) for h in (sc.get("hours") or []))
    minutes = sorted(int(m) for m in (sc.get("minutes") or []))
    wdays = sorted(int(w) for w in (sc.get("wdays") or []))
    return ((j.get("url") or "").rstrip("/") == DISPATCH_URL
            and sc.get("timezone") == "America/New_York"
            and hours == [9, 10, 11, 12, 13, 14, 15, 16]
            and minutes == [0, 30]
            and list(sc.get("mdays") or []) == [-1]
            and list(sc.get("months") or []) == [-1]
            and wdays == [1, 2, 3, 4, 5])


def run_register_tsla(now: datetime) -> int:
    """US regular-session TSLA price check. Timezone America/New_York keeps DST on the clock. No expiry."""
    del now  # same signature as the other register_* helpers; this job does not expire
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not risking a duplicate")
    for j in res.get("jobs") or []:
        if (j.get("title") or "").strip() == TSLA_TITLE or is_tsla_like(j):
            print("skip: tsla price job already exists:")
            print_job(j)
            return SKIP
    template = load_template(key)
    job = {
        "enabled": True,
        "title": TSLA_TITLE,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(TSLA_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(TSLA_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {TSLA_TITLE!r} Mon-Fri 09:00-16:30 America/New_York "
              f"(code gates 09:30-16:00), body {json.dumps(TSLA_BODY)}, expiresAt 0")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    got = call("GET", f"/jobs/{int(job_id)}", key).get("jobDetails") or {}
    try:
        body_ok = json.loads((got.get("extendedData") or {}).get("body") or "null") == TSLA_BODY
    except ValueError:
        body_ok = False
    print("created:")
    print_job(got)
    print(f"body_ok={body_ok}")
    return 0


def next_trend_fire(now: datetime) -> datetime:
    """Next weekday 15:40 KST strictly after `now` (Saturday and Sunday roll to Monday)."""
    nxt = now.astimezone(KST).replace(hour=15, minute=40, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def is_trend_schedule(sched: dict) -> bool:
    sc = sched or {}
    try:
        minutes = sorted(int(m) for m in (sc.get("minutes") or []))
        hours = sorted(int(h) for h in (sc.get("hours") or []))
        wdays = sorted(int(w) for w in (sc.get("wdays") or []))
        mdays = [int(x) for x in (sc.get("mdays") or [])]
        months = [int(x) for x in (sc.get("months") or [])]
    except (TypeError, ValueError):
        return False
    return (sc.get("timezone") == TIMEZONE
            and minutes == [40]
            and hours == [15]
            and wdays == [1, 2, 3, 4, 5]
            and mdays == [-1]
            and months == [-1])


def trend_duplicate_reason(j: dict, at: datetime) -> str | None:
    """Why this cron-job.org job already covers trend-monitor, or None.

    Matches the trend title (even when disabled) and the trend-monitor.yml URL only.
    port-alerts.yml and flow-alerts.yml jobs at 15:40 are different URLs and are not matches.
    """
    if (j.get("title") or "").strip() == TREND_TITLE:
        return "title"
    if (j.get("url") or "").rstrip("/") != TREND_URL:
        return None
    sched = j.get("schedule") or {}
    exp = int(sched.get("expiresAt") or 0)
    if exp and exp < int(at.strftime("%Y%m%d%H%M%S")):
        return None
    if is_trend_schedule(sched) or (j.get("enabled") and fires_at(sched, at)):
        return "url"
    return None


def run_register_trend(now: datetime) -> int:
    """Weekday 15:40 KST trend-monitor.yml dispatch. Does not touch other 15:40 jobs."""
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    nxt = next_trend_fire(now)
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed); not risking a duplicate")
    for j in res.get("jobs") or []:
        reason = trend_duplicate_reason(j, nxt)
        if reason is None:
            continue
        print(f"skip: trend job already exists ({reason}):")
        print_job(j)
        return SKIP
    template = load_template(key)
    job = {
        "enabled": True,
        "title": TREND_TITLE,
        "saveResponses": True,
        "url": TREND_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(TREND_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(TREND_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {TREND_TITLE!r} Mon-Fri 15:40 Asia/Seoul, "
              f"url trend-monitor.yml, body {json.dumps(TREND_BODY)}, first {nxt:%Y-%m-%d %H:%M} KST")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    got = call("GET", f"/jobs/{int(job_id)}", key).get("jobDetails") or {}
    try:
        body_ok = json.loads((got.get("extendedData") or {}).get("body") or "null") == TREND_BODY
    except ValueError:
        body_ok = False
    url_ok = (got.get("url") or "").rstrip("/") == TREND_URL
    print("created:")
    print_job(got)
    print(f"body_ok={body_ok} url_ok={url_ok}")
    return 0


def run_register_portlight(now: datetime) -> int:
    """Existing portlight slot (AGENTS.md Repeating slots, Sunday 21:00 KST, GitHub cron 0 12 * * 0)."""
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    template = load_template(key)
    nxt = now.replace(hour=21, minute=0, second=0, microsecond=0) + timedelta(days=(6 - now.weekday()) % 7)
    if nxt <= now:
        nxt += timedelta(days=7)
    dup = find_duplicate(key, PORTLIGHT_TITLE, nxt)
    if dup is not None:
        print(f"skip: job {dup} already fires at {nxt:%Y-%m-%d %H:%M} KST or has title {PORTLIGHT_TITLE!r}")
        return SKIP
    job = {
        "enabled": True,
        "title": PORTLIGHT_TITLE,
        "saveResponses": True,
        "url": DISPATCH_URL,
        "requestMethod": POST,
        "requestTimeout": template.get("requestTimeout", 30),
        "redirectSuccess": False,
        "folderId": template.get("folderId", 0),
        "schedule": dict(PORTLIGHT_SCHEDULE),
        "extendedData": {
            "headers": dict(template["extendedData"]["headers"]),
            "body": json.dumps(PORTLIGHT_BODY, separators=(",", ":")),
        },
    }
    if isinstance(template.get("notification"), dict):
        job["notification"] = template["notification"]
    if env("CRONJOB_DRY_RUN") == "1":
        print(f"dry run ok: would create {PORTLIGHT_TITLE!r} Sunday 21:00 Asia/Seoul, body {json.dumps(PORTLIGHT_BODY)}, first {nxt:%Y-%m-%d %H:%M} KST")
        return 0
    job_id = call("PUT", "/jobs", key, {"job": job}).get("jobId")
    if not job_id:
        raise Stop(API_ERROR, "cron-job.org returned no jobId")
    got = call("GET", f"/jobs/{int(job_id)}", key).get("jobDetails") or {}
    try:
        body_ok = json.loads((got.get("extendedData") or {}).get("body") or "null") == PORTLIGHT_BODY
    except ValueError:
        body_ok = False
    print("created:")
    print_job(got)
    print(f"body_ok={body_ok}")
    return 0


def run_fix_slot_oneoff(now: datetime) -> int:
    """pt21u: one light pass — list once, patch morning/afternoon/0090b0 (+ any
    other port-alerts dispatcher missing oneoff_mode=auto when the job uses it),
    read back each. Fail fast on 429. No long warmup/retry loops.
    """
    key = env("CRONJOB_API_KEY")
    if not key:
        raise Stop(BAD_INPUT, "CRONJOB_API_KEY is not set")
    wanted = {
        "morning": TEMPLATE_BODY,
        "afternoon": {"ref": "main", "inputs": {"job": "afternoon", "oneoff_mode": "auto"}},
        "0090b0": {"ref": "main", "inputs": {"job": "0090b0", "oneoff_mode": "auto"}},
        "k203780": SLOT_BODY,
        "tslaspcx": TSLASPCX_BODY,
        "tsla": TSLA_BODY,
        # oneshot/portlight ignore some oneoff paths, but with workflow default=dry they must
        # send auto explicitly or the scheduled run becomes a dry test (pt21u).
        "oneshot": {"ref": "main", "inputs": {"job": "oneshot", "oneoff_mode": "auto"}},
        "portlight": {"ref": "main", "inputs": {"job": "portlight", "oneoff_mode": "auto"}},
    }
    gap = float(env("CRONJOB_GAP_SEC") or "1.5")

    def pause() -> None:
        time.sleep(gap)

    print("fix_slot_oneoff: GET /jobs (once)")
    res = call("GET", "/jobs", key)
    if res.get("someFailed"):
        raise Stop(API_ERROR, "cron-job.org job list is incomplete (someFailed)")
    port_ids: list[tuple[int, str]] = []
    for j in res.get("jobs") or []:
        jid = int(j.get("jobId") or 0)
        if not jid or not j.get("enabled"):
            continue
        if (j.get("url") or "").rstrip("/") != DISPATCH_URL:
            continue
        port_ids.append((jid, (j.get("title") or "").strip()))
    print(f"port-alerts dispatchers enabled: {len(port_ids)}")
    for jid, title in port_ids:
        print(f"  listed id={jid} title={title!r}")

    # Prefer known/hint order: morning template, then afternoon/0090b0 by title, then rest
    def rank(item: tuple[int, str]) -> tuple[int, int]:
        jid, title = item
        tl = title.lower()
        if jid == TEMPLATE_JOB_ID or "morning" in tl or "09:00" in tl:
            return (0, jid)
        if "afternoon" in tl or "15:20" in tl:
            return (1, jid)
        if "0090b0" in tl or "15:41" in tl:
            return (2, jid)
        return (3, jid)

    port_ids.sort(key=rank)
    confirmed_auto: list[str] = []
    patched: list[str] = []
        pending: list[str] = []
    must = {"morning", "afternoon", "0090b0"}
    must_ok: set[str] = set()

    for jid, title in port_ids:
        pause()
        print(f"GET /jobs/{jid}")
        detail = call("GET", f"/jobs/{jid}", key).get("jobDetails") or {}
        ext = detail.get("extendedData") or {}
        try:
            body = json.loads(ext.get("body") or "null")
        except json.JSONDecodeError:
            body = None
        inputs = (body.get("inputs") or {}) if isinstance(body, dict) else {}
        job_name = inputs.get("job")
        if jid == TEMPLATE_JOB_ID:
            job_name = "morning"
        oneoff = inputs.get("oneoff_mode")
        line = f"id={jid} title={title!r} job={job_name!r} oneoff_mode={oneoff!r} body={json.dumps(body, separators=(',', ':'))}"
        print(f"read: {line}")

        if job_name in wanted:
            target = wanted[job_name]
            if body == target and oneoff == "auto":
                print(f"ok: {job_name} already auto")
                confirmed_auto.append(line)
                if job_name in must:
                    must_ok.add(job_name)
                continue
            print(f"update: {job_name} {json.dumps(body)} -> {json.dumps(target)}")
            if env("CRONJOB_DRY_RUN") == "1":
                pending.append(f"dry-run would patch {line}")
                continue
            pause()
            headers = dict(ext.get("headers") or {})
            call("PATCH", f"/jobs/{jid}", key, {
                "job": {
                    "extendedData": {
                        "headers": headers,
                        "body": json.dumps(target, separators=(",", ":")),
                    }
                }
            })
            pause()
            detail2 = call("GET", f"/jobs/{jid}", key).get("jobDetails") or {}
            try:
                body2 = json.loads((detail2.get("extendedData") or {}).get("body") or "null")
            except json.JSONDecodeError:
                body2 = None
            inputs2 = (body2.get("inputs") or {}) if isinstance(body2, dict) else {}
            if body2 == target and inputs2.get("oneoff_mode") == "auto":
                print(f"confirmed: id={jid} job={job_name} oneoff_mode=auto")
                confirmed_auto.append(
                    f"id={jid} title={title!r} job={job_name!r} oneoff_mode=auto body={json.dumps(body2, separators=(',', ':'))}"
                )
                patched.append(job_name)
                if job_name in must:
                    must_ok.add(job_name)
            else:
                pending.append(f"patch verify failed id={jid} got={body2}")
            continue

        # Other port-alerts dispatchers not in wanted map
        if oneoff == "auto":
            print(f"ok-other: already auto job={job_name!r}")
            confirmed_auto.append(line)
        elif oneoff is None:
            pending.append(f"missing oneoff_mode: {line}")
        else:
            pending.append(f"unexpected oneoff={oneoff!r}: {line}")

    print("--- summary ---")
    print(f"must_ok={sorted(must_ok)} patched={patched}")
    print(f"confirmed_auto_count={len(confirmed_auto)}")
    for x in confirmed_auto:
        print(f"CONFIRMED {x}")
    for x in pending:
        print(f"PENDING {x}")
    if must - must_ok:
        raise Stop(API_ERROR, f"required slots not confirmed auto: {sorted(must - must_ok)}; pending={pending}")
    if pending:
        raise Stop(API_ERROR, f"required slots OK but other dispatchers not verified auto: {pending}")
    print("fix_slot_oneoff: all port-alerts dispatchers verified oneoff_mode=auto")
    print("ALL_DISPATCHERS_AUTO=1")
    return 0



def main() -> int:
    now = datetime.now(KST)
    try:
        action = (env("CRONJOB_ACTION") or "register").lower()
        if action == "delete":
            return run_delete()
        if action == "register_slot":
            return run_register_slot(now)
        if action == "update_slot":
            return run_update_slot(now)
        if action == "list":
            return run_list()
        if action == "register_pulse":
            return run_register_pulse(now)
        if action == "update_pulse":
            return run_update_pulse(now)
        if action == "register_tslaspcx":
            return run_register_tslaspcx(now)
        if action == "register_portlight":
            return run_register_portlight(now)
        if action == "register_tsla":
            return run_register_tsla(now)
        if action == "register_trend":
            return run_register_trend(now)
        if action == "fix_slot_oneoff":
            return run_fix_slot_oneoff(now)
        if action != "register":
            raise Stop(BAD_INPUT, f"action must be register, register_slot, update_slot, fix_slot_oneoff, list, register_pulse, update_pulse, register_tslaspcx, register_portlight, register_tsla, register_trend or delete, got {action!r}")
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
