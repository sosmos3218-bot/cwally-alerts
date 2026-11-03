#!/usr/bin/env python3
"""Alux (475580) watch: Q3 verdict, coastal recon order, CB and financing notices, 7,600 close breakout.

Runs inside the weekday morning slot (no new cron). Modes:
  event   change-only (weekdays except Monday)
  weekly  event checks plus one status message (Monday; at most once per ISO week, state weekly_sent_week)
  dry     print what would be sent, never publish, never write state
Anything else is treated like event.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import naver_api  # noqa: E402
import ntfy_util  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "alux.json"
STATE_PATH = ROOT / "state" / "alux_seen.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
WR_BASE = "https://navercomp.wisereport.co.kr/v2/company/"
TOPIC = "cwally-port"
OPS = "cwally-ops"


# ---------------------------------------------------------------- helpers
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_won(x) -> str:
    return "없음" if x is None else f"{x:,.0f}원"


def fmt_eok(x, digits: int = 1) -> str:
    return "없음" if x is None else f"{x:,.{digits}f}억"


def body_lines(now: str, what: str, status: str, todo: str, later: str, reduce: str, avoid: str) -> str:
    # ntfy copy style order. The end line is appended by ntfy_util.publish().
    return "\n".join(
        [
            f"{now} | ALUX | 에이럭스 475580",
            f"이게 뭐 알림: {what}",
            f"지금 결과: {status}",
            f"지금 할 일: {todo}",
            f"나중에: {later}",
            f"줄일 때: {reduce}",
            f"하지 말 것: {avoid}",
        ]
    )


# ---------------------------------------------------------------- price
def completed_closes(code: str, now: datetime, size: int = 30) -> list[dict]:
    """Newest first. Today's row counts only after 15:40 KST. Regular-session close and volume."""
    import krx_close
    bars = list(reversed(krx_close.completed(krx_close.regular_bars(code, size), now, (15, 40))))
    return [{"day": b.day.replace("-", ""), "close": b.close, "volume": b.volume} for b in bars]


def volume_ratio(rows: list[dict]) -> float | None:
    if len(rows) < 6 or not rows[0].get("volume"):
        return None
    prev = [r["volume"] for r in rows[1:21] if r.get("volume")]
    if len(prev) < 5:
        return None
    avg = sum(prev) / len(prev)
    return rows[0]["volume"] / avg if avg else None


# ---------------------------------------------------------------- Q3 numbers
def _http_text(url: str, referer: str | None = None) -> str:
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _cell(row: dict, idx: int):
    v = row.get(f"DATA{idx + 1}")
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _clean_name(name: str) -> str:
    return re.sub(r"^[\s.*]+", "", str(name or "")).strip()


def wisereport_quarter(code: str, period: str) -> dict | None:
    """Confirmed (non-estimate) consolidated quarter column, in eok won. None if that column is not posted yet."""
    page = f"{WR_BASE}c1010001.aspx?cmp_cd={code}"
    html = _http_text(page)
    m = re.search(r"encparam\s*:\s*'([^']+)'", html)
    if not m:
        raise RuntimeError("wisereport encparam not found")
    enc = m.group(1)
    out: dict = {}
    for rpt in (0, 1):  # 0 income statement, 1 balance sheet
        url = f"{WR_BASE}cF3002.aspx?cmp_cd={code}&frq=1&rpt={rpt}&finGubun=MAIN&frqTyp=1&cn=&encparam={enc}"
        data = json.loads(_http_text(url, referer=page))
        idx = None
        for i, head in enumerate(data.get("YYMM") or []):
            first = re.split(r"<br", str(head), flags=re.I)[0].strip()
            if first.replace("(E)", "") == period and "(E)" not in first:
                idx = i
                break
        if idx is None:
            return None
        rows = {}
        for r in data.get("DATA") or []:
            rows.setdefault(_clean_name(r.get("ACC_NM")), r)
        if rpt == 0:
            rev_row = next((r for n, r in rows.items() if n.startswith("매출액")), None)
            out["rev"] = _cell(rev_row, idx) if rev_row else None
            out["gross"] = _cell(rows["매출총이익"], idx) if "매출총이익" in rows else None
            out["op"] = _cell(rows["영업이익"], idx) if "영업이익" in rows else None
        else:
            out["cash"] = _cell(rows["현금및현금성자산"], idx) if "현금및현금성자산" in rows else None
            out["short_fin"] = _cell(rows["단기금융자산"], idx) if "단기금융자산" in rows else None
            out["fvpl"] = _cell(rows["당기손익-공정가치측정금융자산"], idx) if "당기손익-공정가치측정금융자산" in rows else None
    return out if out.get("rev") is not None else None


