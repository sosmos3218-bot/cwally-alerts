#!/usr/bin/env python3
"""[IREN-EARN] IREN Q1 FY27 results (approved in chat 2026-10-30). Watchlist only (user does not hold IREN).

Verified 2026-10-30: results Thursday 2026-11-05 after the US close, call 17:00 ET (= 2026-11-06 07:00 KST),
GlobeNewswire release 2026-10-22. CIK 0001878848 (domestic filer: 8-K Item 2.02 + 10-Q).
  - 2026-11-05 19:00 KST (k203780 slot): reminder p3 with the 7-item checklist. Once. Not sent after 11/06 06:00 KST.
  - 2026-11-06 .. 2026-11-16 (morning 09:00 and k203780 19:00 slots): poll EDGAR submissions. A new 8-K with Item 2.02
    or a new 10-Q filed on/after 2026-11-05 -> one p3 message per filing (dedupe by accession), with links
    (8-K: Exhibit 99.1 when found). Numbers are NOT auto-parsed; the user compares them with the checklist.
  - Neither the 8-K 2.02 nor the 10-Q by 2026-11-10 09:00 KST (2 US business days after 11/5) -> one p3 notice.
Modes: morning | evening (slot label) + auto | dry (print only, no ntfy, never writes state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "irenearn.json"
STATE = ROOT / "state" / "irenearn.json"
NAME = "[IREN-EARN]"
BANNED = ("\u00b7", "\u2014")
BUY_WORDS = ("분할매수", "매수 검토", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "나눠서")


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(kind: str, title: str, body: str, prio: int, tags: list[str], dry: bool) -> None:
    if dry:
        print(f"DRY RUN would send ({kind}, p{prio}): {title}\n{body}")
        return
    publish(resolve_topic(kind), title, body, prio, tags=tags, config_name="irenearn")


def kst(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


# ---------- EDGAR ----------

def edgar_get(cfg: dict, url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": cfg["user_agent"], "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def recent_filings(sub: dict) -> list[dict]:
    r = sub["filings"]["recent"]
    return [{"form": r["form"][i], "date": r["filingDate"][i], "acc": r["accessionNumber"][i],
             "items": (r.get("items") or [""] * len(r["form"]))[i] or "", "doc": r["primaryDocument"][i]}
            for i in range(len(r["form"]))]


def result_kind(f: dict) -> str | None:
    items = [x.strip() for x in f["items"].split(",")]
    if f["form"] in ("8-K", "8-K/A") and "2.02" in items:
        return "8k"
    if f["form"] in ("10-Q", "10-Q/A"):
        return "10q"
    return None


def pick_results(cfg: dict, filings: list[dict], seen: set[str]) -> list[dict]:
    return [f for f in sorted(filings, key=lambda x: (x["date"], x["acc"]))
            if result_kind(f) and f["date"] >= cfg["filing_not_before"] and f["acc"] not in seen]


def urls(cfg: dict, f: dict) -> tuple[str, str, str]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cfg['cik'])}/{f['acc'].replace('-', '')}"
    return base, f"{base}/{f['acc']}-index.htm", f"{base}/{f['doc']}"


def exhibit_url(cfg: dict, f: dict, get) -> str | None:
    base = urls(cfg, f)[0]
    try:
        names = [i["name"] for i in json.loads(get(f"{base}/index.json"))["directory"]["item"]]
    except Exception:  # noqa: BLE001
        return None
    for n in names:
        if re.search(r"(ex|exhibit)[-_]?99[-_.]?1?", n, re.I) and n.lower().endswith((".htm", ".html", ".txt")):
            return f"{base}/{n}"
    return None


# ---------- messages ----------

def checklist(cfg: dict) -> str:
    return " ".join(f"{i}) {x}." for i, x in enumerate(cfg["checklist"], 1))


def head() -> str:
    return f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | IREN | 1분기(FY27, 9월 분기) 실적 | 보유 없음, 관심 종목"


def reminder_message(cfg: dict) -> tuple[str, str, int]:
    lines = [
        head(),
        "이게 뭐 알림: IREN 실적 전 체크리스트(1회). " + cfg["explain"],
        "지금 결과/상태: 실적은 미 동부 11/5(목) 장 마감 뒤 발표, 콘퍼런스콜 11/5 17:00 ET(한국 11/6 금 07:00). 2026-10-22 회사 보도자료로 확인. 실적 전 시간외 변동이 큽니다(베타 약 4.3).",
        "지금 할 일: 실적 전에는 관망. 발표 뒤 볼 7가지를 미리 정리: " + checklist(cfg),
        "나중에: 11/6 09:00 부터 11/16 까지 09:00, 19:00 에 SEC 실적 8-K(Item 2.02)와 10-Q 를 확인해 올라오면 링크를 보냅니다. 11/10 09:00 까지 없으면 안내 1번.",
        "익절/줄일 때: 보유 없음이라 해당 없음.",
        "하지 말 것: 실적 전 미리 진입, 발표 직후 시간외 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return f"{NAME} 내일 아침 실적 발표 | 체크리스트 7개 준비, 관망", "\n".join(lines), 3


def result_message(cfg: dict, f: dict, links: dict) -> tuple[str, str, int]:
    k = result_kind(f)
    what = "실적 8-K(Item 2.02, 보도자료)" if k == "8k" else "10-Q 분기보고서"
    link = f"공시 목록 {links['index']}" + (f", 보도자료(Exhibit 99.1) {links['ex']}" if links.get("ex") else f", 본문 {links['main']}")
    extra = ("10-Q 에서는 유동성(현금, 전환사채 만기와 전환가), 약정 CapEx, ATM 사용액, 고객 선수금 항목을 보세요."
             if k == "10q" else "보도자료 숫자를 아래 7가지와 직접 비교하세요. 콘퍼런스콜(11/6 07:00 KST) 질의응답의 Horizon 일정 설명도 확인.")
    lines = [
        head(),
        f"이게 뭐 알림: IREN {what}가 SEC 에 올라와 1번 오는 알림. 숫자는 자동 추출하지 않습니다. 보유 없음 기준, 투자 권유 아님.",
        f"지금 결과/상태: {f['date']} {f['form']} 제출(접수번호 {f['acc']}). {link}.",
        f"지금 할 일: {extra} " + checklist(cfg),
        "나중에: 판단은 종가 구간 알림([IREN-PX] 23.72, 20, 35달러)과 함께. Horizon 지연, 가이던스 하향, 대규모 희석이 겹치면 관망 유지.",
        "익절/줄일 때: 보유 없음이라 해당 없음.",
        "하지 말 것: 헤드라인 숫자만 보고 시간외나 장 시작 직후 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return f"{NAME} {'실적 8-K 공시' if k == '8k' else '10-Q 제출'} | 체크리스트 대조", "\n".join(lines), 3


def missing_message(cfg: dict) -> tuple[str, str, int]:
    lines = [
        head(),
        "이게 뭐 알림: 예정일(미 동부 11/5) 뒤 미국 영업일 2일이 지나도 SEC 에 실적 8-K(Item 2.02)와 10-Q 가 안 보여 1번 오는 안내.",
        f"지금 결과/상태: EDGAR(CIK {cfg['cik']})에 2026-11-05 이후 실적 8-K, 10-Q 없음. 발표 연기이거나 EDGAR 반영 지연일 수 있음. 발표 연기라면 그 자체가 경고 신호.",
        "지금 할 일: IREN IR 페이지(iren.com/investors)와 뉴스에서 발표 여부와 일정을 직접 확인. 그 전까지 관망 유지, 추가매수 보류.",
        "나중에: 11/16 까지 09:00, 19:00 에 계속 확인하고 공시가 보이면 링크를 1번씩 보냅니다.",
        "익절/줄일 때: 보유 없음이라 해당 없음.",
        "하지 말 것: 공시 확인 전 추측으로 주문, 신용/미수. 투자 권유 아님.",
    ]
    return f"{NAME} 실적 공시 아직 없음 | 일정 확인, 추가매수 보류", "\n".join(lines), 3


# ---------- run ----------

def run(cfg: dict, dry: bool, now: datetime, fetch_sub=None, get=None) -> int:
    st = load_state()
    if now > kst(cfg["watch_until_kst"]):
        print("after watch window; silent")
        return 0
    changed = False
    if kst(cfg["reminder_kst"]) <= now < kst(cfg["reminder_not_after_kst"]) and not st.get("reminder_sent"):
        t, b, p = reminder_message(cfg)
        send("port", t, b, p, ["calendar"], dry)
        st["reminder_sent"] = now.strftime("%Y-%m-%d %H:%M KST")
        changed = True
    if now >= kst(cfg["watch_from_kst"]):
        get = get or (lambda u: edgar_get(cfg, u))
        fetch_sub = fetch_sub or (lambda: json.loads(edgar_get(cfg, cfg["submissions"])))
        try:
            filings = recent_filings(fetch_sub())
        except Exception as exc:  # noqa: BLE001
            print(f"EDGAR fetch failed: {exc}")
            day = now.strftime("%Y-%m-%d")
            if st.get("fail_day") != day:
                send("ops", f"{NAME} EDGAR 조회 실패 | 매매 신호 아님", f"SEC EDGAR submissions 조회 실패: {type(exc).__name__} {exc}"[:300] + ". 다음 슬롯에서 다시 봅니다.", 3, ["warning"], dry)
                st["fail_day"] = day
                changed = True
            filings = None
        if filings is not None:
            seen = set(st.get("seen_accessions") or [])
            new = pick_results(cfg, filings, seen)
            for f in new:
                base, idx, main = urls(cfg, f)
                links = {"index": idx, "main": main, "ex": exhibit_url(cfg, f, get) if result_kind(f) == "8k" else None}
                t, b, p = result_message(cfg, f, links)
                send("port", t, b, p, ["bar_chart"], dry)
                st.setdefault("found", {})[result_kind(f)] = {"acc": f["acc"], "date": f["date"], "at": now.strftime("%Y-%m-%d %H:%M KST")}
                seen.add(f["acc"])
                changed = True
            st["seen_accessions"] = sorted(seen)
            if not new:
                print("no new results 8-K 2.02 / 10-Q")
            if not st.get("found") and now >= kst(cfg["missing_after_kst"]) and not st.get("missing_sent"):
                t, b, p = missing_message(cfg)
                send("port", t, b, p, ["hourglass"], dry)
                st["missing_sent"] = now.strftime("%Y-%m-%d %H:%M KST")
                changed = True
    if changed:
        save_state(st, dry)
    else:
        print("nothing due; silent")
    return 0


def main(argv: list[str]) -> int:
    slot = (argv[1] if len(argv) > 1 else "morning").lower()
    if slot == "selftest":
        return selftest()
    mode = (argv[2] if len(argv) > 2 else "auto").lower()
    dry = mode != "auto"
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} irenearn slot={slot} mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    return run(load_cfg(), dry, now)


# ---------- selftest ----------

def selftest() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global STATE
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    old_rows = [("8-K", "2026-08-27", "0001878848-26-000051", "2.02,9.01", "iren-20260827.htm"),
                ("10-K", "2026-08-27", "0001878848-26-000050", "", "iren-20260630.htm")]
    k8 = ("8-K", "2026-11-05", "0001878848-26-000090", "2.02,9.01", "iren-20261105.htm")
    q10 = ("10-Q", "2026-11-06", "0001878848-26-000091", "", "iren-20260930.htm")
    other = ("8-K", "2026-11-06", "0001140361-26-099999", "8.01,9.01", "x.htm")

    def sub(extra=()):
        rows = sorted(old_rows + list(extra), key=lambda r: r[1], reverse=True)
        return {"filings": {"recent": {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
                                       "accessionNumber": [r[2] for r in rows], "items": [r[3] for r in rows],
                                       "primaryDocument": [r[4] for r in rows]}}}
    pages = {"index.json": json.dumps({"directory": {"item": [{"name": "iren-20261105.htm"}, {"name": "ex991-q1fy27.htm"}]}})}
    getter = lambda u: pages[u.rsplit("/", 1)[1]]  # noqa: E731

    check("old Aug 2.02 ignored", pick_results(cfg, recent_filings(sub()), set()), [])
    check("8.01 ignored", pick_results(cfg, recent_filings(sub([other])), set()), [])
    got = pick_results(cfg, recent_filings(sub([k8, q10, other])), set())
    check("8-K 2.02 + 10-Q picked in order", [f["acc"] for f in got], [k8[2], q10[2]])
    check("dedupe", pick_results(cfg, recent_filings(sub([k8, q10])), {k8[2], q10[2]}), [])
    f8 = got[0]
    check("exhibit 99.1", exhibit_url(cfg, f8, getter), "https://www.sec.gov/Archives/edgar/data/1878848/000187884826000090/ex991-q1fy27.htm")
    msgs = {"rem": reminder_message(cfg),
            "8k": result_message(cfg, f8, {"index": "I", "main": "M", "ex": "E"}),
            "10q": result_message(cfg, got[1], {"index": "I", "main": "M", "ex": None}),
            "miss": missing_message(cfg)}
    for n, (t, b, p) in msgs.items():
        print(f"  p{p} {t}")
        check(f"{n} p3", p, 3)
        check(f"{n} prefix", t.startswith(NAME + " "), True)
        check(f"{n} no dot/dash", any(c in t + b for c in BANNED), False)
        check(f"{n} no buy wording", [w for w in BUY_WORDS if w in t + b], [])
        check(f"{n} six parts", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    for kw in ("Horizon 2~4", "ARR", "가이던스", "전환사채", "CapEx", "ATM", "프리페이먼트"):
        check(f"reminder checklist {kw}", kw in msgs["rem"][1], True)

    def at(s):
        return kst(s)
    old = STATE
    sent: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE = Path(td) / "irenearn.json"

            def go(now_s, subs, dry=False):
                buf = io.StringIO()
                with mock.patch(__name__ + ".publish", side_effect=lambda topic, t, *a, **k: sent.append(t)), redirect_stdout(buf):
                    run(cfg, dry, at(now_s), fetch_sub=lambda: subs, get=getter)
                return buf.getvalue()
            go("2026-11-05 09:00", sub())
            check("11/5 09:00 nothing", sent, [])
            go("2026-11-05 19:00", sub(), dry=True)
            check("dry: nothing sent, no state", (sent, STATE.exists()), ([], False))
            go("2026-11-05 19:00", sub())
            check("11/5 19:00 reminder", sent, [msgs["rem"][0]])
            go("2026-11-05 19:05", sub())
            check("reminder once", len(sent), 1)
            go("2026-11-06 09:00", sub([k8]))
            check("11/6 09:00 8-K found", sent[-1], msgs["8k"][0])
            go("2026-11-06 19:00", sub([k8]))
            check("8-K dedupe", len(sent), 2)
            go("2026-11-06 19:00", sub([k8, q10]))
            check("10-Q found", sent[-1], msgs["10q"][0])
            go("2026-11-10 09:00", sub([k8, q10]))
            check("no missing notice when found", len(sent), 3)
            STATE.unlink()
            sent.clear()
            go("2026-11-06 09:00", sub())
            check("late reminder skipped after 11/6 06:00", sent, [])
            go("2026-11-09 19:00", sub())
            check("no missing before 11/10 09:00", sent, [])
            go("2026-11-10 09:00", sub())
            check("missing notice", sent, [msgs["miss"][0]])
            go("2026-11-10 19:00", sub())
            check("missing once", len(sent), 1)
            go("2026-11-12 09:00", sub([k8]))
            check("late 8-K still reported", sent[-1], msgs["8k"][0])
            n = len(sent)
            go("2026-11-17 09:00", sub([q10]))
            check("after 11/16 silent", len(sent), n)
            sent.clear()
            STATE.unlink()

            def boom():
                raise OSError("down")
            buf = io.StringIO()
            with mock.patch(__name__ + ".publish", side_effect=lambda topic, t, *a, **k: sent.append((topic, t))), redirect_stdout(buf):
                run(cfg, False, at("2026-11-06 09:00"), fetch_sub=boom, get=getter)
                run(cfg, False, at("2026-11-06 19:00"), fetch_sub=boom, get=getter)
            check("EDGAR fail -> ops once per day", [t for _, t in sent], [f"{NAME} EDGAR 조회 실패 | 매매 신호 아님"])
            check("ops topic", sent[0][0], resolve_topic("ops"))
    finally:
        STATE = old
    check("ends_kst", cfg["ends_kst"], "2026-11-16 23:59")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
