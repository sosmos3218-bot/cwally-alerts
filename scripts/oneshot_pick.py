#!/usr/bin/env python3
"""Pick and run due one-shots for an on-time external trigger (workflow_dispatch job=oneshot).

Source of truth: `oneshot_kst` in configs/<name>.json (AGENTS.md). An external cron (cron-job.org)
calls port-alerts.yml with {"job":"oneshot"} at each one-shot time. This script decides which
one-shots are due *now*, so the dispatcher needs no per-alert job id or time mapping.

Each one-shot occurrence runs at most once: key "<name>@<YYYY-MM-DD HH:MM>" is recorded in
state/slots/oneshot.json. The GitHub one-shot crons stay as the late fallback; their Decide branch
calls `check-cron`, which skips an occurrence that the on-time dispatch already ran.

  python scripts/oneshot_pick.py pick  [--now "YYYY-MM-DD HH:MM"] [--grace 60] [--out PLAN]
        exit 0 = something due (plan written), 10 = nothing due, other = error
        (exit 1 is left to crashes: an uncaught Python exception must never look like "skip")
  python scripts/oneshot_pick.py run   PLAN
        marks every key in the plan first (same as slot_guard), then runs each command in
        isolation; exit 1 if any command failed
  python scripts/oneshot_pick.py check-cron JOB "<utc cron>" [--now ...]
        prints the occurrence key; exit 0 = run, 10 = already ran, 3 = not in any oneshot_kst
  python scripts/oneshot_pick.py mark KEY [KEY ...]
  python scripts/oneshot_pick.py plan  --from YYYY-MM-DD --to YYYY-MM-DD
        lists occurrences and how each is triggered (for cron-job.org registration)

Config fields read: oneshot_kst (list of "YYYY-MM-DD HH:MM" KST), optional oneshot_args
({"YYYY-MM-DD HH:MM": "<mode arg>"}), schedule_kst (to detect occurrences already covered by
the 09:00 / 15:20 port slots).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
MARKS = ROOT / "state" / "slots" / "oneshot.json"

# Port slot times whose bundle already runs these alerts (morning 09:00, afternoon 15:20).
# An occurrence at one of these times, for an alert whose schedule_kst lists that time, is run by
# the slot itself, not by the one-shot picker.
PORT_SLOT_TIMES = {"09:00", "15:20"}
# Mode argument when the config has no oneshot_args entry. Mirrors the existing run steps:
# "Run oklo" uses `event`, JOB=hce forces hce_mode=daily.
DEFAULT_ARG = {"oklo": "event", "hce": "daily"}
DEFAULT_GRACE_MIN = 60
NOTHING = 10  # "skip" exit code; 1 is what a crash returns
KEEP_DAYS = 60


def now_kst(arg: str | None) -> datetime:
    if arg:
        return datetime.strptime(arg, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return datetime.now(KST)


def parse_at(text: str) -> datetime:
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def schedule_times(cfg: dict) -> set[str]:
    return set(re.findall(r"\b\d{2}:\d{2}\b", str(cfg.get("schedule_kst") or "")))


def occurrences() -> list[dict]:
    """All one-shot occurrences from configs, sorted by time."""
    out = []
    for path in sorted(CONFIGS.glob("*.json")):
        name = path.stem
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config JSON error: {path.name}: {exc}")
        times = cfg.get("oneshot_kst") or []
        if not isinstance(times, list):
            continue
        args = cfg.get("oneshot_args") or {}
        sched = schedule_times(cfg)
        for raw in times:
            try:
                at = parse_at(raw)
            except ValueError:
                raise SystemExit(f"bad oneshot_kst in {path.name}: {raw!r}")
            hhmm = at.strftime("%H:%M")
            via = "slot" if (hhmm in PORT_SLOT_TIMES and hhmm in sched and at.weekday() < 5) else "oneshot"
            arg = args.get(raw.strip()) or DEFAULT_ARG.get(name, "auto")
            out.append({
                "name": name,
                "at": at.strftime("%Y-%m-%d %H:%M"),
                "key": f"{name}@{at.strftime('%Y-%m-%d %H:%M')}",
                "via": via,
                "cmd": ["python", f"scripts/{name}_monitor.py", arg],
            })
    out.sort(key=lambda o: (o["at"], o["name"]))
    return out


def load_marks() -> dict:
    try:
        data = json.loads(MARKS.read_text(encoding="utf-8"))
        return data.get("done", {}) if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_marks(done: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    done = {k: v for k, v in done.items() if k.split("@", 1)[-1][:10] >= cutoff}
    MARKS.parent.mkdir(parents=True, exist_ok=True)
    MARKS.write_text(json.dumps({"done": dict(sorted(done.items()))}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def opt(argv: list[str], flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def cmd_pick(argv: list[str]) -> int:
    now = now_kst(opt(argv, "--now"))
    grace = timedelta(minutes=int(opt(argv, "--grace", DEFAULT_GRACE_MIN)))
    done = load_marks()
    due, missed = [], []
    for o in occurrences():
        if o["via"] != "oneshot" or o["key"] in done:
            continue
        at = parse_at(o["at"])
        if at <= now < at + grace:
            if not (ROOT / o["cmd"][1]).exists():
                print(f"::error::missing script {o['cmd'][1]} for {o['key']}")
                return 2
            due.append(o)
        elif at + grace <= now and at.date() == now.date():
            missed.append(o["key"])
    for k in missed:
        print(f"past grace, left to GitHub fallback cron: {k}")
    print(f"now={now.strftime('%Y-%m-%d %H:%M')} KST due={[o['key'] for o in due]}")
    out = opt(argv, "--out")
    if out:
        Path(out).write_text(json.dumps(due, ensure_ascii=False), encoding="utf-8")
    return 0 if due else NOTHING


def cmd_run(argv: list[str]) -> int:
    plan = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    now = datetime.now(KST)
    done = load_marks()
    for o in plan:  # mark first: a crash must not make the fallback cron send twice
        done[o["key"]] = now.isoformat(timespec="seconds")
    save_marks(done, now)
    failed = 0
    for o in plan:
        print(f"::group::{o['key']} -> {' '.join(o['cmd'])}")
        rc = subprocess.call(o["cmd"], cwd=ROOT)
        if rc != 0:
            print(f"::error::{o['key']} failed (exit {rc})")
            failed = 1
        print("::endgroup::")
    return failed


def cron_to_kst(cron: str, ref: datetime) -> datetime | None:
    parts = cron.split()
    if len(parts) != 5 or not all(p.isdigit() for p in parts[:4]):
        return None  # repeating cron, not a one-shot
    mi, h, d, mo = (int(p) for p in parts[:4])
    utc = datetime(ref.astimezone(timezone.utc).year, mo, d, h, mi, tzinfo=timezone.utc)
    return utc.astimezone(KST)


def cmd_check_cron(argv: list[str]) -> int:
    job, cron = argv[0], argv[1]
    at = cron_to_kst(cron, now_kst(opt(argv, "--now")))
    if at is None:
        return 3
    key = f"{job}@{at.strftime('%Y-%m-%d %H:%M')}"
    if key not in {o["key"] for o in occurrences()}:
        print(key)
        return 3
    print(key)
    return NOTHING if key in load_marks() else 0


def cmd_mark(argv: list[str]) -> int:
    now = datetime.now(KST)
    done = load_marks()
    for k in argv:
        done[k] = now.isoformat(timespec="seconds")
    save_marks(done, now)
    return 0


def cmd_plan(argv: list[str]) -> int:
    lo, hi = opt(argv, "--from", "0000-00-00"), opt(argv, "--to", "9999-99-99")
    for o in occurrences():
        if lo <= o["at"][:10] <= hi:
            print(f"{o['at']}  {o['via']:7}  {o['name']:10} {' '.join(o['cmd'][2:])}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    handlers = {"pick": cmd_pick, "run": cmd_run, "check-cron": cmd_check_cron, "mark": cmd_mark, "plan": cmd_plan}
    if cmd not in handlers or (cmd in {"run", "mark"} and not rest) or (cmd == "check-cron" and len(rest) < 2):
        print(__doc__)
        return 2
    return handlers[cmd](rest)


if __name__ == "__main__":
    raise SystemExit(main())
