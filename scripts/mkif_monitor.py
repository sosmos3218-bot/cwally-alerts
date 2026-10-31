#!/usr/bin/env python3
"""[MKIF] 맥쿼리인프라(088980) 배당 목적 분할매수 검토 알림 (approved in chat 2026-10-29).

price (morning 09:00 KST slot, completed KRX daily close from the Naver price API):
  - close <= 8,950  p3 '분할매수 검토 구간' (분배금 하향, 비엔씨티 재구조화 없을 때만).
                    hold-only version when the latest distribution <= 340 or the 비엔씨티 flag is set.
  - close <= 8,300  p4 warning only (급락, 원인 확인, 추가매수 보류). Overrides the 8,950 buy text.
  - close >= 11,300 p3 '신규매수 보류, 배당 매력 낮아짐'.
  Each line fires once on entry and re-arms quietly on exit. The first run only records state.
disc (k203780 19:00 KST slot, Naver KRX notice feed with full text; no DART key in this repo):
  - 금전분배 결의 / 예상분배금: parse 분배금액(원). >=350 p3 기준 충족, 341~349 p3 중립, <=340 p4 매수 보류 조건,
    unparsed p3 금액 원문 확인. Latest amount kept in state (dist_latest).
  - results release '분배금으로 주당 N원': silent when equal to dist_latest, else treated as a distribution notice.
  - 비엔씨티 restructuring / rate / impairment notice: p4, sets bnct_flag.
  - new investment / acquisition / borrowing / capital raise: p3.
  - other material (재구조화, 소송, 처분, 청산 ...): p3.
  - routine filings (보고서 게시, 투자설명서 갱신, 분배락, 주주명부 ...) ignored.
  - missing reminder: if no expected-distribution notice for a period by its due time, one p3.
  Dedupe by notice number (state seen). The first run seeds every listed notice silently.
Modes: price|disc [auto|dry] | confirm (install-confirmation send, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402
import naver_api  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "mkif.json"
STATE = ROOT / "state" / "mkif.json"
NOTICE_URL = "https://m.stock.naver.com/domestic/stock/088980/notice"
DISCLAIMER = "투자 권유 아님."


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool, now: datetime) -> None:
    if dry:
        return
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text: str) -> str:
    """External text (notice titles) must not bring the middle dot or the em-dash into a message."""
    return str(text).replace("\u00b7", ", ").replace("\u2014", "-")


def won(v: float) -> str:
    return f"{v:,.0f}원"


def yld(dps: float, px: float) -> str:
    return f"{dps / px * 100:.1f}%"


# ---------- price ----------

def fetch_closes(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    """Completed daily closes [(YYYY-MM-DD, close)] ascending. Today's row only after close_ready_kst."""
    rows = naver_api.get_json(cfg["sources"]["price"])
    if isinstance(rows, dict):
        rows = rows.get("result") or []
    hh, mm = (int(x) for x in cfg.get("close_ready_kst", "15:40").split(":"))
    closed = now.hour * 60 + now.minute >= hh * 60 + mm
    today = now.strftime("%Y-%m-%d")
    out = {}
    for r in rows or []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = naver_api.num(r.get("closePrice"))
        if not d or c is None or (d == today and not closed):
            continue
        out[d] = float(c)
    return sorted(out.items())


def hit(lv: dict, close: float) -> bool:
    return close <= float(lv["price"]) if lv["op"] == "le" else close >= float(lv["price"])


def step(levels: list[dict], armed: dict | None, close: float) -> tuple[dict, list[str]]:
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k, inside = lv["key"], hit(lv, close)
        if new.get(k, True):
            if inside:
                new[k] = False
                fired.append(k)
        elif not inside:
            new[k] = True
    return new, fired


def hold_reasons(cfg: dict, st: dict, today: date) -> list[str]:
    out = []
    dl = st.get("dist_latest") or {}
    if dl.get("amount") is not None and float(dl["amount"]) <= float(cfg["dist"]["low_max"]):
        out.append(f"최근 분배금 공시 {won(dl['amount'])}({dl.get('filed', '')}), 340원 이하")
    bf = st.get("bnct_flag") or {}
    if bf.get("date"):
        age = (today - date.fromisoformat(bf["date"])).days
        if age <= int(cfg.get("bnct_flag_days", 365)):
            out.append(f"비엔씨티 관련 공시({bf['date']}): {clean(bf.get('subject', ''))}")
    return out


