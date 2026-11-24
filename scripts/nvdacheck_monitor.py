#!/usr/bin/env python3
"""[NVDA] 12/3 상원 청문 후 판단 조건 3가지 1회 점검 (2026-11-24 pt46u).

Attached to the existing weekday 09:00 KST morning slot (configs/nvdacheck.json).
The first run on or after 2026-12-04 00:00 KST checks three conservative, official-source
conditions, sends one summary, sets notified=true, and ends. Ends 2026-12-10 23:59 KST.
Lookup failure -> [OPS] [NVDA] ... once per day, retry until the deadline. No guesswork.

Modes: auto | dry [--now "YYYY-MM-DD HH:MM"] (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "nvdacheck.json"
STATE_PATH = ROOT / "state" / "nvdacheck.json"

C1_KEYWORDS = (
    "restatement",
    "material weakness",
    "wells notice",
    "subpoena",
    "sec investigation",
)
# Downward guidance revision, not a bare mention of outlook/guidance.
C2_CUT_RE = re.compile(
    r"revis(?:e|es|ed|ing).{0,60}(?:outlook|guidance)"
    r"|(?:outlook|guidance).{0,60}(?:revis(?:e|es|ed|ing)|lower|reduc|cut|below)"
    r"|updates?.{0,40}(?:outlook|guidance)"
    r"|below(?: the)? previously"
    r"|(?:lower(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|cut(?:s|ting)?).{0,40}(?:outlook|guidance)",
    re.I,
)
C3_CONTEXT_RE = re.compile(
    r"export[\s-]*control|export[\s-]*restriction|\bbis\b|"
    r"department of justice|\bdoj\b|national security",
    re.I,
)
C3_VIOLATION_RE = re.compile(
    r"violat(?:e|es|ed|ing|ion)|enforcement|indictment|charged|"
    r"willfully|unlawful|penalty|penalties|subpoena|found that",
    re.I,
)
Q4_LIMIT = "4Q 실적 발표는 2027년 2월 예정이라 이번 1회 점검 범위 밖"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": False, "ops_fetch_day": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(cfg: dict, kind: str, title: str, lines: list[str], prio: int) -> None:
    topic = resolve_topic(kind, cfg.get("ntfy_topic") if kind == "port" else cfg.get("ntfy_ops_topic"))
    publish(
        topic, title, "\n".join(lines), prio,
        ["warning"] if kind == "ops" else ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def in_window(cfg: dict, now: datetime) -> bool:
    if now.year != int(cfg["year_guard"]):
        return False
    day = now.strftime("%Y-%m-%d")
    if not (cfg["window_start"] <= day <= cfg["window_end"]):
        return False
    raw = (cfg.get("ends_kst") or "").replace("T", " ").split(" KST")[0].strip()
    try:
        end = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return True
    return now <= end


def html_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def http_get(url: str, user_agent: str, timeout: int = 25, max_bytes: int = 2_000_000) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    return data[:max_bytes].decode("utf-8", errors="replace")


def recent_filings(sub: dict) -> list[dict]:
    recent = (sub or {}).get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    n = len(forms)
    items = recent.get("items") or [""] * n
    dates = recent.get("filingDate") or [""] * n
    accs = recent.get("accessionNumber") or [""] * n
    docs = recent.get("primaryDocument") or [""] * n
    out = []
    for i in range(n):
        out.append({
            "form": forms[i],
            "date": dates[i] if i < len(dates) else "",
            "acc": accs[i] if i < len(accs) else "",
            "items": items[i] if i < len(items) else "",
            "doc": docs[i] if i < len(docs) else "",
        })
    return out


def item_list(row: dict) -> list[str]:
    return [p.strip() for p in (row.get("items") or "").split(",") if p.strip()]


def has_item(row: dict, codes: set[str]) -> bool:
    return bool(codes.intersection(item_list(row)))


def in_filing_window(row: dict, start: str, end: str) -> bool:
    day = row.get("date") or ""
    return bool(day) and start <= day <= end


def filing_urls(cfg: dict, row: dict) -> tuple[str, str]:
    base = f"{cfg['edgar']['archive_base']}/{str(row.get('acc') or '').replace('-', '')}"
    doc = row.get("doc") or ""
    index = f"{base}/{row.get('acc')}-index.html"
    main = f"{base}/{doc}" if doc else index
    return index, main


def link_for(cfg: dict, row: dict) -> str:
    _, main = filing_urls(cfg, row)
    return main


def keyword_hits(text: str, words: tuple[str, ...]) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if w in low]


# ---------- condition judges (pure; texts injected) ----------

def judge_c1(cfg: dict, rows: list[dict], texts: dict[str, str | None]) -> dict:
    """Accounting / restatement. Item 4.02 or 10-Q / 10-K/A keyword hit -> 충족."""
    start, end = cfg["filing_start"], cfg["_asof"]
    four02 = [r for r in rows if r["form"] in {"8-K", "8-K/A"} and has_item(r, {"4.02"}) and in_filing_window(r, start, end)]
    qs = [r for r in rows if r["form"] in {"10-Q", "10-Q/A", "10-K/A"} and in_filing_window(r, start, end)]
    evidence = []
    hits = []
    text_fail = False
    for r in four02:
        evidence.append(f"8-K Item 4.02 {r['date']} {link_for(cfg, r)}")
    for r in qs:
        body = texts.get(r["acc"], None)
        url = link_for(cfg, r)
        if body is None:
            text_fail = True
            evidence.append(f"{r['form']} {r['date']} 본문 조회 실패 {url}")
            continue
        found = keyword_hits(body, C1_KEYWORDS)
        if found:
            hits.extend(found)
            evidence.append(f"{r['form']} {r['date']} 키워드 {', '.join(found)} {url}")
        else:
            evidence.append(f"{r['form']} {r['date']} 해당 키워드 없음 {url}")
    if four02 or hits:
        return {"verdict": "충족", "evidence": evidence or ["Item 4.02 또는 키워드 확인"]}
    if text_fail and not four02:
        return {"verdict": "판정 유보", "evidence": evidence + ["원문 확인 필요"]}
    note = "Item 4.02 없음"
    if not qs:
        note += ". 점검 창 안 10-Q/10-K/A 없음(제출되면 본문 키워드를 추가로 봄)"
    return {"verdict": "미충족", "evidence": evidence + [note]}


def judge_c2(cfg: dict, rows: list[dict], texts: dict[str, str | None]) -> dict:
    """Guidance-revision 8-K only. 4Q results are out of scope."""
    start, end = cfg["filing_start"], cfg["_asof"]
    cands = [
        r for r in rows
        if r["form"] in {"8-K", "8-K/A"}
        and has_item(r, {"2.02", "7.01", "8.01"})
        and in_filing_window(r, start, end)
    ]
    evidence = [Q4_LIMIT]
    cuts, unclear, failed = [], [], []
    for r in cands:
        url = link_for(cfg, r)
        items = r.get("items") or ""
        body = texts.get(r["acc"], None)
        if body is None:
            failed.append(r)
            evidence.append(f"8-K Item {items} {r['date']} 본문 조회 실패 {url}")
            continue
        if C2_CUT_RE.search(body):
            cuts.append(r)
            evidence.append(f"8-K Item {items} {r['date']} 가이던스 하향 취지 문구 {url}")
        else:
            unclear.append(r)
            evidence.append(f"8-K Item {items} {r['date']} 가이던스 하향 단정 문구 없음 {url}")
    if cuts:
        return {"verdict": "충족", "evidence": evidence}
    if failed or unclear:
        # A 2.02/7.01/8.01 exists but we will not guess a cut.
        extra = "원문 확인 필요" if (failed or unclear) else ""
        return {"verdict": "판정 유보", "evidence": evidence + ([extra] if extra else [])}
    evidence.append(f"{start}~{end} Item 2.02/7.01/8.01 8-K 없음")
    return {"verdict": "미충족", "evidence": evidence}


def judge_c3(cfg: dict, rows: list[dict], texts: dict[str, str | None], pages: list[dict]) -> dict:
    """Export-control / national-security violation. No guess if official text is missing."""
    start, end = cfg["filing_start"], cfg["_asof"]
    cands = [
        r for r in rows
        if r["form"] in {"8-K", "8-K/A"}
        and has_item(r, {"8.01", "5.02"})
        and in_filing_window(r, start, end)
    ]
    evidence = []
    triggered = []
    weak = []
    text_fail = False
    for r in cands:
        url = link_for(cfg, r)
        body = texts.get(r["acc"], None)
        items = r.get("items") or ""
        if body is None:
            text_fail = True
            evidence.append(f"8-K Item {items} {r['date']} 본문 조회 실패 {url}")
            continue
        has_ctx = bool(C3_CONTEXT_RE.search(body))
        has_vio = bool(C3_VIOLATION_RE.search(body))
        if has_ctx and has_vio:
            triggered.append(r)
            evidence.append(f"8-K Item {items} {r['date']} 수출통제/안보 위반 취지 문구 {url}")
        elif has_ctx or has_vio:
            weak.append(r)
            evidence.append(f"8-K Item {items} {r['date']} 관련 단어만 있고 위반 단정 문장 없음 {url}")
        else:
            evidence.append(f"8-K Item {items} {r['date']} 해당 키워드 없음 {url}")

    page_fail = False
    page_ok = 0
    for p in pages:
        url, name = p.get("url") or "", p.get("name") or "위원회 자료"
        body = p.get("text", None)
        if p.get("error") or body is None:
            page_fail = True
            evidence.append(f"{name} 조회 실패({p.get('error') or 'empty'}) {url}")
            continue
        page_ok += 1
        has_ctx = bool(C3_CONTEXT_RE.search(body))
        has_vio = bool(C3_VIOLATION_RE.search(body))
        if has_ctx and has_vio:
            triggered.append(p)
            evidence.append(f"{name} 수출통제/안보 위반 취지 문구 {url}")
        elif has_ctx or has_vio:
            weak.append(p)
            evidence.append(f"{name} 관련 단어만 있고 위반 단정 문장 없음 {url}")
        else:
            evidence.append(f"{name} 해당 키워드 없음 {url}")

    if triggered:
        return {"verdict": "충족", "evidence": evidence}
    # Missing official committee text, or only weak keywords: do not guess.
    if page_fail or text_fail or weak:
        evidence.append("판정 유보, 원문 확인 필요")
        return {"verdict": "판정 유보", "evidence": evidence}
    if not cands and page_ok == 0:
        evidence.append("판정 유보, 원문 확인 필요")
        return {"verdict": "판정 유보", "evidence": evidence}
    evidence.append("공식 원문에서 수출통제/안보 위반을 단정하는 문구 없음")
    return {"verdict": "미충족", "evidence": evidence}


def summary_title(cfg: dict, verdicts: dict[str, dict]) -> str:
    n = sum(1 for v in verdicts.values() if v["verdict"] == "충족")
    prefix = cfg["title_prefix"]
    if n:
        return f"{prefix} 12/3 청문 후 판단 조건 점검 | 3개 중 {n}개 충족 | 관망 전환 검토"
    return f"{prefix} 12/3 청문 후 판단 조건 점검 | 충족 없음 | 판단 유지"


def ops_title(cfg: dict) -> str:
    return f"[OPS] {cfg['title_prefix']} 12/3 청문 후 판단 조건 점검 | 조회 실패"


def result_lines(cfg: dict, verdicts: dict[str, dict], now: datetime) -> list[str]:
    labels = {
        "c1": "조건1 회계/정정(8-K Item 4.02 또는 10-Q·10-K/A 키워드)",
        "c2": "조건2 가이던스 하향 수정 8-K(Item 2.02/7.01/8.01)",
        "c3": "조건3 수출통제/안보 위반(8-K Item 8.01/5.02 및 위원회 공식 자료)",
    }
    parts = []
    for key in ("c1", "c2", "c3"):
        v = verdicts[key]
        ev = "; ".join(v.get("evidence") or [])
        parts.append(f"{labels[key]}: {v['verdict']}. {ev}")
    n = sum(1 for v in verdicts.values() if v["verdict"] == "충족")
    stance = "관망 전환 검토" if n else "판단 유지(충족된 조건 없음)"
    return [
        "이게 뭐 알림: NVIDIA(NVDA) 12/3 상원 은행위 청문 뒤, 기존 '매력' 판단을 '관망'으로 바꿀 조건 3가지를 공식 일차 자료로 한 번 점검합니다. 투자 권유가 아닙니다.",
        (
            f"지금 결과/상태: {stance}. " + " ".join(parts) + " "
            f"{cfg.get('guidance_note') or Q4_LIMIT} "
            f"조회 {now:%Y-%m-%d %H:%M} KST. EDGAR CIK {cfg.get('cik')} 제출분 {cfg['filing_start']}~{cfg['_asof']}."
        ),
        "지금 할 일: 본문 링크의 원문을 확인하고 판단을 직접 하세요. 숫자는 추정하지 않았습니다.",
        "하지 말 것: 키워드만으로 확정 처분으로 보기, 익명 보도만으로 조건 충족으로 보기, 이 알림을 매수/매도 지시로 보기. 투자 권유 아님.",
    ]


def defer_lines(cfg: dict, now: datetime, detail: str) -> list[str]:
    return [
        f"{now:%Y-%m-%d %H:%M} KST | OPS | nvdacheck",
        "이게 뭐 알림: NVIDIA 12/3 청문 후 판단 조건 점검을 이번 실행에서는 마치지 못했습니다. 투자 판단 알림이 아닙니다. 숫자는 추정하지 않습니다.",
        f"지금 상태: {detail}. notified는 그대로 false이며 다음날 09:00에 다시 봅니다. {cfg['edgar']['submissions']}",
        "지금 할 일: 로그와 EDGAR를 직접 확인하세요.",
    ]


def fetch_bundle(cfg: dict, rows: list[dict], get) -> tuple[dict[str, str | None], list[dict], list[str]]:
    """Fetch 8-K / 10-Q texts and committee pages. Returns (texts, pages, errors)."""
    start, end = cfg["filing_start"], cfg["_asof"]
    needed = []
    for r in rows:
        if not in_filing_window(r, start, end):
            continue
        form, items = r["form"], set(item_list(r))
        if form in {"10-Q", "10-Q/A", "10-K/A"}:
            needed.append(r)
        elif form in {"8-K", "8-K/A"} and items.intersection({"4.02", "2.02", "7.01", "8.01", "5.02"}):
            needed.append(r)
    texts: dict[str, str | None] = {}
    errors: list[str] = []
    for r in needed:
        _, url = filing_urls(cfg, r)
        try:
            texts[r["acc"]] = html_text(get(url))
        except Exception as exc:  # noqa: BLE001
            texts[r["acc"]] = None
            errors.append(f"{r['form']} {r['acc']} {type(exc).__name__}")
    pages = []
    for src in cfg.get("committee_sources") or []:
        url = src.get("url") or ""
        name = src.get("name") or url
        try:
            pages.append({"name": name, "url": url, "text": html_text(get(url)), "error": None})
        except Exception as exc:  # noqa: BLE001
            pages.append({"name": name, "url": url, "text": None, "error": type(exc).__name__})
            errors.append(f"{name} {type(exc).__name__}")
    return texts, pages, errors


def evaluate(cfg: dict, rows: list[dict], texts: dict[str, str | None], pages: list[dict]) -> dict[str, dict]:
    return {
        "c1": judge_c1(cfg, rows, texts),
        "c2": judge_c2(cfg, rows, texts),
        "c3": judge_c3(cfg, rows, texts, pages),
    }


def run(
    cfg: dict,
    state: dict,
    now: datetime,
    mode: str,
    rows: list[dict] | None = None,
    texts: dict[str, str | None] | None = None,
    pages: list[dict] | None = None,
    fetch_exc: Exception | None = None,
    emit=None,
    persist=None,
) -> int:
    dry = mode == "dry"
    day = now.strftime("%Y-%m-%d")
    cfg = dict(cfg)
    cfg["_asof"] = day

    def out(kind: str, title: str, lines: list[str], prio: int) -> None:
        if emit is not None:
            emit(kind, title, lines, prio)
            return
        if dry:
            print(title, *lines, "dry-run; no ntfy", sep="\n")
            return
        send(cfg, kind, title, lines, prio)

    def store() -> None:
        if persist is not None:
            persist(state)
            return
        if not dry:
            save_state(state, now)

    if state.get("notified"):
        print("nvdacheck already notified; silent")
        return 0

    ua = cfg["edgar"]["user_agent"]

    def live_get(url: str) -> str:
        return http_get(url, ua)

    if fetch_exc is not None:
        rows_err: Exception | None = fetch_exc
    elif rows is None:
        try:
            sub = json.loads(live_get(cfg["edgar"]["submissions"]))
            rows = recent_filings(sub)
            rows_err = None
        except Exception as exc:  # noqa: BLE001
            rows_err = exc
    else:
        rows_err = None

    if rows_err is not None:
        print(f"::warning::EDGAR fetch failed: {type(rows_err).__name__}", file=sys.stderr)
        if state.get("ops_fetch_day") != day:
            out("ops", ops_title(cfg), defer_lines(cfg, now, f"EDGAR submissions 조회 실패({type(rows_err).__name__})"), 3)
            if not dry:
                state["ops_fetch_day"] = day
                store()
        else:
            print("ops fetch already sent today; silent")
        return 0

    if texts is None or pages is None:
        got_texts, got_pages, _errs = fetch_bundle(cfg, rows or [], live_get)
        if texts is None:
            texts = got_texts
        if pages is None:
            pages = got_pages

    verdicts = evaluate(cfg, rows or [], texts or {}, pages or [])
    title = summary_title(cfg, verdicts)
    lines = result_lines(cfg, verdicts, now)
    out("port", title, lines, 4)
    if not dry:
        state.update({
            "notified": True,
            "verdict": {k: v["verdict"] for k, v in verdicts.items()},
            "last_title": title,
            "sent_at": now.isoformat(timespec="seconds"),
        })
        store()
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        raise SystemExit(f"unknown mode: {mode} (auto|dry|selftest)")
    now = datetime.now(KST)
    if "--now" in args:
        if mode != "dry":
            raise SystemExit("--now is for dry only")
        now = datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    if mode == "dry":
        print(f"DRY RUN (no ntfy, no state write) now={now:%Y-%m-%d %H:%M} KST")
    if not in_window(cfg, now):
        if mode == "dry":
            try:
                raw = http_get(cfg["edgar"]["submissions"], cfg["edgar"]["user_agent"])
                rows = recent_filings(json.loads(raw))
                cfg = dict(cfg)
                cfg["_asof"] = now.strftime("%Y-%m-%d")
                start = cfg["filing_start"]
                relevant = [r for r in rows if in_filing_window(r, start, cfg["_asof"]) and (
                    r["form"] in {"10-Q", "10-Q/A", "10-K/A"}
                    or (r["form"] in {"8-K", "8-K/A"} and has_item(r, {"4.02", "2.02", "7.01", "8.01", "5.02"}))
                )]
                print(
                    f"outside window {cfg['window_start']}~{cfg['window_end']}; "
                    f"dry EDGAR filings={len(rows)} relevant={len(relevant)}; silent"
                )
                for r in relevant[:8]:
                    print("  ", r["date"], r["form"], r.get("items"), r.get("acc"))
            except Exception as exc:  # noqa: BLE001
                print(f"outside window; dry EDGAR fetch failed ({type(exc).__name__}); silent")
        else:
            print(f"outside window {cfg['window_start']}~{cfg['window_end']}; silent")
        return 0
    return run(cfg, state, now, mode)


# ---------------- selftest ----------------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0
    inside = datetime(2026, 12, 4, 9, 0, tzinfo=KST)
    cfg_run = dict(cfg)
    cfg_run["_asof"] = "2026-12-04"

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, got, "" if ok else f"want {want}")
        fails += not ok

    def row(form, day, acc, items="", doc="x.htm"):
        return {"form": form, "date": day, "acc": acc, "items": items, "doc": doc}

    def play(rows, texts=None, pages=None, now=None, state=None, mode="auto", fetch_exc=None):
        st = {"notified": False, "ops_fetch_day": None}
        if state is not None:
            st.update(state)
        sent: list = []

        def emit(kind, title, lines, prio):
            sent.append((kind, title, lines, prio))

        written = {}

        def persist(s):
            written.clear()
            written.update(s)

        pages = pages if pages is not None else [
            {"name": "Senate Banking hearings", "url": "https://www.banking.senate.gov/hearings",
             "text": "Upcoming hearings calendar", "error": None},
            {"name": "House Select Committee on the CCP media", "url": "https://selectcommitteeontheccp.house.gov/media",
             "text": "Press releases", "error": None},
        ]
        rc = run(
            cfg, st, now or inside, mode,
            rows=rows if fetch_exc is None else None,
            texts=texts if texts is not None else {},
            pages=pages,
            fetch_exc=fetch_exc, emit=emit, persist=persist,
        )
        if written:
            st.update(written)
        return sent, st, rc

    # --- condition units ---
    r402 = row("8-K", "2026-12-01", "0001045810-26-000401", "4.02,9.01")
    r10q = row("10-Q", "2026-12-02", "0001045810-26-000402", "", "q.htm")
    r202 = row("8-K", "2026-12-03", "0001045810-26-000403", "2.02,9.01")
    r801 = row("8-K", "2026-12-03", "0001045810-26-000404", "8.01")
    r502 = row("8-K", "2026-11-21", "0001045810-26-000405", "5.02")
    old = row("8-K", "2026-11-10", "0001045810-26-000300", "4.02,9.01")  # before filing_start

    c1 = judge_c1(cfg_run, [r402, old], {})
    check("c1 Item 4.02 충족", c1["verdict"], "충족")
    check("c1 4.02는 filing_start 이후만", "0001045810-26-000300" not in " ".join(c1["evidence"]), True)

    c1 = judge_c1(cfg_run, [r10q], {"0001045810-26-000402": "The company identified a material weakness in internal control and is considering a restatement."})
    check("c1 10-Q 키워드 충족", c1["verdict"], "충족")
    check("c1 키워드 목록", "material weakness" in " ".join(c1["evidence"]) and "restatement" in " ".join(c1["evidence"]), True)

    c1 = judge_c1(cfg_run, [r202], {"0001045810-26-000403": "earnings release outlook unchanged"})
    check("c1 미충족(4.02/10-Q 없음)", c1["verdict"], "미충족")

    c1 = judge_c1(cfg_run, [r10q], {"0001045810-26-000402": None})
    check("c1 10-Q 본문 실패 유보", c1["verdict"], "판정 유보")

    cut_txt = "The company revises its outlook for the fourth quarter and now expects revenue below previously announced guidance."
    c2 = judge_c2(cfg_run, [r202], {"0001045810-26-000403": cut_txt})
    check("c2 가이던스 하향 충족", c2["verdict"], "충족")
    check("c2 본문에 4Q 범위 밖 문구", Q4_LIMIT in " ".join(c2["evidence"]), True)

    c2 = judge_c2(cfg_run, [], {})
    check("c2 해당 8-K 없음 미충족", c2["verdict"], "미충족")
    check("c2 미충족에도 한계 문구", Q4_LIMIT in " ".join(c2["evidence"]), True)

    c2 = judge_c2(cfg_run, [r202], {"0001045810-26-000403": "The company confirmed its previously announced outlook for the fourth quarter."})
    check("c2 하향 단정 없음 유보", c2["verdict"], "판정 유보")

    c2 = judge_c2(cfg_run, [r202], {"0001045810-26-000403": None})
    check("c2 본문 실패 유보", c2["verdict"], "판정 유보")

    viol = "The committee found that the company willfully committed an export control violation and referred the matter to the Department of Justice and BIS."
    c3 = judge_c3(cfg_run, [r801], {"0001045810-26-000404": viol}, [
        {"name": "Senate Banking hearings", "url": "https://www.banking.senate.gov/hearings", "text": "calendar", "error": None}
    ])
    check("c3 8-K 위반 문구 충족", c3["verdict"], "충족")

    pages_ok = [
        {"name": "Senate Banking hearings", "url": "https://www.banking.senate.gov/hearings", "text": "hearing calendar", "error": None},
        {"name": "House media", "url": "https://selectcommitteeontheccp.house.gov/media", "text": "press list", "error": None},
    ]
    c3 = judge_c3(cfg_run, [r801, r502], {
        "0001045810-26-000404": "The company announced a routine officer appointment.",
        "0001045810-26-000405": "Director resignation, no other matters.",
    }, pages_ok)
    check("c3 키워드 없음 미충족", c3["verdict"], "미충족")

    c3 = judge_c3(cfg_run, [r801], {
        "0001045810-26-000404": "Management discussed export control licensing and national security reviews in China.",
    }, pages_ok)
    check("c3 약한 키워드 유보", c3["verdict"], "판정 유보")
    check("c3 유보 문구", any("원문 확인 필요" in e for e in c3["evidence"]), True)

    c3 = judge_c3(cfg_run, [r801], {"0001045810-26-000404": "routine update"}, [
        {"name": "Senate Banking hearings", "url": "https://www.banking.senate.gov/hearings", "text": None, "error": "HTTPError"}
    ])
    check("c3 위원회 조회 실패 유보", c3["verdict"], "판정 유보")

    # --- titles ---
    check("제목 충족", summary_title(cfg, {"c1": {"verdict": "충족"}, "c2": {"verdict": "미충족"}, "c3": {"verdict": "판정 유보"}}),
          "[NVDA] 12/3 청문 후 판단 조건 점검 | 3개 중 1개 충족 | 관망 전환 검토")
    check("제목 유지", summary_title(cfg, {"c1": {"verdict": "미충족"}, "c2": {"verdict": "미충족"}, "c3": {"verdict": "판정 유보"}}),
          "[NVDA] 12/3 청문 후 판단 조건 점검 | 충족 없음 | 판단 유지")
    check("OPS 제목", ops_title(cfg), "[OPS] [NVDA] 12/3 청문 후 판단 조건 점검 | 조회 실패")

    # --- run: clean filings, one send ---
    sent, st, _ = play(
        [r202],
        texts={"0001045810-26-000403": "The company confirmed its previously announced outlook."},
        pages=pages_ok,
    )
    check("통합 발송 1통", len(sent), 1)
    check("통합 제목 유지", sent[0][1] if sent else None, "[NVDA] 12/3 청문 후 판단 조건 점검 | 충족 없음 | 판단 유지")
    check("본문 조건 판정", sent and all(x in sent[0][2][1] for x in ("조건1", "조건2", "조건3", "미충족", "판정 유보")), True)
    check("본문 4Q 한계", sent and Q4_LIMIT in sent[0][2][1], True)
    check("본문 투자 권유 아님", sent and any("투자 권유 아님" in ln for ln in sent[0][2]), True)
    check("notified", st.get("notified") is True, True)

    sent2, _, _ = play([r402], texts={}, state=st)
    check("notified 후 재발송 없음", sent2, [])

    sent, st, _ = play([r402, r202], texts={"0001045810-26-000403": cut_txt}, pages=pages_ok)
    check("2개 충족 제목", sent[0][1] if sent else None, "[NVDA] 12/3 청문 후 판단 조건 점검 | 3개 중 2개 충족 | 관망 전환 검토")

    # outside window
    check("창 전", in_window(cfg, datetime(2026, 12, 3, 23, 59, tzinfo=KST)), False)
    check("창 시작 09:00", in_window(cfg, datetime(2026, 12, 4, 9, 0, tzinfo=KST)), True)
    check("창 마감", in_window(cfg, datetime(2026, 12, 10, 23, 59, tzinfo=KST)), True)
    check("창 후", in_window(cfg, datetime(2026, 12, 11, 0, 0, tzinfo=KST)), False)
    check("연도 가드", in_window(cfg, datetime(2027, 12, 4, 9, 0, tzinfo=KST)), False)

    # fetch fail once per day
    sent, st, _ = play(None, fetch_exc=RuntimeError("x"))
    check("조회 실패 제목", sent[0][1] if sent else None, ops_title(cfg))
    check("조회 실패 notified false", st.get("notified") is False, True)
    sent2, _, _ = play(None, fetch_exc=RuntimeError("x"), state=st)
    check("조회 실패 하루 1회", sent2, [])
    sent3, _, _ = play(None, fetch_exc=RuntimeError("x"), now=datetime(2026, 12, 7, 9, 0, tzinfo=KST), state=st)
    check("조회 실패 다음날 재시도", bool(sent3) and sent3[0][1] == ops_title(cfg), True)

    sent, st, _ = play([r202], texts={"0001045810-26-000403": cut_txt}, mode="dry", pages=pages_ok)
    check("dry notified 유지 false", st.get("notified") is False and bool(sent), True)

    check("title_prefix", cfg["title_prefix"], "[NVDA]")
    check("topics", (cfg["ntfy_topic"], cfg["ntfy_ops_topic"]), ("cwally-port", "cwally-ops"))
    check("window", (cfg["window_start"], cfg["window_end"], cfg["year_guard"], cfg["ends_kst"]),
          ("2026-12-04", "2026-12-10", 2026, "2026-12-10 23:59"))

    print("nvdacheck selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
