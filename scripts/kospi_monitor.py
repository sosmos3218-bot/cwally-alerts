#!/usr/bin/env python3
"""[KOSPI] one 15:40 KOSPI ladder message (2026-10-26 audit, approved in chat 18:22 KST).

Merges kosdown (5550/5450/5300/5200/5000 down), kosup (5750/6000 up), kosbrk (7/29 close 5663.24, intraday low
5262.77, foreign selling re-expansion, guidance keywords) and kos5500 (5500 hold / break / 5800 recovery
scenario). The four component scripts still run on their own run_one lines in flow-alerts 15:40 with their own
configs/<name>.json, state/<name>.json, thresholds and re-arm logic, unchanged. The flow step sets KOSPI_LADDER=1,
so their signal sends go through scripts/kospi_spool.py into a spool file instead of ntfy; failure notices
(조회 실패 and the like, routed to cwally-ops) still go out directly.

  python scripts/kospi_monitor.py begin   before the components: clear the spool, snapshot component states
  python scripts/kospi_monitor.py send    after them: publish ONE [KOSPI] message with every new condition
  python scripts/kospi_monitor.py dry     print what send would publish (no ntfy, no state change)
  python scripts/kospi_monitor.py selftest

kos5500 used to report every trading day at 15:40 and again at the next 09:00 (Naver revision check). In the
ladder its report is included only when its scenario label changes vs the last label included
(state/kospi.json kos5500_last_label; seeded "5500 이탈 첫날", sent 2026-10-26 15:40). The 09:00 repeat is
dropped from the morning bundle.

If the combined publish raises, every component state file is restored from the begin snapshot, so the next
run re-detects and re-sends (no condition is marked sent without a successful publish). A component config's
ends_kst (dashboard "알림 끄기") still mutes that component's part (checked in kospi_spool and again here).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ntfy_util  # noqa: E402
from kospi_spool import spool_path  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "kospi.json"
STATE_PATH = ROOT / "state" / "kospi.json"
# order = section order and title priority on ties
COMPONENTS = [("kosdown", "하방 레벨"), ("kosbrk", "대기 논리 변경 조건"), ("kosup", "상방 레벨"), ("kos5500", "5500 시나리오")]
FORBIDDEN = ("\u00b7", "\u2014")


def strip_prefix(title: str) -> str:
    return title.split("]", 1)[1].strip() if title.startswith("[") and "]" in title else title


def kos5500_label(title: str) -> str:
    return strip_prefix(title).split(" | ")[0].strip()


def combine(caught: list[dict], stamp: str) -> tuple[str, str, int, list[str]]:
    order = [n for n, _ in COMPONENTS]
    caught = sorted(caught, key=lambda c: (-c["priority"], order.index(c["name"])))
    top = caught[0]
    title = "[KOSPI] " + strip_prefix(top["title"]) + (f" 외 {len(caught) - 1}건" if len(caught) > 1 else "")
    label = dict(COMPONENTS)
    lines = [
        f"{stamp} | KOSPI 사다리 (15:40)",
        "이게 뭐 알림: 코스피 레벨 알림 묶음입니다(하방 5550/5450/5300/5200/5000, 상방 5750/6000, 시장 상태 6,700 재탈환/7,000 안착, 7/29 종가 5663.24, "
        "장중 저가 5262.77, 외국인 수급, 5500/5800 시나리오). 이번 실행에서 새로 걸린 조건만 한 번에 모았습니다. 매매 지시가 아닙니다.",
    ]
    for c in sorted(caught, key=lambda c: order.index(c["name"])):
        body = [ln for ln in c["message"].split("\n") if ln.strip()]
        if body and " | " in body[0] and body[0][:4].isdigit():
            body = body[1:]  # component stamp line; the ladder has one stamp
        body = [ln for ln in body if not ln.startswith(ntfy_util.ENDS_PREFIX)]
        lines.append(f"[{label[c['name']]}] {strip_prefix(c['title'])}")
        lines.extend(body)
    msg = "\n".join(lines)
    for ch in FORBIDDEN:
        title, msg = title.replace(ch, ", "), msg.replace(ch, ", ")
    return title, msg, max(c["priority"] for c in caught), list(top["tags"] or [])


def snap_dir() -> Path:
    return Path(os.environ.get("KOSPI_SNAP") or Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "kospi_snap")


def component_paths() -> list[Path]:
    return [ROOT / "state" / f"{n}.json" for n, _ in COMPONENTS]


def begin() -> int:
    spool_path().unlink(missing_ok=True)
    d = snap_dir()
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for p in component_paths():
        if p.exists():
            shutil.copy2(p, d / p.name)
        else:
            (d / (p.name + ".absent")).write_text("", encoding="utf-8")
    print(f"kospi ladder: spool cleared, {len(component_paths())} component states snapshotted to {d}")
    return 0


def restore() -> None:
    d = snap_dir()
    if not d.is_dir():
        print("kospi ladder: no snapshot to restore", file=sys.stderr)
        return
    for p in component_paths():
        if (d / p.name).exists():
            shutil.copy2(d / p.name, p)
        elif (d / (p.name + ".absent")).exists():
            p.unlink(missing_ok=True)


def read_spool() -> list[dict]:
    p = spool_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def send(dry: bool) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    names = [n for n, _ in COMPONENTS]
    st = load_state()
    keep = []
    for c in read_spool():
        if c.get("name") not in names:
            print(f"spool entry from unknown component {c.get('name')!r}; ignored", file=sys.stderr)
            continue
        skip = ntfy_util.ends_skip_reason(c["topic"], c["title"])
        if skip:
            print(skip)
            continue
        if c["name"] == "kos5500":
            lab = kos5500_label(c["title"])
            if lab == st.get("kos5500_last_label"):
                print(f"kos5500 label unchanged ({lab}); not in the ladder")
                continue
            st["kos5500_last_label"] = lab
            st["kos5500_last_day"] = now.strftime("%Y-%m-%d")
        keep.append(c)
    if not keep:
        print("kospi ladder: no new condition; silent")
    else:
        title, msg, pri, tags = combine(keep, stamp)
        print(title)
        print(msg)
        if dry:
            print(f"DRY: would send p{pri}; no state change")
            return 0
        try:
            ntfy_util.publish(ntfy_util.resolve_topic("port", cfg.get("ntfy_topic")), title, msg, pri, tags,
                              ntfy_util.resolve_server(cfg.get("ntfy_server")), config_name="kospi")
        except Exception:
            restore()
            print("combined publish failed; component states restored so the next run re-sends", file=sys.stderr)
            raise
        st["last_sent"] = {"at": stamp, "title": title, "parts": [c["name"] for c in keep]}
    if dry:
        return 0
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    spool_path().unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = (argv[0] if argv else "send").lower()
    if mode == "selftest":
        return selftest()
    if mode == "begin":
        return begin()
    if mode in ("send", "auto"):
        return send(False)
    if mode == "dry":
        return send(True)
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


def selftest() -> int:
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got} want={want}")
    stamp = "2026-11-02 15:40 KST"
    down = {"name": "kosdown", "title": "[KOSDOWN] 종가 5,440.00 | 5450 이탈 | 하방 확인", "priority": 4, "tags": ["warning"],
            "message": "2026-11-02 15:40 KST | KOSDOWN\n이게 뭐 알림: 하방.\n지금 상태: x"}
    k55 = {"name": "kos5500", "title": "[KOS5500] 5500 재이탈 지속", "priority": 4, "tags": ["chart_with_upwards_trend"],
           "message": "이게 뭐 알림: 시나리오\n지금 상태: y"}
    up = {"name": "kosup", "title": "[KOSUP] 종가 5,760.00 | 5750 돌파 | 상방 확인", "priority": 4, "tags": ["warning"],
          "message": "2026-11-02 15:40 KST | KOSUP\n지금 상태: z"}
    t, m, p, _ = combine([k55, down], stamp)
    check("title from kosdown first on tie", t, "[KOSPI] 종가 5,440.00 | 5450 이탈 | 하방 확인 외 1건")
    check("sections in ladder order", [ln for ln in m.split("\n") if ln.startswith("[")],
          ["[하방 레벨] 종가 5,440.00 | 5450 이탈 | 하방 확인", "[5500 시나리오] 5500 재이탈 지속"])
    check("component stamp line dropped", m.count("15:40 KST"), 1)
    check("priority max", p, 4)
    t, m, _, _ = combine([up], stamp)
    check("single part, no count", t, "[KOSPI] 종가 5,760.00 | 5750 돌파 | 상방 확인")
    check("kos5500 label", kos5500_label("[KOS5500] 5500 이탈 첫날 | 10/20 원샷"), "5500 이탈 첫날")
    check("no forbidden chars", any(ch in m for ch in FORBIDDEN), False)
    rev = {"name": "kosbrk", "title": "[KOSBRK] SK하이닉스 11월 판단 되돌림 신호 | 외국인 5일 연속 순매수, 종가 6,010.00",
           "priority": 4, "tags": ["chart_with_upwards_trend"],
           "message": "2026-11-06 15:40 KST | KOSBRK\n이게 뭐 알림: 되돌림\n지금 상태: 5거래일 연속 순매수"}
    t, m, _, _ = combine([rev, down], stamp)
    check("reversal section under kosbrk label", [ln for ln in m.split("\n") if ln.startswith("[")],
          ["[하방 레벨] 종가 5,440.00 | 5450 이탈 | 하방 확인",
           "[대기 논리 변경 조건] SK하이닉스 11월 판단 되돌림 신호 | 외국인 5일 연속 순매수, 종가 6,010.00"])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