def price_message(cfg: dict, fired: list[str], close: float, day: str, holds: list[str]) -> tuple[str, str, int]:
    b = cfg["basis"]
    y680, y700 = yld(b["dps_2027e"], close), yld(b["dps_2026e"], close)
    stamp = f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | MKIF | 맥쿼리인프라(088980)"
    base = f"{day} 종가 {won(close)}. 예상 DPS 기준 수익률: 2026년 700원 {y700}, 2027년 680원 {y680}."
    later = ("12월 중순 하반기 예상분배금 공시(상반기는 6/15)를 보세요. 350원 이상이거나 금리 10% 안팎 후순위대출 중심 "
             "신규 투자가 확정되면 매력 증가, 340원 이하나 비엔씨티 추가 재구조화, 금리 재상승이면 미루기. 이 알림이 공시도 같이 봅니다.")
    trim = "아직 보유 없음. 보유하게 되면 11,300원 이상(수익률 약 6.2%)에서는 배당 매력이 낮아져 추가 편입보다 비중 점검이 먼저입니다."
    if "warn8300" in fired:
        title = f"[MKIF] 088980 종가 {won(close)}, 8,300원 이하 급락 | 추가매수 보류 후 원인 확인"
        status = base + " 8,300원 이하 구간에 새로 들어왔습니다." + (" 8,950원 선도 같이 걸렸지만 경고가 우선합니다." if "buy8950" in fired else "")
        if holds:
            status += " 진행 중인 보류 조건: " + "; ".join(holds) + "."
        todo = "추가매수 보류. 급락 원인부터 확인하세요: 분배금 하향 공시, 금리 급등, 비엔씨티 재구조화, 신규 투자에 따른 차입 확대."
        avoid = "원인 확인 전 주문, 하락 중 물타기, 신용/미수. " + DISCLAIMER
        prio = 4
    elif "buy8950" in fired and holds:
        title = f"[MKIF] 088980 종가 {won(close)}, 8,950원 이하 | 매수 보류 조건 진행 중"
        status = base + " 8,950원 이하 구간에 들어왔지만 보류 조건이 있습니다: " + "; ".join(holds) + "."
        todo = "보류 유지. 다음 예상분배금 공시와 비엔씨티 관련 내용을 원문으로 먼저 확인하세요."
        avoid = "가격만 보고 주문, 신용/미수. " + DISCLAIMER
        prio = 3
    elif "buy8950" in fired:
        title = f"[MKIF] 088980 종가 {won(close)}, 8,950원 이하 | 분할매수 검토 구간"
        status = base + " 8,950원 이하 구간에 새로 들어왔습니다. 2027년이 배당 저점(680원), 2028년부터 성장 예상."
        todo = ("분배금 하향, 비엔씨티 재구조화 없을 때만 중개형 ISA에서 계획 금액의 일부로 분할매수 검토. "
                "2차는 하반기 예상분배금 공시 확인 뒤에 판단.")
        avoid = "한 번에 전액 매수, 신용/미수, 시세 차익 기대(신규 투자 발표 전에는 배당이 주 수익). " + DISCLAIMER
        prio = 3
    else:
        title = f"[MKIF] 088980 종가 {won(close)}, 11,300원 이상 | 신규매수 보류, 배당 매력 낮아짐"
        status = base + " 삼성증권 목표가 11,300원 이상 구간에 새로 들어왔습니다. 컨센서스 목표가는 12,633원."
        todo = "신규매수 보류. 2026년 700원 기준 수익률이 6.2% 근처로 내려와 배당 목적 편입 매력이 낮아졌습니다. 상승 이유(신규 투자 발표 등)를 공시로 확인."
        avoid = "상승 추격 매수, 신용/미수. " + DISCLAIMER
        prio = 3
    lines = [stamp, "이게 뭐 알림: " + cfg["explain"], "지금 결과/상태: " + status, "지금 할 일: " + todo,
             "나중에: " + later, "익절/줄일 때: " + trim, "하지 말 것: " + avoid]
    return title, "\n".join(lines), prio