def naver_quarter(code: str, key: str) -> dict | None:
    raw = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")
    info = (raw or {}).get("financeInfo") or {}
    titles = {t.get("key"): t for t in info.get("trTitleList") or []}
    t = titles.get(key)
    if not t or t.get("isConsensus") != "N":
        return None
    vals = {}
    for r in info.get("rowList") or []:
        vals[r.get("title")] = ((r.get("columns") or {}).get(key) or {}).get("value")
    rev, op = naver_api.num(vals.get("매출액")), naver_api.num(vals.get("영업이익"))
    return {"rev": rev, "op": op} if rev is not None else None


def judge_q3(q: dict, nums: dict) -> dict:
    gpm = (nums["gross"] / nums["rev"] * 100.0) if nums.get("gross") is not None and nums.get("rev") else None
    op = nums.get("op")
    cash = None
    if nums.get("cash") is not None:
        cash = nums["cash"] + (nums.get("short_fin") or 0.0) + (nums.get("fvpl") or 0.0)
    ok_g = None if gpm is None else gpm >= q["gpm_min"]
    ok_o = None if op is None else op >= q["op_min_eok"]
    ok_c = None if cash is None else cash >= q["cash_min_eok"]
    drop = (op is not None and op < q["op_drop_eok"]) or (cash is not None and cash < q["cash_min_eok"])
    known = [ok_g, ok_o, ok_c]
    if drop:
        verdict = "DROP"
    elif None in known:
        verdict = "MISSING"
    elif all(known):
        verdict = "UP"
    else:
        verdict = "HOLD"
    return {"verdict": verdict, "gpm": gpm, "op": op, "cash": cash, "ok": [ok_g, ok_o, ok_c], "rev": nums.get("rev")}


def mark(ok) -> str:
    return "확인 못 함" if ok is None else ("충족" if ok else "미달")


def q3_message(q: dict, j: dict, close: float | None, now_s: str) -> tuple[str, str, int, str]:
    gpm_txt = "없음" if j["gpm"] is None else "%.1f%%" % j["gpm"]
    status = (
        f"3분기 매출 {fmt_eok(j['rev'])}. "
        f"매출총이익률 {gpm_txt} (기준 {q['gpm_min']:.0f}% 이상, {mark(j['ok'][0])}). "
        f"영업이익 {fmt_eok(j['op'])} (기준 {q['op_min_eok']:.0f}억 이상, {mark(j['ok'][1])}). "
        f"현금성자산 {fmt_eok(j['cash'], 0)} (기준 {q['cash_min_eok']:.0f}억 이상, {mark(j['ok'][2])}). "
        "숫자는 WiseReport/Naver 연결 값이라 회사 공시와 다르면 공시가 우선입니다."
    )
    cap = q["entry_price_cap"]
    price_note = f" 지금 종가는 {fmt_won(close)}입니다." if close is not None else ""
    what = "에이럭스 3분기 실적이 확정되어 중립 상향 기준 세 가지로 자동 판정했습니다."
    if j["verdict"] == "UP":
        title = "[ALUX] 475580 3Q 중립 상향 | 계획의 50%만, 지정가"
        todo = f"계획 금액의 50%만 {cap:,}원 이하 지정가로 사세요.{price_note} {cap:,}원 위면 사지 마세요."
        later = "나머지 50%는 CB 해법 확정 또는 해안정찰 수주를 확인하고 종가 7,600원 돌파(거래량 2배)가 나온 뒤에만 삽니다."
        reduce = "산 뒤 종가가 진입가 아래로 8% 빠지거나 7,000원을 이탈하면 정리합니다. 7,500~7,750원에서 절반 익절."
        avoid = f"{cap:,}원 위 추격매수, 신용과 미수, 한 번에 전액 진입."
        pri, tags = 4, "chart_with_upwards_trend"
    elif j["verdict"] == "DROP":
        title = "[ALUX] 475580 3Q 탈락 | 검토 중단"
        todo = "새로 사지 마세요. 영업적자 -25억 초과 또는 현금성자산 150억 미만이라 신규 진입 검토를 멈춥니다."
        later = "다음 판단은 2027-03 사업보고서입니다. 그 전에는 CB 해법이나 큰 수주 공시가 나올 때만 다시 봅니다."
        reduce = "이미 들고 있다면 종가 5,600원 이탈 때 정리하는 기존 기준을 지키세요."
        avoid = "물타기, 신용과 미수, 반등 추격."
        pri, tags = 4, "warning"
    else:
        title = "[ALUX] 475580 3Q 관망 유지 | 아직 사지 마세요"
        todo = "아직 사지 마세요. 세 기준 중 일부가 미달이지만 탈락선은 넘지 않았습니다."
        later = "다음 판단은 2027-03 사업보고서입니다. 그 전에는 CB 해법이나 큰 수주 공시가 나올 때 다시 봅니다."
        reduce = "이미 들고 있다면 종가 5,600원 이탈 때 정리하는 기존 기준을 지키세요."
        avoid = "실적 일부만 보고 추격매수, 신용과 미수."
        pri, tags = 3, "eyes"
    return title, body_lines(now_s, what, status, todo, later, reduce, avoid), pri, tags


