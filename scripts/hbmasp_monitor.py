#!/usr/bin/env python3
"""HBM blended-ASP baseline reminder. Standard library only. Not a trade signal.

Active window: configs/hbmasp.json active_from..expire_date and year_guard (an int or a list of years; 2026-11-03
extension to 2027-02-06 needs [2026, 2027], t46u). ntfy_util.publish also stops at ends_kst.
Modes: (none) run | --install-confirm | --selftest or selftest (offline fixtures: stubbed ntfy and notice feed, temp state).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "hbmasp.json"
STATE_PATH = ROOT / "state" / "hbmasp.json"
KST = timezone(timedelta(hours=9))
NOTICE_RE = re.compile(r"HBM|고대역폭|공급가|판매가|공급계약|단일판매", re.I)


def helper(name: str):
    try:
        return __import__(name)
    except ImportError:
        flat = ROOT.parent / "ntfy-source" / f"scripts__{name}.py"
        if not flat.exists():
            raise
        spec = importlib.util.spec_from_file_location(name, flat)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(fallback)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = copy.deepcopy(fallback)
    if isinstance(data, dict):
        out.update(data)
    return out


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_state() -> dict:
    return {
        "schema_version": 1,
        "last_baseline_week": None,
        "notice_versions": [],
        "ops_daily": {},
        "updated_at": None,
    }


def body(stamp: str, what: str, status: str, action: str, later: str, reduce: str, avoid: str, extra: list[str] | None = None) -> str:
    lines = [
        f"{stamp} | HBMASP",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {reduce}",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines + (extra or []))


def baseline_extra(cfg: dict) -> list[str]:
    b = cfg["baseline"]
    return [
        f"기준: {b['lock']}",
        b["offer_not_contract"],
        b["later_report"],
        b["trendforce"],
        b["kb"],
        b["sk"],
        b["goldman"],
    ] + [b[k] for k in ("dt_1026", "eugene") if b.get(k)] + [b["direction"]]


def year_ok(cfg: dict, year: int) -> bool:
    yg = cfg["year_guard"]
    return year in [int(y) for y in yg] if isinstance(yg, list) else year == int(yg)


def notice_key(code: str, item: dict) -> str:
    raw = "|".join((code, str(item.get("id") or ""), str(item.get("title") or ""), str(item.get("datetime") or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def publish(ntfy, server: str, title: str, message: str, priority: int = 3) -> None:
    print(f"channel=cwally-port title={title}")
    print(message)
    ntfy.publish("cwally-port", title, message, priority, ["chart_with_upwards_trend"], server)


def install_confirm(now: datetime) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    message = body(
        stamp,
        "HBM 블렌디드 ASP 기준 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 금액 민감도는 없음.",
        "확정 계약가와 회사 가이던스만 보고, 제시가를 영업이익에 넣지 마세요.",
        "같은 기관이 ASP 두 시나리오 영업이익을 공개하면 그때만 금액 비교.",
        "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
        "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
        baseline_extra(cfg),
    )
    publish(ntfy, server, "[HBMASP] 005930/000660 설치 확인 | 매매 신호 아님", message, 3)
    print("install-confirm sent; state not written")
    return 0


def run(now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    work = copy.deepcopy(state)
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    if not year_ok(cfg, now.year) or not (cfg["active_from"] <= today <= cfg["expire_date"]):
        print(f"{stamp} outside active window; silent")
        return 0
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    changed = False
    week = now.strftime("%G-W%V")
    if now.weekday() == 0 and state.get("last_baseline_week") != week:
        message = body(
            stamp,
            "HBM 가격 기준 주간 점검입니다. 매수 신호가 아닙니다.",
            "비교 기준은 블렌디드 ASP. 제시가 3배는 확정가가 아님. 하이닉스 탄력성이 크고 삼성은 연결에서 희석.",
            "확정 계약가와 회사 가이던스만 확인하세요. 제시가를 영업이익에 넣지 마세요.",
            "같은 기관의 ASP 두 시나리오가 나오기 전에는 금액 민감도를 만들지 않습니다.",
            "이 알림으로 줄이지 마세요. 최종가가 2배 안팎이면 3배 프레임을 폐기.",
            "연도가 다른 점추정을 차액으로 읽기. 4달러대 중후반을 확정 ASP로 읽기.",
            baseline_extra(cfg),
        )
        try:
            publish(ntfy, server, "[HBMASP] 005930/000660 기준 유지 | 제시가를 확정가로 보지 말 것", message, 3)
            work["last_baseline_week"] = week
            changed = True
        except Exception as exc:
            print(f"publish failed baseline: {exc}", file=sys.stderr)
            return 1
    naver = helper("naver_api")
    seen = set(state.get("notice_versions") or [])
    errors = []
    for code, name in cfg["notices"]["codes"].items():
        try:
            items = naver.notices(code, size=cfg["notices"]["size"])
            if not isinstance(items, list):
                raise ValueError("공시 목록 형식 오류")
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                if not NOTICE_RE.search(item["title"]):
                    continue
                key = notice_key(code, item)
                if key in seen:
                    continue
                message = body(
                    stamp,
                    f"{name} HBM 가격, 계약 제목 공시 원문 확인 요청입니다. 확정 판정이 아닙니다.",
                    f"제목: {item.get('title')}",
                    "원문에서 제시가인지 확정 계약가인지 직접 나누세요.",
                    "확정가 또는 회사 ASP 가이던스가 나오면 기준을 실적 민감도로 바꿉니다.",
                    "제목만으로 보유를 줄이지 마세요.",
                    "제목을 확정 계약, 3배 ASP, 영업이익 민감도로 읽기.",
                    [f"출처: {item.get('url') or '주소 없음'}"],
                )
                publish(ntfy, server, "[HBMASP] 005930/000660 새 공시 | 제시가와 확정가 구분", message, 4)
                work.setdefault("notice_versions", []).append(key)
                seen.add(key)
                work["notice_versions"] = work["notice_versions"][-500:]
                changed = True
        except Exception as exc:
            errors.append(f"{name} 공시 조회 실패: {exc}")
    if errors and state.get("ops_daily", {}).get(today) != "notice":
        ops = "[HBMASP] 005930/000660 공시 조회 실패 | 매매 신호 아님"
        message = body(
            stamp,
            "HBM 공시 목록 조회 실패 안내입니다. 매매 신호가 아닙니다.",
            " / ".join(errors),
            "다음 평일 09:00에 다시 봅니다. 오늘 기준은 유지하세요.",
            "원문이 없으면 제시가를 확정가로 올리지 않습니다.",
            "조회 실패로 보유를 줄이지 마세요.",
            "빈 결과를 계약 없음 또는 가격 확정으로 읽기.",
        )
        try:
            print(f"channel=cwally-ops title={ops}")
            ntfy.publish("cwally-ops", ops, message, 3, ["warning"], server)
            work.setdefault("ops_daily", {})[today] = "notice"
            changed = True
        except Exception as exc:
            print(f"ops publish failed: {exc}", file=sys.stderr)
    if changed:
        work["updated_at"] = stamp
        atomic_save(STATE_PATH, work)
        print("state updated")
    else:
        print(f"{stamp} no new baseline or notice")
    return 0


def selftest() -> int:
    """Offline: stubbed ntfy_util and naver_api (no network, no publish), temp state file; the real state is untouched."""
    import tempfile

    global STATE_PATH, helper
    real_state, real_helper = STATE_PATH, helper
    before = real_state.read_bytes() if real_state.exists() else None
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    sent: list = []
    feed = {"items": [], "fail": False}
    fails = []

    class Ntfy:
        @staticmethod
        def resolve_server(x):
            return "https://ntfy.invalid"

        @staticmethod
        def publish(topic, title, msg, pri=3, tags=None, server=None):
            sent.append((topic, title, msg, pri))

        @staticmethod
        def parse_ends_kst(raw):
            return real_helper("ntfy_util").parse_ends_kst(raw)

    class Naver:
        @staticmethod
        def notices(code, size=30):
            if feed["fail"]:
                raise RuntimeError("HTTP 503")
            return list(feed["items"]) if code == "005930" else []

    def check(label, got, want):
        good = got == want
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got!r} want={want!r}")
        if not good:
            fails.append(label)

    def at(d: str) -> datetime:
        return datetime.fromisoformat(d).replace(hour=9, minute=0, tzinfo=KST)

    def titles(d: str) -> list:
        sent.clear()
        run(at(d))
        return [t for _, t, _, _ in sent]

    with tempfile.TemporaryDirectory() as tmp:
        STATE_PATH = Path(tmp) / "hbmasp.json"
        helper = lambda name: {"ntfy_util": Ntfy, "naver_api": Naver}[name]  # noqa: E731
        try:
            base = "[HBMASP] 005930/000660 기준 유지 | 제시가를 확정가로 보지 말 것"
            check("config: window to 2027-02-06, years 2026 and 2027, ends 2027-02-06 09:00",
                  (cfg["expire_date"], cfg["year_guard"], cfg["ends_kst"]), ("2027-02-06", [2026, 2027], "2027-02-06 09:00"))
            check("config: ends_kst parses", str(Ntfy.parse_ends_kst(cfg["ends_kst"])[0])[:16], "2027-02-06 09:00")
            check("Mon 2026-11-16 (after the old 11/13 end): weekly baseline once", titles("2026-11-16"), [base])
            check("Tue 2026-11-17: nothing new, silent", titles("2026-11-17"), [])
            check("Mon 2027-01-04 (year 2027 allowed): baseline", titles("2027-01-04"), [base])
            check("Mon 2027-02-01 (last Monday in window): baseline", titles("2027-02-01"), [base])
            msg = sent[0][2]
            check("baseline lists the 10/26 quote and the Eugene estimate",
                  (cfg["baseline"]["dt_1026"] in msg, cfg["baseline"]["eugene"] in msg, "계약가 아님" in msg, "3.31달러/Gb" in msg), (True, True, True, True))
            check("baseline under 3,800 bytes (ntfy 4,096 with the end line)", len(msg.encode("utf-8")) < 3800, True)
            feed["items"] = [{"id": "1", "title": "삼성전자(주) 단일판매ㆍ공급계약체결", "datetime": "2027-02-05 08:00", "url": "u"},
                             {"id": "2", "title": "삼성전자(주) 자기주식처분결과보고서", "datetime": "2027-02-05 08:00", "url": "u"}]
            got = titles("2027-02-05")
            check("Fri 2027-02-05: matching filing once (unrelated silent)", got, ["[HBMASP] 005930/000660 새 공시 | 제시가와 확정가 구분"])
            check("filing message has no middle dot / em-dash", any(c in sent[0][2] for c in "\u00b7\u2014"), False)
            check("same filing again silent", titles("2027-02-05"), [])
            feed["items"] = [dict(feed["items"][0], id="3", datetime="2027-02-08 08:00")]
            check("Sun 2027-02-07 after expire_date 2027-02-06 (inclusive; no weekday run on 2/6, ntfy also stops at ends_kst): silent",
                  titles("2027-02-07"), [])
            check("Mon 2027-02-08: silent (no baseline, no filing)", titles("2027-02-08"), [])
            check("Mon 2028-01-03: silent (year outside guard)", titles("2028-01-03"), [])
            check("2026-10-06 before active_from: silent", titles("2026-10-06"), [])
            feed["fail"] = True
            sent.clear()
            run(at("2027-01-05"))
            check("feed down: one cwally-ops notice", [(tp, t) for tp, t, _, _ in sent], [("cwally-ops", "[HBMASP] 005930/000660 공시 조회 실패 | 매매 신호 아님")])
            check("feed down again same day: silent", titles("2027-01-05"), [])
            check("year_ok int and list", (year_ok({"year_guard": 2026}, 2027), year_ok({"year_guard": [2026, 2027]}, 2027)), (False, True))
        finally:
            STATE_PATH, helper = real_state, real_helper
    check("real state file untouched", (real_state.read_bytes() if real_state.exists() else None) == before, True)
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 0 if not fails else 1


def main() -> int:
    if sys.argv[1:] == ["selftest"]:  # same spelling as the other monitors
        return selftest()
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-confirm", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.selftest:
        return selftest()
    if args.install_confirm:
        return install_confirm(now)
    return run(now)


if __name__ == "__main__":
    raise SystemExit(main())
