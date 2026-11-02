#!/usr/bin/env python3
"""Korea Zinc (010130) Q3 2026 one-shot: compare actual vs estimates, scenarios.

After results, score OP vs Meritz/Hyundai estimates, note metal sensitivity,
US project financing disclosure. Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic
from naver_api import get_json, notices  # legacy finance.naver.com pages are gone (410); see naver_api.py

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "kzq3.json"
STATE = ROOT / "state" / "kzq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}


def fetch(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def default_state():
    return {"final_notified": False, "empty_notified": False, "fail_notified": False}


def load_state():
    st = default_state()
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


TITLE_KEY = "연결재무제표기준영업(잠정)실적"  # KRX 공정공시, consolidated only (estimates are consolidated)
UNIT_TO_EOK = {"백만원": 100.0, "억원": 1.0, "천원": 100000.0, "조원": 0.0001}


def parse_notice(body, period_end):
    """Consolidated 영업이익 (억원) from a KRX 영업(잠정)실적 notice body.

    period_end: "YYYY-MM-DD" end of the 당기실적 period (Q3 = YYYY-09-30). Returns
    {"op": 억원 for the quarter, "ytd": 억원 cumulative, "period": "<start> ~ <end>"} or None.
    """
    m = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", body or "")
    if not m or m.group(2) != period_end:
        return None
    um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", body)
    if not um:
        return None
    div = UNIT_TO_EOK[um.group(1)]
    row = re.search(r"영업이익\s*당해실적\s*(-?[\d,]+(?:\.\d+)?)[^가-힣]{0,80}?누계실적\s*(-?[\d,]+(?:\.\d+)?)", body)
    if not row:
        return None
    op = float(row.group(1).replace(",", "")) / div
    ytd = float(row.group(2).replace(",", "")) / div
    return {"op": round(op), "ytd": round(ytd), "period": f"{m.group(1)} ~ {m.group(2)}"}


def find_result(period_end, since):
    """(result, info, errors). Source 1: KRX/DART 공정공시 via stock.naver.com notice API.
    Source 2 (only if source 1 is empty): m.stock.naver.com finance/quarter, accepted only when
    the quarter column is NOT a consensus estimate."""
    errors = []
    try:
        for r in notices("010130", 60):
            if TITLE_KEY not in r.get("title", "") or "정정" in r.get("title", ""):
                continue
            if str(r.get("datetime", ""))[:10] < since:
                continue
            got = parse_notice(r.get("text", ""), period_end)
            if got:
                got.update(source="notice", datetime=r.get("datetime"), title=r.get("title"))
                return got, "notice", "; ".join(errors)
    except Exception as exc:
        errors.append(f"notice: {exc}")
    try:
        key = period_end[:4] + period_end[5:7]
        d = get_json("https://m.stock.naver.com/api/stock/010130/finance/quarter")["financeInfo"]
        cons = {t["key"]: t.get("isConsensus") == "Y" for t in d["trTitleList"]}
        if key in cons and not cons[key]:
            for row in d["rowList"]:
                if row["title"] == "영업이익":
                    val = (row["columns"].get(key) or {}).get("value")
                    if val not in (None, ""):
                        return {"op": round(float(str(val).replace(",", ""))), "ytd": None, "period": key,
                                "source": "naver_quarter", "datetime": None, "title": "naver finance/quarter"}, "naver_quarter", "; ".join(errors)
    except Exception as exc:
        errors.append(f"quarter: {exc}")
    return None, "none", "; ".join(errors)


def collect(cfg):
    year = int(cfg.get("event_year", 2026))
    since = cfg["window_start_kst"][:10]
    return find_result(f"{year}-09-30", since)


def selftest(cfg):
    """Dry-run only: prove the parser on the already published Q2 notice (and Q1)."""
    year = int(cfg.get("event_year", 2026))
    out = []
    for label, end in (("Q2", f"{year}-06-30"), ("Q1", f"{year}-03-31")):
        got, _, err = find_result(end, f"{year}-01-01")
        out.append((label, end, got, err))
    return out


def score(op, b):
    if op is None:
        return "확인불가", "실적 원문에서 영업이익 추출 실패"
    meritz, hyundai = b["meritz_op_eok"], b["hyundai_op_eok"]
    if op >= 4300:
        return "상향", f"OP {op}억원 >= 4300, 추정 상회"
    elif op >= hyundai:
        return "기준", f"OP {op}억원, 현대차 {hyundai} 부근"
    elif op >= meritz:
        return "중립", f"OP {op}억원, 메리츠 {meritz} 상회 현대차 하회"
    else:
        return "하향", f"OP {op}억원 < 메리츠 {meritz}"


def emit(cfg, title, lines, pri):
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        "\n".join(lines),
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    if mode == "selftest":
        return offline_selftest(cfg)
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode not in {"force", "dry"}:
        print("already final; skip")
        save_state(st, now)
        return 0
    res, src, err = collect(cfg)
    op = res["op"] if res else None
    b = cfg["baseline"]
    action, why = score(op, b)
    if mode == "dry":
        # dry: print the parse/score result only. No ntfy send, no state write.
        print("DRY RUN | no ntfy, no state write")
        print(f"collect_error={err or '-'} source={src}")
        if res:
            print(f"found: {res.get('title')} | posted {res.get('datetime')} (UTC) | period {res.get('period')} | ytd_eok={res.get('ytd')}")
        print(f"parsed_op_eok={op}")
        for label, end, got, e in selftest(cfg):
            if got:
                extra = ""
                if label == "Q2" and got.get("ytd") is not None:
                    extra = f" | ytd {got['ytd']} vs baseline h1_op_eok {b['h1_op_eok']} -> {'MATCH' if got['ytd'] == b['h1_op_eok'] else 'MISMATCH'}"
                print(f"selftest {label} ({end}): op_eok={got['op']} via {got['source']}{extra}")
            else:
                print(f"selftest {label} ({end}): NOT FOUND {e}")
        print(f"decision={action} | {why}")
        print(f"state(read-only)={json.dumps(st, ensure_ascii=False)}")
        if op is None:
            print("would send: [KZ] 010130 원문 없음 | 보유 유지" + (" (skipped: empty_notified already true)" if st.get("empty_notified") else ""))
        else:
            print(f"would send: [KZ] 010130 {action} | 3분기 vs 추정")
        return 0
    if op is None and mode != "force":
        if not st.get("empty_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | 고려아연 | 3분기",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 실적 원문 미확인. 추정 비교 보류.",
                "지금 할 일: 보유 유지. DART/공시 직접 확인.",
                "나중에: 11/6, 11/7 09:10 KST 재확인. 만료 2026-11-07.",
                "익절/줄일 때: 미발표로 비중 조정 금지.",
                "하지 말 것: 추정을 실적으로 읽기. 신용, 미수, 추격매수. 투자 권유 아님.",
            ]
            emit(cfg, "[KZ] 010130 원문 없음 | 보유 유지", lines, 3)
            st["empty_notified"] = True
            save_state(st, now)
        return 0
    # Scenario notes from prior analysis
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | 고려아연 | 3분기 비교",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 상태: 결론 {action}. {why}. 메리츠 {b['meritz_op_eok']}억, 현대차 {b['hyundai_op_eok']}억 대비. 아연 {b['zinc_lme']}달러, 은 {b['silver_spot']}달러 기준. 주가 앵커 {b['price_anchor']//10000}만원(10/8). 목표 {b['target_hyundai']//10000}만원.",
        "지금 할 일: " + ("매수 우위 검토" if action == "상향" else "중립 유지, 조정 시 분할" if action in {"기준", "중립"} else "축소 검토") + ". 미국 47억달러 조달 공개 여부 별도 확인.",
        "나중에: 은 온스당 1달러 → 연간 OP ~100~120억 변동. 아연 3,500달러 하회 시 하향. 4분기 5,000억대 회복 시 상향.",
        "익절/줄일 때: OP 3,800억 미만 또는 금속가격 급락 시 비중 축소. 인허가, 조달 지연 시 할인 확대.",
        "하지 말 것: TTM PER 편차로 과평가 단정. 경영권 분쟁 무시. 신용, 미수, 추격매수. 투자 권유 아님.",
        "출처: " + cfg["sources"]["news"],
    ]
    title = f"[KZ] 010130 {action} | 3분기 vs 추정"
    emit(cfg, title, lines, 4 if action != "중립" else 3)
    st["final_notified"] = True
    st["score"] = action
    save_state(st, now)
    return 0


# ---------- offline selftest (no network, no ntfy, no state file) ----------
# Added 2026-11-02 (user approval pt33u). `kzq3_monitor.py selftest` replaces notices()/get_json() with the fixtures
# below, blocks urlopen and sockets, captures publish() and save_state(), fixes the clock, and seeds state from
# default_state(). It never reads or writes state/kzq3.json. The live Q2/Q1 parser check stays in `dry` (selftest(cfg)).

def offline_selftest(cfg):
    import copy
    from unittest import mock

    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        good = got == want
        n["ok" if good else "fail"] += 1
        print(("ok  " if good else "FAIL"), label, "" if good else f"got={got!r} want={want!r}")

    g = globals()
    sent, saved, net = [], [], []
    fx = {"notices": [], "quarter": None}
    state_before = (lambda s: (s.st_size, s.st_mtime_ns))(STATE.stat()) if STATE.exists() else None

    def body(op, ytd, start="2026-07-01", end="2026-09-30", unit="억원"):
        return (f"1. 연결실적내용 단위 : {unit}, % 구분 당기실적 {start} ~ {end} 매출액 당해실적 40,000 39,000 2.6 "
                f"영업이익 당해실적 {op} 3,500 10.0 3,300 12.0 누계실적 {ytd} 12,000 5.0 법인세비용차감전계속사업이익 당해실적 3,000")

    def note(title, dt, text):
        return {"id": title + dt, "title": title, "datetime": dt, "text": text, "url": "fixture"}

    def quarter(cons, op="4,100"):
        return {"financeInfo": {"trTitleList": [{"key": "202606", "isConsensus": "N"}, {"key": "202609", "isConsensus": cons}],
                                "rowList": [{"title": "영업이익", "columns": {"202606": {"value": "3,633"}, "202609": {"value": op}}}]}}

    def fake_notices(code, size=30):
        if isinstance(fx["notices"], Exception):
            raise fx["notices"]
        return copy.deepcopy(fx["notices"])

    def fake_get_json(url, timeout=20):
        if fx["quarter"] is None:
            raise OSError("selftest: quarter fixture missing")
        return copy.deepcopy(fx["quarter"])

    def blocked(*a, **_k):
        net.append(str(a[:1]))
        raise OSError("selftest: network disabled")

    def fake_publish(topic, title, message, priority=3, tags=None, server=None, **_k):
        sent.append((title, priority, message))

    def fake_save(st, now):
        st["updated_at"] = now.isoformat()
        saved.append(copy.deepcopy(st))

    clock = {"now": None}

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def run(mode, when, st):
        clock["now"] = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        g["load_state"] = lambda: st
        old_argv = sys.argv
        sys.argv = ["kzq3_monitor.py", mode]
        try:
            return main()
        finally:
            sys.argv = old_argv

    q3_ok = note("고려아연 연결재무제표기준영업(잠정)실적(공정공시)", "2026-11-05T07:10:00", body("4,400", "13,332"))
    keep = {k: g[k] for k in ("notices", "get_json", "publish", "save_state", "load_state", "datetime")}
    patches = [mock.patch("urllib.request.urlopen", blocked), mock.patch("socket.socket.connect", blocked),
               mock.patch("socket.create_connection", blocked)]
    for p in patches:
        p.start()
    g.update(notices=fake_notices, get_json=fake_get_json, publish=fake_publish, save_state=fake_save, datetime=FixedDT)
    g["urlopen"] = blocked
    try:
        # --- parser
        check("parse Q3 notice 억원: op, ytd, period", parse_notice(body("4,400", "13,332"), "2026-09-30"),
              {"op": 4400, "ytd": 13332, "period": "2026-07-01 ~ 2026-09-30"})
        check("parse Q3 notice 백만원 -> 억원", (parse_notice(body("440,000", "1,333,200", unit="백만원"), "2026-09-30") or {}).get("op"), 4400)
        check("Q2 period is not read as Q3", parse_notice(body("3,633", "13,332", "2026-04-01", "2026-06-30"), "2026-09-30"), None)
        check("no unit -> None", parse_notice(body("4,400", "13,332").replace("단위 : 억원,", ""), "2026-09-30"), None)
        check("no 영업이익 row -> None", parse_notice(body("4,400", "13,332").replace("영업이익", "기타"), "2026-09-30"), None)

        # --- find_result: notice first, 정정 and pre-window skipped, quarter fallback only when not consensus
        fx.update(notices=[q3_ok], quarter=quarter("N", "9,999"))
        got, src, _ = find_result("2026-09-30", "2026-11-04")
        check("notice found -> op 4400 via notice (quarter ignored)", ((got or {}).get("op"), src), (4400, "notice"))
        fx.update(notices=[note("[정정] 고려아연 연결재무제표기준영업(잠정)실적(공정공시)", "2026-11-05T07:10:00", body("1,000", "9,000"))], quarter=quarter("Y"))
        check("정정 notice skipped, consensus quarter -> None", find_result("2026-09-30", "2026-11-04")[:2], (None, "none"))
        fx.update(notices=[note("고려아연 연결재무제표기준영업(잠정)실적(공정공시)", "2026-11-03T07:10:00", body("4,400", "13,332"))])
        check("notice before window start skipped", find_result("2026-09-30", "2026-11-04")[1], "none")
        fx.update(notices=[], quarter=quarter("N", "4,100"))
        got, src, _ = find_result("2026-09-30", "2026-11-04")
        check("no notice + actual quarter -> fallback op 4100", ((got or {}).get("op"), src), (4100, "naver_quarter"))
        fx.update(notices=OSError("HTTP 500"), quarter=quarter("N", "4,100"))
        got, src, err = find_result("2026-09-30", "2026-11-04")
        check("notice fetch error -> fallback still used, error kept", (src, err.startswith("notice: HTTP 500")), ("naver_quarter", True))
        fx.update(notices=OSError("HTTP 500"), quarter=None)
        got, src, err = find_result("2026-09-30", "2026-11-04")
        check("both sources fail -> None with both errors", (got, "notice:" in err and "quarter:" in err), (None, True))

        # --- score thresholds
        b = cfg["baseline"]
        check("score 4300 상향, 4246 기준, 3888 중립, 3887 하향, None 확인불가",
              [score(x, b)[0] for x in (4300, b["hyundai_op_eok"], b["meritz_op_eok"], b["meritz_op_eok"] - 1, None)],
              ["상향", "기준", "중립", "하향", "확인불가"])

        # --- main: fire once
        fx.update(notices=[q3_ok], quarter=quarter("Y"))
        st = default_state()
        sent.clear(); saved.clear()
        run("auto", "2026-11-05 16:10", st)
        check("fire: OP 4400 -> one send", len(sent), 1)
        check("fire title has [KZ] 010130 prefix", sent[0][0] if sent else None, "[KZ] 010130 상향 | 3분기 vs 추정")
        check("fire p4, body has the six items", (sent[0][1] if sent else None, all(k in (sent[0][2] if sent else "") for k in ("이게 뭐 알림", "지금 상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"))), (4, True))
        check("fire marks final_notified + score (captured save)", (st["final_notified"], st.get("score"), len(saved)), (True, "상향", 1))
        sent.clear()
        run("auto", "2026-11-06 09:10", st)
        check("once-only: next run after final -> silent", sent, [])
        fx.update(notices=[note(q3_ok["title"], q3_ok["datetime"], body("3,900", "13,000"))])
        st = default_state()
        run("auto", "2026-11-05 16:10", st)
        check("중립 (3900) -> p3", [(s[0], s[1]) for s in sent], [("[KZ] 010130 중립 | 3분기 vs 추정", 3)])
        sent.clear()

        # --- missing data -> hold once, then the result still fires
        fx.update(notices=[], quarter=quarter("Y"))
        st = default_state()
        run("auto", "2026-11-05 16:10", st)
        check("missing: no notice, consensus only -> 원문 없음 | 보유 유지 p3", [(s[0], s[1]) for s in sent], [("[KZ] 010130 원문 없음 | 보유 유지", 3)])
        check("missing: empty_notified set, final not set", (st["empty_notified"], st["final_notified"]), (True, False))
        sent.clear()
        run("auto", "2026-11-06 09:10", st)
        check("missing again -> no second 원문 없음", sent, [])
        fx.update(notices=[q3_ok])
        run("auto", "2026-11-07 09:10", st)
        check("result appears later -> final send", [s[0] for s in sent], ["[KZ] 010130 상향 | 3분기 vs 추정"])
        sent.clear()
        fx.update(notices=OSError("HTTP 503"), quarter=None)
        st = default_state()
        run("auto", "2026-11-05 16:10", st)
        check("both fetches fail -> held as 원문 없음 (no score)", [s[0] for s in sent], ["[KZ] 010130 원문 없음 | 보유 유지"])
        sent.clear()

        # --- window / expiry / year
        fx.update(notices=[q3_ok], quarter=quarter("Y"))
        for when, label in (("2026-11-04 08:59", "before window start 11/4 09:00"), ("2026-11-08 00:00", "after expire 11/7 23:59:59"),
                            ("2027-11-05 16:10", "other year")):
            st = default_state()
            run("auto", when, st)
            check(f"outside window ({label}) -> silent, not final", (sent, st["final_notified"]), ([], False))
        st = default_state()
        run("auto", "2026-11-07 23:59", st)
        check("last minute of window still fires", len(sent), 1)
        sent.clear()

        # --- 11/7 23:00 KST: script window and publish() end gate both still send (ends_kst 2026-11-07 23:59 + 59m grace)
        import ntfy_util
        st = default_state()
        run("auto", "2026-11-07 23:00", st)
        check("11/7 23:00 KST result -> sends", [s[0] for s in sent], ["[KZ] 010130 상향 | 3분기 vs 추정"])
        late = sent[0][0] if sent else "[KZ] 010130 상향 | 3분기 vs 추정"
        gate = lambda when: ntfy_util.ends_skip_reason("cwally-port", late, config_name="kzq3", config=cfg,
                                                       now=datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=KST))
        check("publish end gate: 11/7 23:00 not blocked", gate("2026-11-07 23:00"), None)
        check("publish end gate: 11/8 00:58 (23:59 + 59m) not blocked", gate("2026-11-08 00:58"), None)
        check("publish end gate: 11/8 01:00 blocked", gate("2026-11-08 01:00") is not None, True)
        sent.clear()

        # --- user-facing text: every message kind, with the 꺼지는 때 line, has no middle dot, no em-dash, no 매리츠
        texts = [cfg["explain"], ntfy_util.ends_line("kzq3") or ""]
        for op in (4400, 4250, 3900, 3000):
            fx.update(notices=[note(q3_ok["title"], q3_ok["datetime"], body(f"{op:,}", "13,000"))], quarter=quarter("Y"))
            run("auto", "2026-11-05 16:10", default_state())
        fx.update(notices=[], quarter=quarter("Y"))
        run("auto", "2026-11-05 16:10", default_state())
        check("text check covers 상향/기준/중립/하향/원문 없음", [s[0].split(" ")[2] for s in sent], ["상향", "기준", "중립", "하향", "원문"])
        texts += [s[0] + "\n" + ntfy_util.append_ends(s[2], "kzq3") for s in sent]
        bad = sorted({ch for t in texts for ch in ("\u00b7", "\u2014", "매리츠") if ch in t})
        check("no middle dot, em-dash or 매리츠 in kzq3 text", bad, [])
        sent.clear()

        # --- dry: no send, no save
        saved.clear()
        st = default_state()
        run("dry", "2026-11-05 16:10", st)
        check("dry: no publish, no save", (sent, saved, st["final_notified"]), ([], [], False))

        check("no network call attempted", net, [])
    finally:
        g.update(keep)
        for p in reversed(patches):
            p.stop()
    check("state/kzq3.json untouched (size, mtime)", ((lambda s: (s.st_size, s.st_mtime_ns))(STATE.stat()) if STATE.exists() else None) == state_before, True)
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