def run_price(cfg: dict, st: dict, now: datetime, dry: bool, fetch=fetch_closes) -> None:
    try:
        rows = fetch(cfg, now)
    except Exception as exc:  # noqa: BLE001
        print(f"price fetch failed: {exc}")
        today = now.strftime("%Y-%m-%d")
        if st.get("price_fail_date") != today:
            t, m = "[MKIF] 088980 시세 조회 실패 | 매매 신호 아님", f"맥쿼리인프라 일별 시세 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {t}\n{m}")
            else:
                publish(resolve_topic("ops"), t, m, 3)
                st["price_fail_date"] = today
        return
    if not rows:
        print("no completed close")
        return
    last_d, last_c = rows[-1]
    if (now.date() - date.fromisoformat(last_d)).days > int(cfg.get("stale_days", 6)):
        print(f"stale close {last_d}; skip")
        return
    L = cfg["levels"]
    if not st.get("armed"):
        armed, _ = step(L, None, last_c)
        st.update({"armed": armed, "last_date": last_d, "last_close": last_c,
                   "init_note": f"initialized from {last_d} close {last_c:,.0f}, no send"})
        print(f"init from {last_d} close {last_c:,.0f}: {armed} (no send)")
        return
    new = [(d, c) for d, c in rows if d > str(st.get("last_date") or "")]
    if not new:
        print(f"no new close after {st.get('last_date')}; armed={st['armed']}")
        return
    armed, fired_all = dict(st["armed"]), []
    for d, c in new:
        armed, fired = step(L, armed, c)
        print(f"close {d} {c:,.0f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new[-1]
    if fired_all:
        title, msg, prio = price_message(cfg, fired_all, c, d, hold_reasons(cfg, st, now.date()))
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            publish(resolve_topic("port"), title, msg, prio, tags=["chart_with_downwards_trend" if prio >= 4 else "bar_chart"])
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_date": d, "last_close": c})


# ---------- disclosures ----------

def subject_of(item: dict) -> str:
    m = re.search(r"제목\s*(.*?)\s*내용", item.get("text") or "")
    if m:
        return m.group(1).strip()
    return re.sub(r"^\s*맥쿼리한국인프라투융자회사\s*", "", item.get("title") or "").strip()


def parse_amount(text: str) -> int | None:
    m = re.search(r"분배금액\s*\(원\)\s*([\d,]+)", text) or re.search(r"1주당\s*분배금\s*\(원\)\s*([\d,]+)", text) \
        or re.search(r"예상분배금[^.]{0,30}?주당\s*([\d,]+)\s*원", text)
    if not m:
        return None
    v = int(m.group(1).replace(",", ""))
    return v if 50 <= v <= 2000 else None


def parse_period(text: str, filed: str) -> str | None:
    m = re.search(r"분배기준일\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    if len(filed) >= 7:
        y, mo = filed[:4], int(filed[5:7])
        return f"{y}-06-30" if 4 <= mo <= 9 else (f"{y}-12-31" if mo >= 10 else f"{int(y) - 1}-12-31")
    return None


def classify(cfg: dict, item: dict) -> dict | None:
    """-> {kind, prio, ...} or None (ignored)."""
    title = re.sub(r"^\s*맥쿼리한국인프라투융자회사\s*", "", item.get("title") or "")
    text = item.get("text") or ""
    subj = subject_of(item)
    filed = str(item.get("datetime") or "")[:10]
    if any(k in title for k in cfg["dist"]["title_keys"]) and "분배락" not in title:
        amt = parse_amount(text)
        return {"kind": "dist", "amount": amt, "period": parse_period(text, filed), "subject": subj or title}
    if "재무실적" in subj:
        m = re.search(r"분배금으로\s*주당\s*([\d,]+)\s*원", text)
        if m:
            return {"kind": "distconfirm", "amount": int(m.group(1).replace(",", "")), "subject": subj}
        return None
    if any(k in subj for k in cfg["ignore_keys"]):
        return None
    bn = cfg["bnct_keys"]
    if any(k in subj for k in bn) or (any(k in text for k in bn) and re.search(r"재구조화|금리|조건 변경|손상|감액|약정서", subj)):
        return {"kind": "bnct", "subject": subj}
    if any(k in subj for k in cfg["invest_keys"]):
        if subj.startswith("(정정)") or "완료" in subj:
            return None  # corrections / completion notices of an already-alerted raise or bond issue
        return {"kind": "invest", "subject": subj}
    if any(k in subj for k in cfg["material_keys"]):
        return {"kind": "material", "subject": subj}
    return None


def disc_tier(cfg: dict, c: dict, latest: dict | None) -> tuple[int, str, str] | None:
    """(prio, title tail, todo) for one classified notice; None = silent."""
    good, low = float(cfg["dist"]["good_min"]), float(cfg["dist"]["low_max"])
    kind = c["kind"]
    if kind == "distconfirm":
        if latest and latest.get("amount") is not None and int(latest["amount"]) == int(c["amount"]):
            return None
        kind = "dist"
    if kind == "dist":
        a = c.get("amount")
        if a is None:
            return 3, "분배금 공시, 금액 원문 확인", "금액을 자동으로 읽지 못했습니다. 원문에서 주당 분배금을 직접 확인하세요."
        if a >= good:
            return 3, f"예상분배금 {won(a)} | 기준 충족, 2차 분할매수 검토 가능", \
                "350원 이상 기준 충족. 분배금 하향, 비엔씨티 재구조화가 없으면 2차 분할매수 검토 가능."
        if a > low:
            return 3, f"예상분배금 {won(a)} | 기준 사이, 원문 확인", \
                "350원 기준에는 못 미치지만 340원 이하 보류 조건도 아닙니다. 하반기 연간 DPS(700원 전망)와 비교해 판단."
        return 4, f"예상분배금 {won(a)} | 매수 보류 조건", "340원 이하라 매수 보류 조건입니다. 감액 이유와 2027년 680원 전망 변경 여부를 원문에서 확인."
    if kind == "bnct":
        return 4, "비엔씨티 관련 공시 | 추가매수 보류, 원문 확인", "추가매수 보류. 후순위대출 금리, 원금, 손상 여부와 분배금 영향을 원문에서 확인."
    if kind == "invest":
        return 3, "신규 투자 발표 | 금리와 레버리지 원문 확인", "투자 대상, 금액, 후순위대출 금리(10% 안팎인지), 차입 규모와 조달 방법을 원문에서 확인."
    return 3, "중요 공시 | 원문 확인", "투자 자산에 생긴 일(재구조화, 소송, 처분 등)이 분배금에 주는 영향을 원문에서 확인."


def disc_message(cfg: dict, item: dict, c: dict, tier: tuple[int, str, str]) -> tuple[str, str, int]:
    prio, tail, todo = tier
    filed = str(item.get("datetime") or "")[:16].replace("T", " ")
    title = f"[MKIF] 088980 {tail}"
    warn = prio >= 4
    amt = f" 주당 {won(c['amount'])}." if c.get("amount") is not None else ""
    status = f"{filed} 공시: {clean(item.get('title', ''))}. 제목: {clean(c.get('subject', ''))}.{amt} 원문: {item.get('url') or NOTICE_URL}"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | MKIF | 맥쿼리인프라(088980) 공시",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 결과/상태: " + status,
        "지금 할 일: " + todo,
        "나중에: 같은 공시는 다시 오지 않습니다. 종가 선(8,950원, 8,300원, 11,300원)은 따로 계속 봅니다.",
        "익절/줄일 때: 아직 보유 없음. 보유하게 되면 분배금 340원 이하가 이어지거나 비엔씨티 추가 재구조화가 나오면 비중 축소 검토.",
        "하지 말 것: " + ("원문 확인 전 주문, 신용/미수. " if warn else "공시 제목만 보고 바로 주문, 신용/미수. ") + DISCLAIMER,
    ]
    return title, "\n".join(lines), prio


def reminder_message(cfg: dict, r: dict, latest: dict | None) -> tuple[str, str, int]:
    last = f"마지막으로 읽은 분배금 공시: {won(latest['amount'])}({latest.get('filed', '')})." if latest and latest.get("amount") else "읽은 분배금 공시 없음."
    title = f"[MKIF] 088980 {r['label']} 예상분배금 공시 미확인 | 직접 확인"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | MKIF | 맥쿼리인프라(088980)",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {r['due_kst']} KST까지 {r['label']} 예상분배금(금전분배 결의) 공시를 피드에서 찾지 못했습니다. {last}",
        f"지금 할 일: KIND 또는 DART에서 맥쿼리인프라 금전분배 결의 공시를 직접 확인하세요. 원문: {NOTICE_URL}",
        "나중에: 공시가 늦게 잡히면 금액 기준(350원 이상, 340원 이하)으로 따로 옵니다. 이 확인 알림은 한 번만 옵니다.",
        "익절/줄일 때: 아직 보유 없음.",
        "하지 말 것: 금액 확인 전 주문. " + DISCLAIMER,
    ]
    return title, "\n".join(lines), 3