# ---------------------------------------------------------------- notices
def classify(n: dict, rules: dict) -> str | None:
    title = n["title"]
    if any(w in title for w in rules["title_exclude"]):
        return None
    text = title + " " + (n.get("text") or "")[:4000]
    coastal_title = any(w in title for w in rules["coastal_words"])
    coastal_body = any(w in text for w in rules["coastal_words"])
    if coastal_title or (coastal_body and any(w in title for w in rules["contract_words"])):
        return "ORDER"
    if any(w in title for w in rules["financing_words"]):
        return "FIN"
    if any(w in title for w in rules["general_words"]):
        return "GENERAL"
    return None


CLASS_KO = {"ORDER": "해안정찰/방산 수주", "FIN": "CB와 자금조달", "GENERAL": "주요 공시"}


# ---------------------------------------------------------------- main
class Out:
    def __init__(self, dry: bool):
        self.dry = dry
        self.sent: list[str] = []

    def send(self, topic: str, title: str, message: str, priority: int, tags: list[str]) -> bool:
        if self.dry:
            print(f"DRY RUN would send to {topic}: {title}\n{message}\n")
            return True
        try:
            ntfy_util.publish(topic, title, message, priority=priority, tags=tags)
            self.sent.append(title)
            return True
        except Exception as exc:  # state must not advance when the send failed
            print(f"publish failed: {exc}", file=sys.stderr)
            return False


