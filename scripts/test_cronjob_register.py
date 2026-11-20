#!/usr/bin/env python3
"""Offline tests for cronjob_register call budget.

The HTTP layer is a fake account. urlopen is replaced before any request.
A tripwire raises if a test reaches the real urllib opener. Do not point
this file at api.cron-job.org, ntfy.sh, or a workflow dispatch.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from email.message import Message

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cronjob_register as reg  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=reg.KST)
HEADERS = {"Authorization": "Bearer test-token", "Accept": "application/vnd.github+json"}
API = reg.API


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(API + "/jobs", code, "err", hdrs, io.BytesIO(b"{}"))


class Resp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def without_auto(target: dict) -> dict:
    body = copy.deepcopy(target)
    body["inputs"].pop("oneoff_mode", None)
    return body


def make_job(jid: int, title: str, body: dict, *, enabled: bool = True, url: str = reg.DISPATCH_URL,
             expires: int = 0, mdays: list | None = None, months: list | None = None,
             hours: list | None = None, minutes: list | None = None, wdays: list | None = None,
             schedule: bool = True) -> dict:
    row = {
        "jobId": jid,
        "enabled": enabled,
        "title": title,
        "saveResponses": False,
        "url": url,
        "requestMethod": reg.POST,
        "extendedData": {
            "headers": dict(HEADERS),
            "body": json.dumps(body, separators=(",", ":")),
        },
    }
    if schedule:
        row["schedule"] = {
            "timezone": "Asia/Seoul",
            "expiresAt": expires,
            "hours": hours if hours is not None else [9],
            "minutes": minutes if minutes is not None else [0],
            "mdays": mdays if mdays is not None else [-1],
            "months": months if months is not None else [-1],
            "wdays": wdays if wdays is not None else [1, 2, 3, 4, 5],
        }
    return row


def big_account() -> list[dict]:
    """133 jobs: 23 live port dispatchers (7 need a body change), 100 expired, 10 ignored."""
    targets = reg.slot_oneoff_targets()
    jobs = [
        make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
        make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
        make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
        make_job(8698629, "cwally port after-close (19:00 KST)", without_auto(targets["k203780"]),
                 hours=[19]),
        make_job(8705009, "cwally mktpulse 11 KST weekdays", without_auto(targets["oneshot"]),
                 expires=20261119115959, hours=[11]),
        make_job(8711128, "cwally tslaspcx daily (07:30 KST)", targets["tslaspcx"],
                 hours=[7], minutes=[30], wdays=[-1]),
        make_job(8723329, "cwally port portlight (Sun 21:00 KST)", targets["portlight"],
                 hours=[21], wdays=[0]),
        make_job(8746217, "cwally tsla US regular 30min (NY)", without_auto(targets["tsla"])),
    ]
    for i in range(12):
        body = targets["oneshot"] if i >= 4 else without_auto(targets["oneshot"])
        jobs.append(make_job(
            8810000 + i, f"cwally oneshot 09:10 KST (Dec 1) fut{i}", body,
            expires=20261201120000, mdays=[1], months=[12], wdays=[-1],
        ))
    for i in range(3):
        jobs.append(make_job(8820000 + i, f"cwally custom live {i}",
                             {"ref": "main", "inputs": {"job": "grab", "oneoff_mode": "auto"}}))
    for i in range(100):
        body = targets["oneshot"] if i >= 40 else without_auto(targets["oneshot"])
        jobs.append(make_job(
            8600000 + i, f"cwally oneshot 09:00 KST (Oct 1) old{i}", body,
            expires=20261001000000, mdays=[1], months=[10], wdays=[-1],
        ))
    for i in range(5):
        jobs.append(make_job(
            8830000 + i, f"cwally oneshot 09:00 KST (Dec 2) off{i}",
            without_auto(targets["oneshot"]), enabled=False, expires=20261202120000,
            mdays=[2], months=[12], wdays=[-1],
        ))
    for i in range(5):
        jobs.append(make_job(
            8840000 + i, "cwally trend-monitor (15:40 KST)",
            {"ref": "main", "inputs": {}}, url=reg.TREND_URL, hours=[15], minutes=[40],
        ))
    return jobs


def split_account(jobs: list[dict], now: datetime):
    live, change, expired, unrelated, disabled = set(), set(), set(), set(), set()
    expired_change = set()
    for j in jobs:
        jid = int(j["jobId"])
        if not j.get("enabled"):
            disabled.add(jid)
            continue
        if not reg.is_port_dispatch(j):
            unrelated.add(jid)
            continue
        body = json.loads(j["extendedData"]["body"])
        decision, _name, _target = reg.oneoff_decision(jid, body)
        if reg.job_expired(j, now):
            expired.add(jid)
            if decision == "patch":
                expired_change.add(jid)
            continue
        live.add(jid)
        if decision == "patch":
            change.add(jid)
    return live, change, expired, expired_change, unrelated, disabled


def legacy_fix_calls(jobs: list[dict]) -> int:
    """Pre-change fix_slot_oneoff: list + one detail GET per enabled port job,
    plus PATCH and a read-back GET for every wanted body that differs.
    Expired jobs were included. Already-correct jobs were still fetched.
    """
    targets = reg.slot_oneoff_targets()
    enabled = 0
    writes = 0
    for j in jobs:
        if not j.get("enabled"):
            continue
        if not reg.is_port_dispatch(j):
            continue
        enabled += 1
        body = json.loads(j["extendedData"]["body"])
        name = "morning" if int(j["jobId"]) == reg.TEMPLATE_JOB_ID else body["inputs"]["job"]
        if name in targets and body != targets[name]:
            writes += 1
    return 1 + enabled + 2 * writes


class FakeAPI:
    def __init__(self, jobs: list[dict], *, list_body: bool = False, some_failed: bool = False,
                 hide_headers: set[int] | None = None, hide_schedule_on_list: set[int] | None = None):
        self.jobs = {int(j["jobId"]): copy.deepcopy(j) for j in jobs}
        self.list_body = list_body
        self.some_failed = some_failed
        self.hide_headers = hide_headers or set()
        self.hide_schedule_on_list = hide_schedule_on_list or set()
        self.calls: list[tuple[str, str]] = []
        self.patches: list[tuple[int, dict]] = []

    def _view(self, jid: int, *, detail: bool) -> dict:
        row = copy.deepcopy(self.jobs[jid])
        if not detail and not self.list_body:
            row.pop("extendedData", None)
        if jid in self.hide_headers and isinstance(row.get("extendedData"), dict):
            row["extendedData"].pop("headers", None)
        if not detail and jid in self.hide_schedule_on_list:
            row.pop("schedule", None)
        return row

    def urlopen(self, req, timeout=20):
        if not str(req.full_url).startswith(API):
            raise AssertionError("refusing non cron-job.org URL " + str(req.full_url))
        path = str(req.full_url)[len(API):]
        # Tripwire above is the default. This fake never opens a socket.
        self.calls.append((req.method, path))
        if req.method == "GET" and path == "/jobs":
            rows = [self._view(jid, detail=False) for jid in self.jobs]
            return Resp({"jobs": rows, "someFailed": self.some_failed})
        if req.method == "GET" and path.startswith("/jobs/"):
            jid = int(path.rsplit("/", 1)[-1])
            if jid not in self.jobs:
                raise http_error(404)
            return Resp({"jobDetails": self._view(jid, detail=True)})
        if req.method == "PATCH" and path.startswith("/jobs/"):
            jid = int(path.rsplit("/", 1)[-1])
            payload = json.loads(req.data.decode())
            self.patches.append((jid, payload))
            job = payload.get("job") or {}
            if "schedule" in job:
                self.jobs[jid]["schedule"] = copy.deepcopy(job["schedule"])
            if "title" in job:
                self.jobs[jid]["title"] = job["title"]
            if "extendedData" in job:
                ext = self.jobs[jid].setdefault("extendedData", {})
                new = job["extendedData"]
                ext["headers"] = copy.deepcopy(new["headers"])
                ext["body"] = new["body"]
            return Resp({})
        if req.method == "DELETE" and path.startswith("/jobs/"):
            jid = int(path.rsplit("/", 1)[-1])
            self.jobs.pop(jid, None)
            return Resp({})
        raise AssertionError(f"unexpected {req.method} {path}")


class CronjobCallTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ[k] for k in os.environ if k.startswith("CRONJOB_")}
        for k in self._env:
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen
        self._sleep = reg.time.sleep
        urllib.request.urlopen = self._tripwire
        reg.time.sleep = lambda _s: None
        reg.reset_call_stats()
        self._mono = reg.time.monotonic
        self._call = reg.call
        self._summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
        self.sleeps: list[float] = []

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        reg.time.sleep = self._sleep
        reg.time.monotonic = self._mono
        reg.call = self._call
        for k in list(os.environ):
            if k.startswith("CRONJOB_"):
                os.environ.pop(k, None)
        os.environ.update(self._env)
        if self._summary_env is None:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
        else:
            os.environ["GITHUB_STEP_SUMMARY"] = self._summary_env

    def install_clock(self) -> list[float]:
        sleeps: list[float] = []
        clock = {"t": 0.0}

        def mono() -> float:
            return clock["t"]

        def sleeper(seconds: float) -> None:
            sleeps.append(float(seconds))
            clock["t"] += float(seconds)

        reg.time.monotonic = mono
        reg.time.sleep = sleeper
        return sleeps

    def run_list_bodies(self, fake: FakeAPI, root: str, **extra: str):
        inner = fake.urlopen

        def urlopen(req, timeout=20):
            if getattr(req, "method", None) != "GET" or getattr(req, "data", None):
                raise AssertionError("list_bodies sent a write")
            return inner(req, timeout)

        fake.urlopen = urlopen
        self.use(fake)
        sleeps = self.install_clock()

        def banned(*_args, **_kwargs):
            raise AssertionError("list_bodies must not use call()")

        reg.call = banned
        vals = {
            "CRONJOB_API_KEY": "test-key",
            "CRONJOB_ACTION": "list_bodies",
            "CRONJOB_LIST_BODIES_DIR": os.path.join(root, "out"),
            "CRONJOB_CHECKPOINT": os.path.join(root, "prev", "checkpoint.json"),
            "GITHUB_STEP_SUMMARY": os.path.join(root, "summary.md"),
        }
        vals.update(extra)
        self.env(**vals)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        return rc, buf.getvalue(), sleeps

    @staticmethod
    def _tripwire(req, timeout=20):
        raise AssertionError("real HTTP is not allowed: " + getattr(req, "full_url", repr(req)))

    def use(self, fake: FakeAPI) -> None:
        urllib.request.urlopen = fake.urlopen

    def env(self, **kwargs: str) -> None:
        os.environ.update(kwargs)

    def test_register_tsla_patches_old_half_hour_schedule(self):
        job = make_job(8746217, reg.TSLA_TITLE, reg.TSLA_BODY)
        job["schedule"] = copy.deepcopy(reg.TSLA_SCHEDULE_OLD)
        job["url"] = reg.DISPATCH_URL
        fake = FakeAPI([job])
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_DRY_RUN="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.run_register_tsla(NOW)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual([c[0] for c in fake.calls if c[0] != "GET"], ["PATCH"])
        self.assertEqual(fake.patches[0][0], 8746217)
        sched = fake.patches[0][1]["job"]["schedule"]
        self.assertEqual(sched["minutes"], [0])
        self.assertEqual(sched["hours"], [10, 11, 12, 13, 14, 15, 16])
        self.assertNotIn("extendedData", fake.patches[0][1]["job"])
        fake.patches.clear()
        fake.calls.clear()
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = reg.run_register_tsla(NOW)
        self.assertEqual(rc2, reg.SKIP, buf2.getvalue())
        self.assertEqual(fake.patches, [])

    def test_morning_template_patch_keeps_url_title_and_job_id(self):
        targets = reg.slot_oneoff_targets()
        wrong = {"ref": "main", "inputs": {"job": "oneshot"}}
        jobs = [
            make_job(reg.TEMPLATE_JOB_ID, "cwally port morning (09:00 KST)", wrong),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
        ]
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_GAP_SEC="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.run_fix_slot_oneoff(NOW)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual([jid for jid, _payload in fake.patches], [reg.TEMPLATE_JOB_ID])
        self.assertEqual(set(fake.patches[0][1]["job"].keys()), {"extendedData"})
        job = fake.jobs[reg.TEMPLATE_JOB_ID]
        self.assertEqual(job["title"], "cwally port morning (09:00 KST)")
        self.assertEqual(job["url"], reg.DISPATCH_URL)
        self.assertEqual(json.loads(job["extendedData"]["body"]), reg.TEMPLATE_BODY)
        self.assertEqual(job["extendedData"]["headers"], HEADERS)

    def test_decision_keeps_morning_template_job(self):
        body = {"ref": "main", "inputs": {"job": "oneshot"}}
        decision, name, target = reg.oneoff_decision(reg.TEMPLATE_JOB_ID, body)
        self.assertEqual(decision, "patch")
        self.assertEqual(name, "morning")
        self.assertEqual(target, reg.TEMPLATE_BODY)
        self.assertEqual(reg.TEMPLATE_JOB_ID, 8588894)
        self.assertFalse(reg.job_expired({"schedule": {"expiresAt": 0}}, NOW))
        self.assertFalse(reg.job_expired({}, NOW))
        self.assertFalse(reg.job_expired({"schedule": {"expiresAt": 20261108100000}}, NOW))
        self.assertTrue(reg.job_expired({"schedule": {"expiresAt": 20261001000000}}, NOW))

    def test_default_budget_is_under_regular_daily_quota(self):
        self.assertEqual(reg.max_calls(), 90)
        self.assertLess(reg.max_calls(), 100)

    def test_list_body_skips_unchanged_expired_and_unrelated(self):
        jobs = big_account()
        live, change, expired, expired_change, unrelated, disabled = split_account(jobs, NOW)
        self.assertEqual(len(jobs), 133)
        self.assertEqual(len(live), 23)
        self.assertEqual(len(change), 7)
        self.assertEqual(len(expired), 100)
        self.assertEqual(len(expired_change), 40)
        self.assertEqual(len(unrelated), 5)
        self.assertEqual(len(disabled), 5)
        before = legacy_fix_calls(jobs)
        self.assertEqual(before, 218)

        self.env(CRONJOB_API_KEY="test-key", CRONJOB_GAP_SEC="0", CRONJOB_MAX_CALLS="90")
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.run_fix_slot_oneoff(NOW)
            reg.print_call_summary()
        text = buf.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(fake.calls), 15)
        self.assertEqual(fake.calls[0], ("GET", "/jobs"))
        patched = {jid for jid, _payload in fake.patches}
        self.assertEqual(patched, change)
        gets = [path for method, path in fake.calls if method == "GET" and path != "/jobs"]
        self.assertEqual(sorted(gets), sorted(f"/jobs/{jid}" for jid in change))
        self.assertNotIn(reg.TEMPLATE_JOB_ID, patched)
        for jid, payload in fake.patches:
            self.assertEqual(set(payload["job"].keys()), {"extendedData"})
            stored = json.loads(fake.jobs[jid]["extendedData"]["body"])
            self.assertEqual(stored["inputs"]["oneoff_mode"], "auto")
            self.assertEqual(fake.jobs[jid]["extendedData"]["headers"], HEADERS)
        touched = " ".join(path for _m, path in fake.calls)
        for jid in expired | unrelated | disabled:
            self.assertNotIn(f"/jobs/{jid}", touched)
        self.assertIn("ALL_DISPATCHERS_AUTO=1", text)
        self.assertIn("unchanged-from-list:", text)
        self.assertNotIn("test-key", text)
        self.assertNotIn("test-token", text)
        self.assertIn("cronjob api calls: total=15 budget=90 GET=8 PATCH=7 PUT=0 DELETE=0", text)
        self.assertIn("detail=0 patch=7 readback=7", text)
        print(f"BEFORE_CALLS={before}")
        print(f"AFTER_CALLS_LIST_BODY={len(fake.calls)}")

    def test_no_list_body_reads_only_live_jobs(self):
        jobs = big_account()
        live, change, expired, _expired_change, unrelated, disabled = split_account(jobs, NOW)
        before = legacy_fix_calls(jobs)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_GAP_SEC="0")
        fake = FakeAPI(jobs, list_body=False)
        self.use(fake)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.run_fix_slot_oneoff(NOW)
        text = buf.getvalue()
        self.assertEqual(rc, 0, text)
        # 1 list + one detail per live job + patch and read-back only for the 7 changes.
        self.assertEqual(len(fake.calls), 1 + len(live) + 2 * len(change))
        self.assertEqual(len(fake.calls), 38)
        from collections import Counter
        detail = Counter(path for method, path in fake.calls if method == "GET" and path != "/jobs")
        for jid in live:
            self.assertGreaterEqual(detail[f"/jobs/{jid}"], 1)
        for jid in change:
            self.assertEqual(detail[f"/jobs/{jid}"], 2)
        for jid in live - change:
            self.assertEqual(detail[f"/jobs/{jid}"], 1)
        touched = " ".join(path for _m, path in fake.calls)
        for jid in expired | unrelated | disabled:
            self.assertNotIn(f"/jobs/{jid}", touched)
        self.assertEqual({jid for jid, _p in fake.patches}, change)
        self.assertIn("detail=23 patch=7 readback=7", text)
        self.assertIn("expired_skip=100", text)
        self.assertNotIn("test-token", text)
        print(f"BEFORE_CALLS={before}")
        print(f"AFTER_CALLS_NO_LIST_BODY={len(fake.calls)}")

    def test_budget_stops_before_the_next_request(self):
        targets = reg.slot_oneoff_targets()
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(8900001, "cwally oneshot 09:10 KST (Dec 1) a", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
            make_job(8900002, "cwally oneshot 09:10 KST (Dec 1) b", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
            make_job(8900003, "cwally oneshot 09:10 KST (Dec 1) c", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=False)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff",
                 CRONJOB_GAP_SEC="0", CRONJOB_MAX_CALLS="5")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        text = buf.getvalue()
        self.assertEqual(rc, reg.API_ERROR, text)
        self.assertEqual(len(fake.calls), 5)
        self.assertNotIn("/jobs/8900002", " ".join(p for _m, p in fake.calls))
        self.assertIn("::error::", text)
        self.assertIn("call budget reached (5/5) before GET /jobs/8900002", text)
        self.assertIn("cronjob api calls: total=5 budget=5", text)
        self.assertEqual(fake.patches, [])

    def test_patch_is_not_sent_when_readback_would_pass_the_cap(self):
        targets = reg.slot_oneoff_targets()
        oneshot_id = 8811001
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(oneshot_id, "cwally oneshot 09:10 KST (Dec 1) need", without_auto(targets["oneshot"]),
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff",
                 CRONJOB_GAP_SEC="0", CRONJOB_MAX_CALLS="2")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        text = buf.getvalue()
        self.assertEqual(rc, reg.API_ERROR, text)
        self.assertEqual(fake.calls, [("GET", "/jobs")])
        self.assertEqual(fake.patches, [])
        self.assertIn(f"before PATCH /jobs/{oneshot_id} + read-back", text)
        self.assertIn("::error::", text)

    def test_patch_and_readback_fit_in_the_budget(self):
        targets = reg.slot_oneoff_targets()
        oneshot_id = 8811002
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(oneshot_id, "cwally oneshot 09:10 KST (Dec 1) need", without_auto(targets["oneshot"]),
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff",
                 CRONJOB_GAP_SEC="0", CRONJOB_MAX_CALLS="3")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(
            [(m, p) for m, p in fake.calls],
            [("GET", "/jobs"), ("PATCH", f"/jobs/{oneshot_id}"), ("GET", f"/jobs/{oneshot_id}")],
        )
        stored = json.loads(fake.jobs[oneshot_id]["extendedData"]["body"])
        self.assertEqual(stored, targets["oneshot"])
        self.assertEqual(fake.patches[0][1]["job"]["extendedData"]["headers"], HEADERS)
        self.assertIn("ALL_DISPATCHERS_AUTO=1", buf.getvalue())

    def test_invalid_budget_makes_no_request(self):
        fake = FakeAPI([], list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff", CRONJOB_MAX_CALLS="nope")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, reg.BAD_INPUT)
        self.assertEqual(fake.calls, [])
        self.assertIn("::error::", buf.getvalue())
        self.assertIn("CRONJOB_MAX_CALLS", buf.getvalue())

    def test_zero_budget_aborts_before_list(self):
        targets = reg.slot_oneoff_targets()
        fake = FakeAPI([make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"])])
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff", CRONJOB_MAX_CALLS="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, reg.API_ERROR)
        self.assertEqual(fake.calls, [])
        self.assertIn("call budget reached (0/0) before GET /jobs", buf.getvalue())

    def test_retry_429_stays_off_unless_asked(self):
        def urlopen(req, timeout=20):
            raise http_error(429)

        urllib.request.urlopen = urlopen
        reg.time.sleep = lambda s: self.sleeps.append(s)
        with self.assertRaises(reg.Stop) as caught:
            reg.call("GET", "/jobs/1", "test-key")
        self.assertEqual(caught.exception.code, reg.API_ERROR)
        self.assertIn("HTTP 429 (no Retry-After header)", str(caught.exception))
        self.assertEqual(self.sleeps, [])
        self.assertEqual(reg._STATS["n"], 1)

    def test_retry_429_counts_every_attempt_and_honors_budget(self):
        calls = {"n": 0}

        def urlopen(req, timeout=20):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http_error(429)
            return Resp({"ok": True})

        urllib.request.urlopen = urlopen
        reg.time.sleep = lambda s: self.sleeps.append(s)
        self.env(CRONJOB_RETRY_429="1", CRONJOB_MAX_CALLS="10")
        got = reg.call("GET", "/jobs/1", "test-key")
        self.assertEqual(got, {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.sleeps, [40, 80])
        self.assertEqual(reg._STATS["n"], 3)

        reg.reset_call_stats()
        calls["n"] = 0
        self.sleeps.clear()
        self.env(CRONJOB_RETRY_429="1", CRONJOB_MAX_CALLS="2")

        def always_429(req, timeout=20):
            calls["n"] += 1
            raise http_error(429)

        urllib.request.urlopen = always_429
        with self.assertRaises(reg.Stop) as caught:
            reg.call("GET", "/jobs/9", "test-key")
        self.assertIn("call budget reached (2/2) before GET /jobs/9", str(caught.exception))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.sleeps, [40, 80])

    def test_delete_uses_the_list_and_not_per_job_gets(self):
        oneshot = make_job(
            8755023, "cwally oneshot 18:00 KST (Nov 19) steelre",
            {"ref": "main", "inputs": {"job": "oneshot", "oneoff_mode": "auto"}},
            expires=20261119185959, hours=[18], minutes=[0], mdays=[19], months=[11], wdays=[-1],
        )
        other = make_job(
            8755024, "cwally oneshot 18:00 KST (Nov 20) steelre",
            {"ref": "main", "inputs": {"job": "oneshot", "oneoff_mode": "auto"}},
            expires=20261120185959, hours=[18], minutes=[0], mdays=[20], months=[11], wdays=[-1],
        )
        fake = FakeAPI([oneshot, other, make_job(8588894, "cwally port morning (09:00 KST)",
                                                  reg.slot_oneoff_targets()["morning"])])
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="delete", CRONJOB_JOB_IDS="8755023,8755024")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(fake.calls, [
            ("GET", "/jobs"),
            ("DELETE", "/jobs/8755023"),
            ("DELETE", "/jobs/8755024"),
        ])
        self.assertIn("cronjob api calls: total=3 budget=90 GET=1 PATCH=0 PUT=0 DELETE=2", buf.getvalue())

    def test_delete_refuses_morning_template_without_a_request(self):
        fake = FakeAPI([make_job(8588894, "cwally port morning (09:00 KST)", reg.TEMPLATE_BODY)])
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="delete", CRONJOB_JOB_IDS="8588894")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, reg.BAD_INPUT)
        self.assertEqual(fake.calls, [])
        self.assertIn("morning template", buf.getvalue())

    def test_delete_refuses_the_slot_job_and_deletes_nothing(self):
        slot = make_job(reg.SLOT_JOB_ID, reg.SLOT_TITLE, reg.SLOT_BODY, hours=[19])
        oneshot = make_job(
            8755023, "cwally oneshot 18:00 KST (Nov 19) steelre",
            reg.slot_oneoff_targets()["oneshot"],
            expires=20261119185959, hours=[18], minutes=[0], mdays=[19], months=[11], wdays=[-1],
        )
        fake = FakeAPI([slot, oneshot])
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="delete",
                 CRONJOB_JOB_IDS=f"{reg.SLOT_JOB_ID},8755023")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, reg.BAD_TEMPLATE, buf.getvalue())
        self.assertEqual(fake.calls, [("GET", "/jobs")])
        self.assertIn(reg.SLOT_JOB_ID, fake.jobs)
        self.assertIn(8755023, fake.jobs)

    def test_dry_run_does_not_patch(self):
        targets = reg.slot_oneoff_targets()
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(8812001, "cwally oneshot 09:10 KST (Dec 1) dry", without_auto(targets["oneshot"]),
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff",
                 CRONJOB_DRY_RUN="1", CRONJOB_GAP_SEC="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        text = buf.getvalue()
        self.assertEqual(rc, reg.API_ERROR, text)
        self.assertEqual(fake.calls, [("GET", "/jobs")])
        self.assertEqual(fake.patches, [])
        self.assertIn("dry-run would patch", text)
        self.assertIn('"oneoff_mode": "auto"', text)

    def test_other_job_missing_oneoff_is_not_rewritten_or_refetched(self):
        targets = reg.slot_oneoff_targets()
        odd = {"ref": "main", "inputs": {"job": "grab", "oneoff_mode": "dry"}}
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(8821001, "cwally custom odd", odd),
        ]
        fake = FakeAPI(jobs, list_body=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff", CRONJOB_GAP_SEC="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        text = buf.getvalue()
        self.assertEqual(rc, reg.API_ERROR, text)
        self.assertEqual(fake.calls, [("GET", "/jobs")])
        self.assertIn("unexpected oneoff='dry'", text)
        self.assertEqual(fake.patches, [])

    def test_missing_list_schedule_still_skips_an_expired_detail(self):
        targets = reg.slot_oneoff_targets()
        special = 8601992
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(special, "cwally oneshot 16:22 KST (Oct 28) sdichg", without_auto(targets["oneshot"]),
                     expires=20261028162200, hours=[16], minutes=[22], mdays=[28], months=[10], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=False, hide_schedule_on_list={special})
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_GAP_SEC="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.run_fix_slot_oneoff(NOW)
        text = buf.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertEqual(fake.patches, [])
        self.assertIn(f"skip-expired-after-read: id={special}", text)
        methods = [(m, p) for m, p in fake.calls]
        self.assertIn(("GET", f"/jobs/{special}"), methods)
        self.assertNotIn(("PATCH", f"/jobs/{special}"), methods)

    def test_refuses_to_patch_when_headers_are_missing(self):
        targets = reg.slot_oneoff_targets()
        oneshot_id = 8813001
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(8588897, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"]),
            make_job(oneshot_id, "cwally oneshot 09:10 KST (Dec 1) nohdr", without_auto(targets["oneshot"]),
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]
        fake = FakeAPI(jobs, list_body=True, hide_headers={oneshot_id})
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff", CRONJOB_GAP_SEC="0")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        text = buf.getvalue()
        self.assertEqual(rc, reg.API_ERROR, text)
        self.assertEqual(fake.patches, [])
        self.assertIn("refusing patch without headers", text)
        self.assertEqual(
            [c for c in fake.calls if c[0] == "PATCH"],
            [],
        )
        # List showed a body but no headers, so one detail GET, then stop. No write.
        self.assertIn(("GET", f"/jobs/{oneshot_id}"), fake.calls)

    def test_incomplete_list_stops_before_detail(self):
        targets = reg.slot_oneoff_targets()
        fake = FakeAPI([
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
        ], some_failed=True)
        self.use(fake)
        self.env(CRONJOB_API_KEY="test-key", CRONJOB_ACTION="fix_slot_oneoff")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reg.main()
        self.assertEqual(rc, reg.API_ERROR)
        self.assertEqual(fake.calls, [("GET", "/jobs")])
        self.assertIn("someFailed", buf.getvalue())

    def test_list_bodies_classifies_and_never_writes(self):
        self.assertEqual(reg.LIST_BODIES_GAP_SEC, 3.0)
        targets = reg.slot_oneoff_targets()
        afternoon = make_job(
            8588897, "cwally port afternoon (15:20 KST), extra | note",
            without_auto(targets["afternoon"]), hours=[15], minutes=[20],
        )
        bad = make_job(8850001, "cwally bad body", targets["oneshot"])
        bad["extendedData"]["body"] = "not-json"
        jobs = [
            make_job(8588894, "cwally port morning (09:00 KST)", targets["morning"]),
            afternoon,
            make_job(8588895, "cwally port 0090b0 (15:41 KST)", targets["0090b0"],
                     hours=[15], minutes=[41]),
            make_job(8810001, "cwally oneshot 09:10 KST (Dec 1) fut", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
            make_job(8821001, "cwally custom odd",
                     {"ref": "main", "inputs": {"job": "grab", "oneoff_mode": "dry"}}),
            make_job(8830001, "cwally oneshot 09:10 KST (Dec 2) off",
                     without_auto(targets["oneshot"]), enabled=False,
                     expires=20271202120000, mdays=[2], months=[12], wdays=[-1]),
            bad,
            make_job(8840001, "cwally trend-monitor (15:40 KST)",
                     {"ref": "main", "inputs": {}}, url=reg.TREND_URL,
                     hours=[15], minutes=[40]),
        ]
        fake = FakeAPI(jobs, list_body=False, some_failed=True)
        with tempfile.TemporaryDirectory() as root:
            rc, text, sleeps = self.run_list_bodies(
                fake, root, CRONJOB_DRY_RUN="1", CRONJOB_RETRY_429="1", CRONJOB_MAX_CALLS="1",
            )
            self.assertEqual(rc, 0, text)
            self.assertEqual(fake.patches, [])
            self.assertTrue(all(method == "GET" for method, _path in fake.calls))
            self.assertNotIn(("GET", "/jobs/8840001"), fake.calls)
            detail = [path for method, path in fake.calls if path != "/jobs"]
            self.assertEqual(detail, [
                "/jobs/8588894", "/jobs/8588895", "/jobs/8588897", "/jobs/8810001",
                "/jobs/8821001", "/jobs/8830001", "/jobs/8850001",
            ])
            self.assertEqual(sleeps, [3.0] * (len(fake.calls) - 1))
            self.assertNotIn("call budget reached", text)
            blob = json.loads(open(os.path.join(root, "out", "checkpoint.json"), encoding="utf-8").read())
            self.assertEqual(blob["status"], "completed")
            self.assertFalse(blob["stopped_on_429"])
            self.assertIsNone(blob["stopping_jobId"])
            self.assertTrue(blob["read_only"])
            self.assertEqual(blob["methods"], ["GET"])
            self.assertEqual(blob["gap_sec"], 3.0)
            self.assertTrue(blob["list_some_failed"])
            self.assertEqual(blob["total_dispatchers"], 7)
            self.assertEqual(blob["auto_count"], 3)
            self.assertEqual(blob["missing_or_not_auto"], [8588897, 8821001, 8830001, 8850001])
            self.assertEqual(blob["not_yet_read"], [])
            by_id = {row["jobId"]: row for row in blob["jobs"]}
            self.assertEqual(by_id[8588894]["class"], "morning")
            self.assertEqual(by_id[8588894]["oneoff_mode"], "auto")
            self.assertEqual(by_id[8588894]["job"], "morning")
            self.assertEqual(by_id[8588897]["class"], "afternoon")
            self.assertEqual(by_id[8588897]["oneoff_mode"], "MISSING")
            self.assertEqual(by_id[8588895]["class"], "0090b0")
            self.assertEqual(by_id[8810001]["class"], "oneshot")
            self.assertEqual(by_id[8821001]["class"], "other")
            self.assertEqual(by_id[8821001]["oneoff_mode"], "dry")
            self.assertEqual(by_id[8830001]["enabled"], False)
            self.assertEqual(by_id[8830001]["class"], "oneshot")
            self.assertEqual(by_id[8850001]["job"], "MISSING")
            self.assertEqual(by_id[8850001]["oneoff_mode"], "MISSING")
            self.assertEqual(by_id[8850001]["class"], "other")
            self.assertNotIn(8840001, by_id)
            raw = json.dumps(blob)
            summary = open(os.path.join(root, "summary.md"), encoding="utf-8").read()
            with open(os.path.join(root, "out", "bodies.csv"), encoding="utf-8", newline="") as fh:
                csv_rows = list(csv.reader(fh))
            dumped = text + raw + summary + json.dumps(csv_rows)
            for secret in ("test-key", "test-token", "Bearer", "Authorization", "extendedData"):
                self.assertNotIn(secret, dumped)
            self.assertEqual(csv_rows[0], ["jobId", "title", "enabled", "schedule", "job", "oneoff_mode", "class"])
            titles = {row[0]: row[1] for row in csv_rows[1:]}
            self.assertEqual(titles["8588897"], "cwally port afternoon (15:20 KST), extra | note")
            self.assertIn("extra \\| note", summary)
            self.assertIn("status: completed", summary)
            self.assertIn("stopped on 429: no", summary)
            self.assertIn("total dispatchers: 7", summary)
            self.assertIn("oneoff_mode=auto: 3", summary)
            self.assertIn("missing or not auto jobIds: 8588897, 8821001, 8830001, 8850001", summary)
            self.assertIn("LIST_BODIES status=completed stopped_on_429=0 total_dispatchers=7 auto=3", text)
            self.assertIn("list_bodies api: GET=8 PATCH=0 PUT=0 DELETE=0 POST=0 gap_sec=3", text)
            self.assertIn("::warning::cron-job.org job list is incomplete (someFailed)", text)

    def test_list_bodies_stops_on_429_without_retry_and_resumes(self):
        targets = reg.slot_oneoff_targets()
        jobs = [
            make_job(10, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(20, "cwally port afternoon (15:20 KST)", targets["afternoon"], hours=[15], minutes=[20]),
            make_job(30, "cwally oneshot 09:10 KST (Dec 1) a", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
            make_job(40, "cwally oneshot 09:10 KST (Dec 1) b", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
        ]

        class StopAt(FakeAPI):
            def __init__(self):
                super().__init__(jobs, list_body=False)
                self.stop_hits = 0

            def urlopen(self, req, timeout=20):
                path = str(req.full_url)[len(API):]
                if path == "/jobs/30":
                    self.calls.append((req.method, path))
                    self.stop_hits += 1
                    raise http_error(429, "1")
                return super().urlopen(req, timeout)

        fake = StopAt()
        with tempfile.TemporaryDirectory() as root:
            rc, text, sleeps = self.run_list_bodies(
                fake, root, CRONJOB_RETRY_429="1", CRONJOB_MAX_CALLS="2",
            )
            self.assertEqual(rc, 0, text)
            self.assertEqual(fake.stop_hits, 1)
            self.assertEqual(fake.calls, [
                ("GET", "/jobs"), ("GET", "/jobs/10"), ("GET", "/jobs/20"), ("GET", "/jobs/30"),
            ])
            self.assertEqual(sleeps, [3.0, 3.0, 3.0])
            self.assertNotIn(1.0, sleeps)
            self.assertNotIn(40, sleeps)
            self.assertEqual(fake.patches, [])
            blob = json.loads(open(os.path.join(root, "out", "checkpoint.json"), encoding="utf-8").read())
            self.assertEqual(blob["status"], "stopped_429")
            self.assertTrue(blob["stopped_on_429"])
            self.assertEqual(blob["stopping_jobId"], 30)
            self.assertEqual(blob["last_read_jobId"], 20)
            self.assertEqual(blob["resume_after"], 20)
            self.assertEqual([row["jobId"] for row in blob["jobs"]], [10, 20])
            self.assertEqual(blob["not_yet_read"], [30, 40])
            self.assertIn("HTTP 429 on GET /jobs/30, stopped without retry", text)
            self.assertIn("LIST_BODIES status=stopped_429 stopped_on_429=1", text)
            self.assertIn("stopping_jobId=30", text)
            summary = open(os.path.join(root, "summary.md"), encoding="utf-8").read()
            self.assertIn("stopped on 429: yes", summary)
            self.assertIn("stopping jobId: 30", summary)

            class Open(FakeAPI):
                def urlopen(self, req, timeout=20):
                    if req.method != "GET":
                        raise AssertionError(req.method)
                    return FakeAPI.urlopen(self, req, timeout)

            nxt = Open(jobs, list_body=False)
            os.makedirs(os.path.join(root, "prev"), exist_ok=True)
            checkpoint = os.path.join(root, "out", "checkpoint.json")
            os.replace(checkpoint, os.path.join(root, "prev", "checkpoint.json"))
            rc2, text2, sleeps2 = self.run_list_bodies(nxt, root)
            self.assertEqual(rc2, 0, text2)
            self.assertEqual(nxt.calls, [
                ("GET", "/jobs"), ("GET", "/jobs/30"), ("GET", "/jobs/40"),
            ])
            self.assertEqual(sleeps2, [3.0, 3.0])
            blob2 = json.loads(open(os.path.join(root, "out", "checkpoint.json"), encoding="utf-8").read())
            self.assertEqual(blob2["status"], "completed")
            self.assertEqual(blob2["total_dispatchers"], 4)
            self.assertEqual(blob2["auto_count"], 4)
            self.assertEqual(blob2["missing_or_not_auto"], [])
            self.assertEqual(blob2["not_yet_read"], [])
            self.assertIsNone(blob2["stopping_jobId"])
            self.assertIn("LIST_BODIES status=completed", text2)

    def test_list_bodies_resume_after_skips_ids_missing_from_checkpoint(self):
        targets = reg.slot_oneoff_targets()
        jobs = [
            make_job(10, "cwally port morning (09:00 KST)", targets["morning"]),
            make_job(20, "cwally port afternoon (15:20 KST)", targets["afternoon"]),
            make_job(30, "cwally oneshot 09:10 KST (Dec 1) a", targets["oneshot"],
                     expires=20271201120000, mdays=[1], months=[12], wdays=[-1]),
            make_job(40, "cwally custom odd",
                     {"ref": "main", "inputs": {"job": "grab"}}),
        ]
        fake = FakeAPI(jobs, list_body=False)
        with tempfile.TemporaryDirectory() as root:
            rc, text, sleeps = self.run_list_bodies(fake, root, CRONJOB_RESUME_AFTER="20")
            self.assertEqual(rc, 0, text)
            self.assertEqual([path for _method, path in fake.calls], ["/jobs", "/jobs/30", "/jobs/40"])
            self.assertEqual(sleeps, [3.0, 3.0])
            blob = json.loads(open(os.path.join(root, "out", "checkpoint.json"), encoding="utf-8").read())
            self.assertEqual(blob["status"], "incomplete")
            self.assertEqual(blob["not_yet_read"], [10, 20])
            self.assertEqual(blob["auto_count"], 1)
            self.assertEqual(blob["missing_or_not_auto"], [40])
            self.assertEqual(blob["total_dispatchers"], 4)
            self.assertFalse(blob["stopped_on_429"])

    def test_list_bodies_429_on_list(self):
        class List429(FakeAPI):
            def urlopen(self, req, timeout=20):
                path = str(req.full_url)[len(API):]
                self.calls.append((req.method, path))
                raise http_error(429)

        fake = List429([])
        with tempfile.TemporaryDirectory() as root:
            rc, text, sleeps = self.run_list_bodies(fake, root, CRONJOB_RETRY_429="1")
            self.assertEqual(rc, 0, text)
            self.assertEqual(fake.calls, [("GET", "/jobs")])
            self.assertEqual(sleeps, [])
            blob = json.loads(open(os.path.join(root, "out", "checkpoint.json"), encoding="utf-8").read())
            self.assertEqual(blob["status"], "stopped_429")
            self.assertIsNone(blob["stopping_jobId"])
            self.assertIsNone(blob["total_dispatchers"])
            self.assertIn("total_dispatchers=unknown", text)
            self.assertIn("HTTP 429 on GET /jobs, stopped without retry", text)

    def test_list_bodies_bad_input_makes_no_request(self):
        fake = FakeAPI([make_job(10, "cwally port morning (09:00 KST)", reg.TEMPLATE_BODY)])
        with tempfile.TemporaryDirectory() as root:
            rc, text, _sleeps = self.run_list_bodies(fake, root, CRONJOB_RESUME_AFTER="nope")
            self.assertEqual(rc, reg.BAD_INPUT, text)
            self.assertEqual(fake.calls, [])
            self.assertNotIn("test-key", text)
            bad = os.path.join(root, "prev", "checkpoint.json")
            os.makedirs(os.path.dirname(bad), exist_ok=True)
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{")
            rc2, text2, _sleeps2 = self.run_list_bodies(fake, root, CRONJOB_RESUME_AFTER="10")
            self.assertEqual(rc2, reg.BAD_INPUT, text2)
            self.assertEqual(fake.calls, [])
        fake2 = FakeAPI([])
        self.use(fake2)
        os.environ.pop("CRONJOB_API_KEY", None)
        self.env(CRONJOB_ACTION="list_bodies")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc3 = reg.main()
        self.assertEqual(rc3, reg.BAD_INPUT)
        self.assertEqual(fake2.calls, [])
        self.assertIn("CRONJOB_API_KEY is not set", buf.getvalue())

    def test_workflow_list_bodies_is_read_only_and_oneoff_default_stays_auto(self):
        workflow = open(os.path.join(REPO, ".github/workflows/cronjob-register.yml"), encoding="utf-8").read()
        head, job = workflow.split("\n  list_bodies:\n", 1)
        self.assertIn("list_bodies", head.split("options:", 1)[1].split("\n", 1)[0])
        self.assertIn("fix_slot_oneoff", head.split("options:", 1)[1].split("\n", 1)[0])
        self.assertIn("if: inputs.action != 'list_bodies'", head)
        self.assertIn("ops_notify.py", head)
        self.assertNotIn("ops_notify", job)
        self.assertNotIn("ntfy", job)
        self.assertNotIn("PATCH", job)
        self.assertNotIn("DELETE", job)
        self.assertIn("CRONJOB_ACTION: list_bodies", job)
        self.assertIn("actions/upload-artifact@v4", job)
        port = open(os.path.join(REPO, ".github/workflows/port-alerts.yml"), encoding="utf-8").read()
        match = re.search(r"\n      oneoff_mode:\n        description:.*\n        default: (\w+)\n", port)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "auto")


if __name__ == "__main__":
    unittest.main()
