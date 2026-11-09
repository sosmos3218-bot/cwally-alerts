#!/usr/bin/env python3
"""Offline tests for cronjob_register call budget.

The HTTP layer is a fake account. urlopen is replaced before any request.
A tripwire raises if a test reaches the real urllib opener. Do not point
this file at api.cron-job.org, ntfy.sh, or a workflow dispatch.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from email.message import Message

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cronjob_register as reg  # noqa: E402

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
            ext = self.jobs[jid].setdefault("extendedData", {})
            new = payload["job"]["extendedData"]
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
        self.sleeps: list[float] = []

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        reg.time.sleep = self._sleep
        for k in list(os.environ):
            if k.startswith("CRONJOB_"):
                os.environ.pop(k, None)
        os.environ.update(self._env)

    @staticmethod
    def _tripwire(req, timeout=20):
        raise AssertionError("real HTTP is not allowed: " + getattr(req, "full_url", repr(req)))

    def use(self, fake: FakeAPI) -> None:
        urllib.request.urlopen = fake.urlopen

    def env(self, **kwargs: str) -> None:
        os.environ.update(kwargs)

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


if __name__ == "__main__":
    unittest.main()