def disc_eval(cfg: dict, st: dict, items: list[dict], now: datetime) -> tuple[list[tuple[str, str, int]], dict]:
    """Pure: -> (messages, state updates)."""
    upd: dict = {}
    seen = set(st.get("seen") or [])
    latest = dict(st.get("dist_latest") or {}) or None
    periods = set(st.get("dist_periods") or [])
    msgs: list[tuple[str, str, int]] = []
    if "seen" not in st:
        for it in sorted(items, key=lambda x: str(x.get("datetime"))):
            c = classify(cfg, it)
            if c and c["kind"] == "dist":
                if c.get("amount") is not None:
                    latest = {"amount": c["amount"], "filed": str(it.get("datetime"))[:10], "id": it["id"], "period": c.get("period")}
                if c.get("period"):
                    periods.add(c["period"])
        upd.update({"seen": sorted({it["id"] for it in items}), "dist_latest": latest, "dist_periods": sorted(periods),
                    "seed_note": f"seeded {len(items)} notices silently at {now:%Y-%m-%d %H:%M} KST"})
        return [], upd
    bnct = None
    for it in sorted([x for x in items if x["id"] not in seen], key=lambda x: str(x.get("datetime"))):
        seen.add(it["id"])
        c = classify(cfg, it)
        print(f"  notice {it['id']} {str(it.get('datetime'))[:16]} {it.get('title')} -> {c['kind'] if c else 'ignore'}")
        if not c:
            continue
        tier = disc_tier(cfg, c, latest)
        if c["kind"] in ("dist", "distconfirm"):
            if c.get("amount") is not None:
                latest = {"amount": c["amount"], "filed": str(it.get("datetime"))[:10], "id": it["id"],
                          "period": c.get("period") or (latest or {}).get("period")}
            if c["kind"] == "dist" and c.get("period"):
                periods.add(c["period"])
        if c["kind"] == "bnct":
            bnct = {"date": now.date().isoformat(), "subject": c["subject"], "id": it["id"]}
        if tier:
            msgs.append(disc_message(cfg, it, c, tier))
    sent_rem = set(st.get("reminders_sent") or [])
    for r in cfg["dist"]["reminders"]:
        due = datetime.strptime(r["due_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if now >= due and r["period"] not in periods and r["period"] not in sent_rem:
            msgs.append(reminder_message(cfg, r, latest))
            sent_rem.add(r["period"])
    upd.update({"seen": sorted(seen), "dist_latest": latest, "dist_periods": sorted(periods), "reminders_sent": sorted(sent_rem)})
    if bnct:
        upd["bnct_flag"] = bnct
    return msgs, upd


def fetch_notices(cfg: dict) -> list[dict]:
    raw = naver_api.get_json(cfg["sources"]["notices"])
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({"id": str(r.get("no") or r.get("bizdateSeq")), "title": str(r.get("title") or "").strip(),
                    "datetime": str(r.get("datetime") or ""), "text": naver_api.html_text(str(r.get("contents") or "")),
                    "url": NOTICE_URL})
    if raw is None or not isinstance(raw, list):
        raise RuntimeError("notice feed not a list")
    return out


def run_disc(cfg: dict, st: dict, now: datetime, dry: bool, fetch=fetch_notices) -> None:
    try:
        items = fetch(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"notice fetch failed: {exc}")
        today = now.strftime("%Y-%m-%d")
        if st.get("notice_fail_date") != today:
            t, m = "[MKIF] 088980 공시 조회 실패 | 매매 신호 아님", f"맥쿼리인프라 공시 피드 조회 실패: {exc}. 다음 19:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {t}\n{m}")
            else:
                publish(resolve_topic("ops"), t, m, 3)
                st["notice_fail_date"] = today
        return
    msgs, upd = disc_eval(cfg, st, items, now)
    if "seed_note" in upd:
        print(upd["seed_note"], "dist_latest", upd.get("dist_latest"))
    for title, msg, prio in sorted(msgs, key=lambda m: -m[2]):
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            publish(resolve_topic("port"), title, msg, prio, tags=["warning" if prio >= 4 else "page_facing_up"])
    if not msgs:
        print("no notice alert")
    st.update(upd)


def confirm_message(cfg: dict, close: float | None, day: str | None, st: dict) -> tuple[str, str]:
    px = f"{won(close)}({day} 종가)" if close is not None else "조회 실패"
    dl = (st.get("dist_latest") or {})
    dtxt = f"최근 예상분배금 공시 {won(dl['amount'])}({dl.get('filed', '')})" if dl.get("amount") else "최근 분배금 공시 미확인"
    lines = [
        "[테스트] MKIF 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: 088980 {px}. {dtxt}. 선: 8,950원 이하 분할매수 검토(3), 8,300원 이하 급락 경고(4, 경고가 우선), 11,300원 이상 신규매수 보류(3).",
        "지금 할 일: 없음. 종가 선은 평일 09:00 KST, 공시는 평일 19:00 KST에 봅니다. 분배금 350원 이상 3, 341~349원 3, 340원 이하 4(보류 조건), 비엔씨티 공시 4, 신규 투자 공시 3.",
        "나중에: 12/20까지 하반기 예상분배금 공시가 안 보이면 한 번 알려 드립니다(2027년 6/20, 12/20도 같음). 2027-12-31까지.",
        "익절/줄일 때: 아직 보유 없음.",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기. " + DISCLAIMER,
    ]
    return "[MKIF] 088980 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- selftest ----------

BUY_OK = ("추가매수 보류", "신규매수 보류", "매수 보류")


def has_buy_wording(text: str) -> bool:
    t = text
    for ok in BUY_OK:
        t = t.replace(ok, "")
    return "매수" in t or "사세요" in t or "담기" in t


REAL_H1_2026 = ("투자회사의 금전분배 결의 1. 투자회사명 맥쿼리한국인프라투융자회사 2. 분배기준일 2026-06-30 3. 분배방법 현금분배 "
                "4. 분배금액(원) 350 - 기준가격 조정 대상 여부 기준가격 10,950 가격제한폭 초과 미해당 5. 분배금총액(백만원) 167,623 "
                "6. 이사회결의일 2026-06-15 7. 기타 - 본 예상분배금은 원본의 반환을 포함하지 않으므로 전액 배당소득세 과세대상에 포함됩니다.")


def selftest() -> int:
    cfg = load_cfg()
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    def clean_ok(t, b):
        return not any(ch in t + b for ch in "\u00b7\u2014")

    A = {"buy8950": True, "warn8300": True, "high11300": True}
    check("init 9450 armed", step(L, None, 9450), (A, []))
    check("init 8800 no send", step(L, None, 8800), ({"buy8950": False, "warn8300": True, "high11300": True}, []))
    check("8951 nothing", step(L, A, 8951)[1], [])
    check("8950 buy (<=)", step(L, A, 8950)[1], ["buy8950"])
    check("8300 gap buy+warn", step(L, A, 8300)[1], ["buy8950", "warn8300"])
    check("11299 nothing", step(L, A, 11299)[1], [])
    check("11300 high (>=)", step(L, A, 11300)[1], ["high11300"])
    walk = [(8900, ["buy8950"]), (8800, []), (8951, []), (8940, ["buy8950"]), (8250, ["warn8300"]), (8200, []),
            (8301, []), (8300, ["warn8300"]), (9500, []), (11400, ["high11300"]), (11500, []), (11299, []), (11300, ["high11300"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, st, c)
        check(f"walk{i} {c}", f, want)

    today = date(2026, 11, 2)
    t, b, p = price_message(cfg, ["buy8950"], 8900, "2026-11-02", [])
    check("buy p3", p, 3)
    check("buy title", t, "[MKIF] 088980 종가 8,900원, 8,950원 이하 | 분할매수 검토 구간")
    check("buy conditional", "분배금 하향, 비엔씨티 재구조화 없을 때만" in b, True)
    check("buy yield 680", "2027년 680원 7.6%" in b, True)
    t, b, p = price_message(cfg, ["buy8950", "warn8300"], 8250, "2026-11-02", [])
    check("warn overrides buy p4", p, 4)
    check("warn title", t, "[MKIF] 088980 종가 8,250원, 8,300원 이하 급락 | 추가매수 보류 후 원인 확인")
    check("warn no buy wording", has_buy_wording(t + b.replace(cfg["explain"], "")) or has_buy_wording(t + b), False)
    t, b, p = price_message(cfg, ["warn8300"], 8250, "2026-11-02", ["최근 분배금 공시 330원(2026-12-11), 340원 이하"])
    check("warn alone no buy", has_buy_wording(t + b), False)
    t, b, p = price_message(cfg, ["high11300"], 11350, "2026-11-02", [])
    check("high title", (p, t), (3, "[MKIF] 088980 종가 11,350원, 11,300원 이상 | 신규매수 보류, 배당 매력 낮아짐"))
    check("high yield 700", "2026년 700원 6.2%" in b, True)
    check("high no buy advice", has_buy_wording(t + b.replace("상승 추격 매수", "")), False)

    # hold-only switch
    s_low = {"dist_latest": {"amount": 330, "filed": "2026-12-11"}}
    s_mid = {"dist_latest": {"amount": 345, "filed": "2026-12-11"}}
    s_bn = {"bnct_flag": {"date": "2026-10-30", "subject": "(주)비엔씨티 후순위대출 약정서 조건 변경"}}
    s_old = {"bnct_flag": {"date": "2025-01-01", "subject": "x"}}
    check("hold reasons low dist", len(hold_reasons(cfg, s_low, today)), 1)
    check("hold reasons 345 none", hold_reasons(cfg, s_mid, today), [])
    check("hold reasons bnct", len(hold_reasons(cfg, s_bn, today)), 1)
    check("bnct flag expires", hold_reasons(cfg, s_old, today), [])
    for name, s in (("low dist", s_low), ("bnct", s_bn)):
        t, b, p = price_message(cfg, ["buy8950"], 8900, "2026-11-02", hold_reasons(cfg, s, today))
        check(f"hold-only {name} title", t, "[MKIF] 088980 종가 8,900원, 8,950원 이하 | 매수 보류 조건 진행 중")
        check(f"hold-only {name} no buy wording", has_buy_wording(t + b), False)

    # run_price with fixtures: init, catch-up, dedupe, override
    now = datetime(2026, 11, 3, 9, 0, tzinfo=KST)
    stp: dict = {}
    run_price(cfg, stp, now, True, fetch=lambda c, n: [("2026-10-30", 9450.0), ("2026-11-02", 9400.0)])
    check("run init no send", (stp["last_date"], stp["armed"]), ("2026-11-02", A))
    sent = []
    import builtins
    orig = builtins.print
    builtins.print = lambda *a, **k: sent.append(" ".join(str(x) for x in a))
    try:
        run_price(cfg, stp, now, True, fetch=lambda c, n: [("2026-11-02", 9400.0), ("2026-11-03", 8200.0)])
    finally:
        builtins.print = orig
    would = [x for x in sent if x.startswith("DRY RUN would send")]
    check("run gap: one message only", len(would), 1)
    check("run gap: warning wins", would[0].startswith("DRY RUN would send (p4): [MKIF] 088980 종가 8,200원, 8,300원 이하 급락"), True)
    sent.clear()
    builtins.print = lambda *a, **k: sent.append(" ".join(str(x) for x in a))
    try:
        run_price(cfg, stp, now, True, fetch=lambda c, n: [("2026-11-02", 9400.0), ("2026-11-03", 8200.0)])
    finally:
        builtins.print = orig
    check("run dedupe same day", [x for x in sent if "would send" in x], [])

    # disclosures
    def N(i, title, text, dt="2026-12-11T06:52:00"):
        return {"id": str(i), "title": "맥쿼리한국인프라투융자회사 " + title, "datetime": dt, "text": text, "url": NOTICE_URL}

    real = N(30729255, "투자회사의 금전분배 결의", REAL_H1_2026, "2026-06-15T06:51:59")
    check("parse real 6/15 H1 notice", parse_amount(REAL_H1_2026), 350)
    check("parse period", classify(cfg, real)["period"], "2026-06-30")

    def dist(amt):
        return N(f"d{amt}", "투자회사의 금전분배 결의", REAL_H1_2026.replace("2026-06-30", "2026-12-31").replace("(원) 350", f"(원) {amt}"))

    cases = [
        (dist(360), 3, "예상분배금 360원 | 기준 충족, 2차 분할매수 검토 가능"),
        (dist(350), 3, "예상분배금 350원 | 기준 충족, 2차 분할매수 검토 가능"),
        (dist(345), 3, "예상분배금 345원 | 기준 사이, 원문 확인"),
        (dist(341), 3, "예상분배금 341원 | 기준 사이, 원문 확인"),
        (dist(340), 4, "예상분배금 340원 | 매수 보류 조건"),
        (dist(320), 4, "예상분배금 320원 | 매수 보류 조건"),
        (N("du", "투자회사의 금전분배 결의", "분배금액 별도 안내"), 3, "분배금 공시, 금액 원문 확인"),
        (N("b1", "기타신고사항", "기타 신고사항 제목 (주)비엔씨티 후순위대출 약정서 조건 변경 내용 (주)비엔씨티는 부산항 신항 ..."), 4,
         "비엔씨티 관련 공시 | 추가매수 보류, 원문 확인"),
        (N("b2", "기타신고사항", "기타 신고사항 제목 부산신항제2배후단부두 사업재구조화 내용 금리 인하"), 4, "비엔씨티 관련 공시 | 추가매수 보류, 원문 확인"),
        (N("b3", "기타신고사항", "기타 신고사항 제목 투자법인 후순위대출 금리 조정 내용 (주)비엔씨티 후순위대출 금리를 6%에서 4%로"), 4,
         "비엔씨티 관련 공시 | 추가매수 보류, 원문 확인"),
        (N("v1", "기타신고사항", "기타 신고사항 제목 잠실 MICE 민간투자사업 신규 투자 결정 내용 ..."), 3, "신규 투자 발표 | 금리와 레버리지 원문 확인"),
        (N("v2", "기타신고사항", "기타 신고사항 제목 5,391억원 한도의 단기사채 인수계약 체결 내용 ..."), 3, "신규 투자 발표 | 금리와 레버리지 원문 확인"),
        (N("v3", "투자회사의 유상증자 결의", "투자회사의 유상증자 결의 ..."), 3, "신규 투자 발표 | 금리와 레버리지 원문 확인"),
        (N("v4", "(정정)투자회사의 유상증자 결의", "(정정)투자회사의 유상증자 결의 ..."), None, None),
        (N("v5", "기타신고사항", "기타 신고사항 제목 회사채 1,000억원 발행 완료 내용 ..."), None, None),
        (N("m1", "기타신고사항", "기타 신고사항 제목 신공항하이웨이(주)의 손실보상금 청구 소송 제기 내용 ..."), 3, "중요 공시 | 원문 확인"),
        (N("m2", "기타신고사항", "기타 신고사항 제목 인천대교(주) 사업재구조화 내용 ..."), 3, "중요 공시 | 원문 확인"),
    ]
    for it, want_p, want_tail in cases:
        c = classify(cfg, it)
        tier = disc_tier(cfg, c, {"amount": 350}) if c else None
        check(f"tier {it['id']}", (tier[0], tier[1]) if tier else None, (want_p, want_tail) if want_p else None)
        if tier:
            t, b, p = disc_message(cfg, it, c, tier)
            check(f"msg {it['id']} prefix/prio", (t.startswith("[MKIF] 088980 "), p), (True, want_p))
            check(f"msg {it['id']} no dot/dash", clean_ok(t, b), True)
            check(f"msg {it['id']} six parts", [x.split(":")[0] for x in b.split("\n")[1:]],
                  ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
            check(f"msg {it['id']} disclaimer", DISCLAIMER in b, True)
            if p >= 4:
                check(f"msg {it['id']} warning no buy wording", has_buy_wording(t + b), False)
    ignored = [
        N("i1", "기타신고사항", "기타 신고사항 제목 2026년 3분기 자산운용보고서 게시 내용 ..."),
        N("i2", "기타신고사항", "기타 신고사항 제목 투자설명서 갱신(비엔씨티 내용 업데이트) 내용 ..."),
        N("i3", "투자회사 분배락 기준가격 안내", "분배락"),
        N("i4", "주주명부폐쇄기간 또는 기준일 설정", "기준일"),
        N("i5", "기타신고사항", "기타 신고사항 제목 2026년 3분기 재무실적 및 투자법인의 운영성과 발표 내용 (주)비엔씨티 후순위대출 약정 조건 변경에 기인"),
        N("i6", "투자회사의 주주총회 결과", "주총"),
        N("i7", "기타신고사항", "기타 신고사항 제목 2026년 3분기 영업보고서 게시 내용"),
    ]
    for it in ignored:
        check(f"ignore {it['id']}", classify(cfg, it), None)
    conf_same = N("c1", "기타신고사항", "기타 신고사항 제목 2026년 재무실적 및 투자법인의 운영성과 발표 내용 1. MKIF 이사회는 금일 2026년 하반기 분배금으로 주당 350원, 총액")
    conf_diff = N("c2", "기타신고사항", conf_same["text"].replace("주당 350원", "주당 330원"))
    check("final == expected silent", disc_tier(cfg, classify(cfg, conf_same), {"amount": 350}), None)
    check("final != expected p4", disc_tier(cfg, classify(cfg, conf_diff), {"amount": 350})[0], 4)

    # disc_eval: seed, dedupe, flags, reminders
    seed_items = [real, N("o1", "기타신고사항", "기타 신고사항 제목 인천대교(주) 사업재구조화 내용", "2025-12-15T06:55:11")]
    msgs, upd = disc_eval(cfg, {}, seed_items, datetime(2026, 10, 29, 19, 0, tzinfo=KST))
    check("seed no send", msgs, [])
    check("seed ids", upd["seen"], ["30729255", "o1"])
    check("seed dist_latest 350", upd["dist_latest"]["amount"], 350)
    s = {**upd}
    msgs, upd = disc_eval(cfg, s, seed_items, datetime(2026, 10, 30, 19, 0, tzinfo=KST))
    check("dedupe no send", msgs, [])
    s.update(upd)
    low = dist(330)
    low["id"] = "n330"
    msgs, upd = disc_eval(cfg, s, seed_items + [low], datetime(2026, 12, 11, 19, 0, tzinfo=KST))
    check("new 330 -> one p4", [m[2] for m in msgs], [4])
    check("dist_latest updated", upd["dist_latest"]["amount"], 330)
    check("period recorded", "2026-12-31" in upd["dist_periods"], True)
    s2 = {**s, **upd}
    check("hold-only after 330", len(hold_reasons(cfg, s2, date(2026, 12, 14))), 1)
    msgs, upd = disc_eval(cfg, s2, seed_items + [low], datetime(2026, 12, 21, 19, 0, tzinfo=KST))
    check("no reminder when H2 notice seen", msgs, [])
    bn = N("nb", "기타신고사항", "기타 신고사항 제목 (주)비엔씨티 사업재구조화 내용 금리 인하")
    msgs, upd = disc_eval(cfg, s, seed_items + [bn], datetime(2026, 11, 20, 19, 0, tzinfo=KST))
    check("bnct p4 and flag", ([m[2] for m in msgs], bool(upd.get("bnct_flag"))), ([4], True))
    # reminders: none before due, one after, never twice
    msgs, upd = disc_eval(cfg, s, seed_items, datetime(2026, 12, 18, 19, 0, tzinfo=KST))
    check("no reminder before 12/20 19:00", msgs, [])
    msgs, upd = disc_eval(cfg, s, seed_items, datetime(2026, 12, 21, 19, 0, tzinfo=KST))
    check("H2 reminder once", [(m[0], m[2]) for m in msgs], [("[MKIF] 088980 2026년 하반기 예상분배금 공시 미확인 | 직접 확인", 3)])
    check("reminder clean", all(clean_ok(m[0], m[1]) for m in msgs), True)
    s3 = {**s, **upd}
    msgs, upd = disc_eval(cfg, s3, seed_items, datetime(2026, 12, 22, 19, 0, tzinfo=KST))
    check("H2 reminder not repeated", msgs, [])
    s3.update(upd)
    msgs, upd = disc_eval(cfg, s3, seed_items, datetime(2027, 6, 21, 19, 0, tzinfo=KST))
    check("H1 2027 reminder", [m[0] for m in msgs], ["[MKIF] 088980 2027년 상반기 예상분배금 공시 미확인 | 직접 확인"])

    # end date via ntfy_util
    import ntfy_util
    check("ends after 2027-12-31 23:59 (+59m grace)", ntfy_util._ended("mkif", cfg, datetime(2028, 1, 1, 1, 0, tzinfo=KST))[0], True)
    check("active before end", ntfy_util._ended("mkif", cfg, datetime(2027, 12, 31, 23, 0, tzinfo=KST))[0], False)
    t, b = confirm_message(cfg, 9450, "2026-10-29", {"dist_latest": {"amount": 350, "filed": "2026-06-15"}})
    check("confirm title", t, "[MKIF] 088980 설치 확인 | 매매 신호 아님")
    check("confirm clean", clean_ok(t, b), True)
    check("clean() strips dot/dash", clean("가\u00b7나\u2014다"), "가, 나-다")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main(argv: list[str]) -> int:
    mode = argv[0].lower() if argv else "price"
    if mode == "selftest":
        return selftest()
    cfg = load_cfg()
    now = datetime.now(KST)
    if mode == "confirm":
        st = load_state()
        try:
            rows = fetch_closes(cfg, now)
            d, c = rows[-1]
        except Exception as exc:  # noqa: BLE001
            print(f"price fetch failed: {exc}")
            d, c = None, None
        t, m = confirm_message(cfg, c, d, st)
        print(t)
        print(m)
        publish(resolve_topic("port"), t, m, 3, tags=["white_check_mark"])
        return 0
    run = (argv[1].lower() if len(argv) > 1 else "auto")
    dry = run == "dry"
    print(f"{now.isoformat(timespec='seconds')} mkif {mode} run={run}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > end + timedelta(minutes=59):
        print(f"ended {cfg['ends_kst']}; silent")
        return 0
    st = load_state()
    if mode == "price":
        run_price(cfg, st, now, dry)
    elif mode == "disc":
        run_disc(cfg, st, now, dry)
    else:
        print(f"unknown mode {mode}")
        return 2
    save_state(st, dry, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