def ops_once(out: Out, state: dict, key: str, title: str, text: str, today: str) -> None:
    sent = state.setdefault("ops_day", {})
    if sent.get(key) == today:
        return
    if out.send(OPS, title, text, 3, ["warning"]):
        sent[key] = today


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    dry = mode == "dry"
    cfg = load_json(CFG_PATH, {})
    if not cfg:
        print("config missing", file=sys.stderr)
        return 1
    state = load_json(STATE_PATH, {})
    now = datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("alux alert ended; silent")
        return 0

    out = Out(dry)
    code = cfg["code"]
    q = cfg["q3"]
    bo = cfg["breakout"]
    rules = cfg["rules"]
    state.setdefault("seen_notices", [])
    state.setdefault("breakout", {"active": False})
    state.setdefault("q3", {"done": False})

    # price
    rows: list[dict] = []
    try:
        rows = completed_closes(code, now)
    except Exception as exc:
        print(f"trend failed: {exc}", file=sys.stderr)
    last = rows[0] if rows else None
    close = last["close"] if last else None
    if last is None:
        ops_once(out, state, "price", "[ALUX] 475580 시세 조회 실패", f"{now_s} 에이럭스 종가를 읽지 못했습니다. 돌파 판단은 건너뛰었습니다. 매매 신호가 아닙니다.", today)

    # 1) Q3 verdict (once)
    q3 = state["q3"]
    if not q3.get("done") and today <= q["active_until"]:
        try:
            wr = None
            try:
                wr = wisereport_quarter(code, q["period"])
            except Exception as exc:
                print(f"wisereport failed: {exc}", file=sys.stderr)
            nq = None
            try:
                nq = naver_quarter(code, q["naver_key"])
            except Exception as exc:
                print(f"naver quarter failed: {exc}", file=sys.stderr)
            if wr or nq:
                nums = dict(wr or {})
                if nums.get("rev") is None and nq:
                    nums["rev"] = nq["rev"]
                if nums.get("op") is None and nq:
                    nums["op"] = nq["op"]
                j = judge_q3(q, nums)
                if j["verdict"] == "MISSING":
                    miss = [n for n, ok in zip(["매출총이익률", "영업이익", "현금성자산"], j["ok"]) if ok is None]
                    ops_once(
                        out, state, "q3",
                        "[ALUX] 475580 3Q 숫자 미확인",
                        f"{now_s} 3분기 확정 열은 보이는데 {', '.join(miss)}을 읽지 못해 판정을 미뤘습니다. 내일 다시 시도합니다. 공시 원문을 직접 확인하세요. 매매 신호가 아닙니다.",
                        today,
                    )
                else:
                    title, msg, pri, tags = q3_message(q, j, close, now_s)
                    if out.send(TOPIC, title, msg, pri, tags.split(",")):
                        q3.update({"done": True, "verdict": j["verdict"], "sent_kst": now_s,
                                   "gpm": j["gpm"], "op": j["op"], "cash": j["cash"], "rev": j["rev"]})
        except Exception as exc:
            print(f"q3 check failed: {exc}", file=sys.stderr)
            ops_once(out, state, "q3err", "[ALUX] 475580 3Q 실적 조회 실패", f"{now_s} 3분기 실적 조회 중 오류가 났습니다. 매매 신호가 아닙니다.", today)

    # 2) notices: coastal order, CB and financing, other key disclosures
    try:
        notices = naver_api.notices(code, 15)
    except Exception as exc:
        notices = []
        print(f"notices failed: {exc}", file=sys.stderr)
        ops_once(out, state, "notice", "[ALUX] 475580 공시 조회 실패", f"{now_s} 에이럭스 공시 목록을 읽지 못했습니다. 매매 신호가 아닙니다.", today)
    seen = list(state["seen_notices"])
    if notices and not seen:
        seen = [n["id"] for n in notices]  # first run: seed silently
        state["seen_notices"] = seen
    new = [n for n in notices if n["id"] not in set(seen)]
    cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    # only notices from the last 14 days can alert; older unseen ones are marked seen silently
    hits = [(classify(n, rules), n) for n in new if str(n.get("datetime") or "")[:10] >= cutoff]
    hits = [(c, n) for c, n in hits if c]
    if hits:
        order = {"ORDER": 0, "FIN": 1, "GENERAL": 2}
        hits.sort(key=lambda x: order[x[0]])
        top = hits[0][0]
        lines = []
        for c, n in hits[:6]:
            lines.append(f"- [{CLASS_KO[c]}] {n['title']}")
        lines.append(hits[0][1]["url"])
        if top == "ORDER":
            title = "[ALUX] 475580 해안정찰/방산 수주 공시 | 원문 확인"
            todo = "공시 원문에서 계약 금액과 기간을 먼저 확인하세요. 아직 사지 마세요."
        elif top == "FIN":
            title = "[ALUX] 475580 CB/자금조달 공시 | 원문 확인"
            todo = "전환가액, 상환 조건, 유상증자 방식을 원문에서 확인하세요. 아직 사지 마세요."
        else:
            title = "[ALUX] 475580 새 공시 | 원문 확인"
            todo = "공시 원문을 확인하세요. 아직 사지 마세요."
        msg = body_lines(
            now_s,
            "에이럭스 새 공시 알림입니다. 해안정찰 수주, CB와 자금조달, 주요 공시를 봅니다.",
            "새 공시 " + str(len(hits)) + "건\n" + "\n".join(lines),
            todo,
            "CB 해법 확정이나 해안정찰 수주가 확인되면 중립 상향 판정과 함께 봅니다.",
            "주식 증자나 고금리 차입으로 CB를 막는 내용이면 신규 진입 검토를 멈춥니다.",
            "제목만 보고 판단, 신용과 미수.",
        )
        pri = 5 if top in {"ORDER", "FIN"} else 4
        if out.send(TOPIC, title, msg, pri, ["warning", "memo"]):
            for n in reversed(new):
                if n["id"] not in seen:
                    seen.append(n["id"])
    else:
        for n in reversed(new):  # nothing matched: mark as seen so it is not rechecked
            seen.append(n["id"])
    state["seen_notices"] = seen[-80:]

    # 3) close breakout (7,600), re-arm below 7,250
    b = state["breakout"]
    if last is not None:
        if close < bo["rearm_below"]:
            b["active"] = False
        elif close >= bo["close"] and not b.get("active"):
            vr = volume_ratio(rows)
            vtxt = "거래량 비교 불가" if vr is None else f"거래량 20일 평균의 {vr:.1f}배"
            vok = vr is not None and vr >= bo["volume_x"]
            verdict = state["q3"].get("verdict")
            if verdict == "UP":
                todo = "3분기 중립 상향이 확인된 상태입니다. " + ("거래량 조건도 맞으니 나머지 50% 진입을 검토하세요." if vok else "다만 거래량 2배가 아직이라 하루 이틀 더 지켜보세요.")
            else:
                todo = "3분기 중립 상향 판정이 없으면 사지 마세요. 가격만 오른 상태입니다."
            msg = body_lines(
                now_s,
                f"에이럭스 종가 {bo['close']:,}원 돌파 알림입니다. 8~10월 박스 상단 매물대를 넘었는지 봅니다.",
                f"{last['day']} 종가 {fmt_won(close)}, {vtxt} (기준 {bo['volume_x']:.0f}배 {'충족' if vok else '미충족'}). 3분기 판정: {verdict or '아직 없음'}.",
                todo,
                "7,250원 위에서 종가가 유지되는지 보세요. 다음 목표는 8,700~9,500원입니다.",
                "종가가 7,250원 아래로 다시 내려오면 돌파 실패로 봅니다.",
                "3분기 판정 전 추격매수, 8,000원 위 급등 추격, 신용과 미수.",
            )
            if out.send(TOPIC, f"[ALUX] 475580 종가 {bo['close']:,} 돌파 | 실적 판정부터", msg, 4, ["rocket"]):
                b["active"] = True
                b["sent_day"] = last["day"]

    # 4) Monday status line
    week = now.strftime("%G-W%V")
    if mode == "weekly" and state.get("weekly_sent_week") == week:
        print(f"weekly already sent {week}; skip status")
    elif mode == "weekly":
        v = state["q3"].get("verdict")
        gap = "" if close is None else f" (7,600까지 {bo['close'] - close:+,.0f}원)"
        q3txt = f"판정 {v}" if v else "3분기 실적 발표 전"
        msg = body_lines(
            now_s,
            "에이럭스 주간 상태 요약입니다. 매주 월요일에 한 번만 옵니다.",
            f"종가 {fmt_won(close)}{gap}. 3분기: {q3txt}. 돌파 알림 {'발송됨' if b.get('active') else '대기'}.",
            "관망입니다. 3분기 판정이 나오기 전에는 사지 않습니다.",
            "3분기 실적 발표는 11월 중순으로 추정합니다. 해안정찰 결과는 연말 예정입니다.",
            "이미 들고 있다면 종가 5,600원 이탈 때 정리합니다.",
            "추격매수, 신용과 미수.",
        )
        if out.send(TOPIC, "[ALUX] 475580 주간 체크 | 관망 유지" if not v else f"[ALUX] 475580 주간 체크 | 3Q {v}", msg, 3, ["calendar"]):
            state["weekly_sent_week"] = week

    if close is not None:
        state["price"] = close
        state["close_day"] = last["day"]
    state["updated_at"] = now_s
    state.pop("zone", None)
    if dry:
        print("DRY RUN: state not written")
        return 0
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
