#!/usr/bin/env python3
"""[AMTM-EARN] Amentum FY26 Q4 results (approved in chat 2026-10-28).

  - 2026-11-23 (09:00 KST morning slot; the 19:00 k203780 slot catches up the same day): reminder with the
    4-item checklist and 3 scenarios. Once.
  - 2026-11-24 .. 2026-12-05 (09:00 morning and 19:00 k203780 slots): poll SEC EDGAR submissions for CIK 0002011286.
    A new 8-K with Item 2.02 filed on/after 2026-11-20 whose accession is not in seen_accessions -> one p4 message with
    the filing link, checklist thresholds, scenarios, and a simple keyword check (Savannah River, Sellafield, protest)
    on the 8-K main doc + Exhibit 99.1 text. Numbers are NOT auto-parsed (not robust); the user compares them.
    Dedupe by accession number.
  - No such 8-K by 2026-11-25 19:00 KST -> one p3 notice. Polling continues until 12/05.
Modes: morning | evening (slot label only) + auto | dry (print only, no ntfy, never writes state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "amtmearn.json"
STATE = ROOT / "state" / "amtmearn.json"
NAME = "[AMTM-EARN]"
BANNED_CHARS = ("\u00b7", "\u2014")
DRY = False


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_state(st: dict, dry: bool) -> None:
    if dry or DRY:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(kind: str, title: str, body: str, prio: int, tags: list[str], dry: bool) -> None:
    if dry or DRY:
        print(f"DRY RUN would send ({kind}, p{prio}): {title}\n{body}")
        return
    publish(resolve_topic(kind), title, body, prio, tags=tags, config_name="amtmearn")


# ---------- EDGAR ----------

def edgar_get(cfg: dict, url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": cfg["edgar"]["user_agent"], "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_text(h: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def recent_filings(sub: dict) -> list[dict]:
    r = sub["filings"]["recent"]
    return [{"form": r["form"][i], "date": r["filingDate"][i], "acc": r["accessionNumber"][i],
             "items": r.get("items", [""] * len(r["form"]))[i] or "", "doc": r["primaryDocument"][i]}
            for i in range(len(r["form"]))]


def pick_earnings(cfg: dict, filings: list[dict], seen: set[str]) -> dict | None:
    e = cfg["edgar"]
    for f in sorted(filings, key=lambda x: x["date"]):
        items = [x.strip() for x in f["items"].split(",")]
        if f["form"] == e["form"] and e["item"] in items and f["date"] >= e["min_filing_date"] and f["acc"] not in seen:
            return f
    return None


def filing_urls(cfg: dict, f: dict) -> tuple[str, str, str]:
    base = f"{cfg['edgar']['archive_base']}/{f['acc'].replace('-', '')}"
    return base, f"{base}/{f['acc']}-index.html", f"{base}/{f['doc']}"


def exhibit_name(index_json: dict) -> str | None:
    names = [i["name"] for i in index_json["directory"]["item"]]
    for n in names:
        if re.search(r"(ex|exhibit)[-_]?99[-_.]?1?", n, re.I) and n.lower().endswith((".htm", ".html", ".txt")):
            return n
    return None


def keyword_hits(cfg: dict, text: str) -> dict[str, bool]:
    low = text.lower()
    return {k: any(w in low for w in ws) for k, ws in cfg["contract_keywords"].items()}


def inspect_filing(cfg: dict, f: dict, get=None) -> dict:
    get = get or (lambda u: edgar_get(cfg, u))
    base, index_url, main_url = filing_urls(cfg, f)
    out = {"index_url": index_url, "main_url": main_url, "ex_url": None, "kw": None, "kw_note": ""}
    texts = []
    try:
        texts.append(html_text(get(main_url)))
    except Exception as exc:  # noqa: BLE001
        out["kw_note"] += f"8-K 본문 조회 실패({type(exc).__name__}). "
    try:
        ex = exhibit_name(json.loads(get(f"{base}/index.json")))
        if ex:
            out["ex_url"] = f"{base}/{ex}"
            texts.append(html_text(get(out["ex_url"])))
        else:
            out["kw_note"] += "Exhibit 99.1 파일 못 찾음. "
    except Exception as exc:  # noqa: BLE001
        out["kw_note"] += f"Exhibit 99.1 조회 실패({type(exc).__name__}). "
    if texts:
        out["kw"] = keyword_hits(cfg, " ".join(texts))
    return out


# ---------- messages ----------

def checklist_text(cfg: dict) -> str:
    return " ".join(f"{i}) {x}." for i, x in enumerate(cfg["checklist"], 1))


def scenario_text(cfg: dict) -> str:
    return " ".join(x.replace(" -> ", "이면 ") + "." for x in cfg["scenarios"])


def reminder_message(cfg: dict) -> tuple[str, str, int]:
    title = f"{NAME} 실적 발표 임박 | 실적 전 추가매수 보류"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | AMTM | 아멘텀 FY26 4분기 실적",
        "이게 뭐 알림: 아멘텀(AMTM) 실적 전 사전 알림(1회). " + cfg["source_note"],
        "지금 결과/상태: 실적은 미 동부 11/23 장 마감 후(Benzinga 표시) 또는 11/24 장 전, 컨퍼런스콜 전 발표. 콜은 11/24 08:30 ET(한국 11/24 22:30). 보유 34주, 평단 23.9888달러.",
        "지금 할 일: 실적 전에는 보유 유지, 추가매수 보류. 발표 뒤 볼 4가지: " + checklist_text(cfg),
        "나중에: 11/24 부터 09:00, 19:00 에 SEC 8-K(실적 공시)를 확인해 나오면 링크와 기준을 보냅니다. 판단은 3가지로: " + scenario_text(cfg),
        "익절/줄일 때: 시나리오 3이면 정리 검토(같은 해 다른 해외주식 이익과 손실 상계 고려).",
        "하지 말 것: 실적 전 미리 추가, 발표 직후 장전/시간외 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 3


def earnings_message(cfg: dict, f: dict, info: dict) -> tuple[str, str, int]:
    title = f"{NAME} 실적 8-K 공시 | 체크리스트 4개 대조"
    if info.get("kw") is None:
        kw = "계약 키워드 확인 불가(" + info.get("kw_note", "").strip() + ")"
    else:
        names = {"Savannah River": "Savannah River", "Sellafield": "Sellafield(PACE, OneAxIoM)", "protest": "protest(이의 제기)"}
        kw = "계약 키워드(단순 단어 확인, 내용 판단 아님): " + ", ".join(
            f"{names.get(k, k)} {'언급 있음' if v else '언급 없음'}" for k, v in info["kw"].items())
        if info.get("kw_note"):
            kw += f" ({info['kw_note'].strip()})"
    links = f"공시 목록 {info['index_url']}" + (f", 보도자료(Exhibit 99.1) {info['ex_url']}" if info.get("ex_url") else f", 8-K 본문 {info['main_url']}")
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | AMTM | 아멘텀 FY26 4분기 실적",
        "이게 뭐 알림: 아멘텀(AMTM) 실적 공시(SEC 8-K Item 2.02)가 올라와 1번 오는 알림. 숫자는 자동 추출하지 않음. " + cfg["source_note"],
        f"지금 결과/상태: {f['date']} 8-K 제출(접수번호 {f['acc']}). {links}. {kw}.",
        "지금 할 일: 보도자료 숫자를 아래 기준과 직접 비교하세요. " + checklist_text(cfg),
        "나중에: 비교 결과로 시나리오를 고르세요. " + scenario_text(cfg) + " 컨퍼런스콜 11/24 22:30 KST 에서 FY27 가이던스와 수주(Savannah River, Sellafield, 이의 제기 건) 설명 확인.",
        "익절/줄일 때: 시나리오 3이면 정리 검토(같은 해 다른 해외주식 이익과 손실 상계 고려). 시나리오 1, 2에서는 서둘러 팔지 않기.",
        "하지 말 것: 헤드라인 숫자만 보고 바로 주문, 장전/시간외 추격, 시나리오 확인 전 추가, 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 4


def missing_message(cfg: dict) -> tuple[str, str, int]:
    title = f"{NAME} 실적 8-K 아직 없음 | 발표일 확인"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | AMTM | 아멘텀 FY26 4분기 실적",
        "이게 뭐 알림: 11/25 19:00 KST 까지 SEC 에 실적 8-K(Item 2.02)가 안 보여 1번 오는 안내. " + cfg["source_note"],
        "지금 결과/상태: EDGAR(CIK 0002011286)에 2026-11-20 이후 Item 2.02 8-K 없음. 발표 일정 변경이거나 EDGAR 반영 지연일 수 있음.",
        "지금 할 일: 아멘텀 IR 페이지나 뉴스에서 발표 일정과 보도자료를 직접 확인. 그 전까지 보유 유지, 추가매수 보류.",
        "나중에: 12/5 까지 09:00, 19:00 에 계속 확인하고 8-K 가 보이면 링크와 기준을 1번 보냅니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 공시 확인 전 추측으로 주문, 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 3


# ---------- run ----------

def kst(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def run(cfg: dict, dry: bool, now: datetime, fetch_sub=None, get=None) -> int:
    st = load_state()
    ev = cfg["event"]
    today = now.date()
    if today.year != int(cfg.get("year_guard", today.year)) or today > date.fromisoformat(ev["poll_end_date"]):
        print("outside window / year guard; silent")
        return 0
    changed = False
    if today == date.fromisoformat(ev["reminder_date"]) and not st.get("reminder_sent"):
        t, b, p = reminder_message(cfg)
        send("port", t, b, p, ["calendar"], dry)
        st["reminder_sent"] = now.strftime("%Y-%m-%d %H:%M KST")
        changed = True
    if now >= kst(ev["poll_start_kst"]) and not st.get("found_accession"):
        fetch_sub = fetch_sub or (lambda: json.loads(edgar_get(cfg, cfg["edgar"]["submissions"])))
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
            f = pick_earnings(cfg, filings, seen)
            if f:
                info = inspect_filing(cfg, f, get)
                t, b, p = earnings_message(cfg, f, info)
                send("port", t, b, p, ["bar_chart"], dry)
                st.update({"found_accession": f["acc"], "found_date": f["date"], "found_at": now.strftime("%Y-%m-%d %H:%M KST"),
                           "found_index": info["index_url"], "keywords": info.get("kw")})
                st["seen_accessions"] = sorted(seen | {f["acc"]})
                changed = True
            else:
                print("no new Item 2.02 8-K")
                if now >= kst(ev["missing_notice_kst"]) and not st.get("missing_sent"):
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
    global DRY
    slot = (argv[1] if len(argv) > 1 else "morning").lower()
    if slot == "selftest":
        return selftest()
    mode = (argv[2] if len(argv) > 2 else "auto").lower()
    dry = mode != "auto"
    DRY = dry
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} amtmearn slot={slot} mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    return run(load_cfg(), dry, now)


# ---------- selftest ----------

def selftest() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global STATE, DRY
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    def sub(extra=()):
        rows = [("8-K", "2026-10-02", "0002011286-26-000026", "5.02,9.01", "amtm-20260928.htm"),
                ("8-K", "2026-08-10", "0001628280-26-055294", "2.02,9.01", "amtm-20260810.htm")] + list(extra)
        rows = sorted(rows, key=lambda r: r[1], reverse=True)
        return {"filings": {"recent": {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
                                       "accessionNumber": [r[2] for r in rows], "items": [r[3] for r in rows],
                                       "primaryDocument": [r[4] for r in rows]}}}

    new8k = ("8-K", "2026-11-24", "0001628280-26-077777", "2.02,7.01,9.01", "amtm-20261124.htm")
    other8k = ("8-K", "2026-11-23", "0001628280-26-077700", "8.01", "amtm-20261123.htm")
    pages = {
        "amtm-20261124.htm": "<html><body>Item 2.02 Results of Operations</body></html>",
        "index.json": json.dumps({"directory": {"item": [{"name": "amtm-20261124.htm"}, {"name": "exhibit991-november242026.htm"}, {"name": "R1.htm"}]}}),
        "exhibit991-november242026.htm": "<p>Amentum reports ... the Savannah River contract ... a bid protest was resolved. Growth pace remained steady.</p>",
    }
    getter = lambda u: pages[u.rsplit("/", 1)[1]]  # noqa: E731
    seen0 = {"0002011286-26-000026", "0001628280-26-055294"}
    check("old Item 2.02 (Aug) ignored by min date", pick_earnings(cfg, recent_filings(sub()), set()), None)
    check("non-2.02 8-K ignored", pick_earnings(cfg, recent_filings(sub([other8k])), seen0), None)
    f = pick_earnings(cfg, recent_filings(sub([other8k, new8k])), seen0)
    check("new Item 2.02 8-K detected", f and f["acc"], new8k[2])
    check("seen accession deduped", pick_earnings(cfg, recent_filings(sub([new8k])), seen0 | {new8k[2]}), None)
    info = inspect_filing(cfg, f, getter)
    check("exhibit 99.1 found", info["ex_url"], "https://www.sec.gov/Archives/edgar/data/2011286/000162828026077777/exhibit991-november242026.htm")
    check("keywords", info["kw"], {"Savannah River": True, "Sellafield": False, "protest": True})
    bad = inspect_filing(cfg, f, lambda u: (_ for _ in ()).throw(OSError("down")))
    check("keyword fetch failure -> None + note", (bad["kw"], "조회 실패" in bad["kw_note"]), (None, True))
    msgs = {"reminder": reminder_message(cfg), "earn": earnings_message(cfg, f, info),
            "earn_nokw": earnings_message(cfg, f, bad), "missing": missing_message(cfg)}
    for n, (t, b, p) in msgs.items():
        check(f"{n} no middle dot / em-dash", any(c in t + b for c in BANNED_CHARS), False)
        check(f"{n} six-part order", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check(f"{n} source note", "붙여넣은 외부 분석(2026-10-28)" in b and "투자 권유 아님" in b, True)
    check("earn p4", msgs["earn"][2], 4)
    check("missing p3", msgs["missing"][2], 3)
    check("earn has link + 4 thresholds + 3 scenarios", all(x in msgs["earn"][1] for x in
          (info["index_url"], "3.12억달러", "3.0배", "FY27 매출", "+20bp", "시나리오 1", "시나리오 2", "시나리오 3")), True)
    check("earn says not auto-parsed", "자동 추출하지 않음" in msgs["earn"][1], True)

    old = STATE
    sent: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE = Path(td) / "amtmearn.json"
            STATE.write_text(json.dumps({"seen_accessions": sorted(seen0)}), encoding="utf-8")
            before = STATE.read_bytes()
            S1 = lambda: sub()  # noqa: E731
            S2 = lambda: sub([other8k, new8k])  # noqa: E731

            def go(ts, s, dry=False):
                with mock.patch(f"{__name__}.publish", side_effect=lambda *a, **k: sent.append(a[1])), redirect_stdout(io.StringIO()):
                    run(cfg, dry, kst(ts), fetch_sub=s, get=getter)

            DRY = True
            go("2026-11-23 09:00", S2, True)
            go("2026-11-24 09:00", S2, True)
            go("2026-11-25 19:00", S1, True)
            DRY = False
            check("dry: state unchanged", STATE.read_bytes() == before, True)
            check("dry: nothing published", sent, [])
            go("2026-11-22 09:00", S1)
            check("before window silent", sent, [])
            go("2026-11-23 09:00", S1)
            go("2026-11-23 19:00", S1)
            check("reminder once", sent, [f"{NAME} 실적 발표 임박 | 실적 전 추가매수 보류"])
            go("2026-11-24 09:00", S1)
            go("2026-11-25 09:00", S1)
            check("no 8-K yet, before 11/25 19:00: silent", len(sent), 1)
            go("2026-11-25 19:00", S1)
            go("2026-11-26 09:00", S1)
            check("missing notice once", sent[1:], [f"{NAME} 실적 8-K 아직 없음 | 발표일 확인"])
            go("2026-11-26 19:00", S2)
            go("2026-11-27 09:00", S2)
            check("8-K message once (dedup by accession)", sent[2:], [f"{NAME} 실적 8-K 공시 | 체크리스트 4개 대조"])
            stj = json.loads(STATE.read_text())
            check("state found accession", (stj["found_accession"], new8k[2] in stj["seen_accessions"]), (new8k[2], True))
            go("2026-12-06 09:00", S2)
            check("after 12/05 silent", len(sent), 3)
            # EDGAR failure -> one ops notice per day
            STATE.write_text(json.dumps({"seen_accessions": sorted(seen0)}), encoding="utf-8")
            sent.clear()
            boom = lambda: (_ for _ in ()).throw(OSError("edgar down"))  # noqa: E731
            go("2026-11-24 09:00", boom)
            go("2026-11-24 19:00", boom)
            check("EDGAR fail notice once per day", [s for s in sent if "조회 실패" in s], [f"{NAME} EDGAR 조회 실패 | 매매 신호 아님"])
    finally:
        STATE, DRY = old, False
    print("\n=== sample 8-K earnings message ===")
    print(msgs["earn"][2], msgs["earn"][0])
    print(msgs["earn"][1])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
