#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sec.json"
STATE_PATH = ROOT / "state" / "sec.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

ZONE_KO = {
    "REDUCE": "20만미만",
    "BUY1": "20~24만",
    "BUY2": "24~28만",
    "WATCH": "28~29만/32~33만/38~41만/45~46만",
    "HOLD": "29~32만",
    "SELL1": "33~38만",
    "HOLD38": "38~41만",
    "NONEW": "41~45만",
    "SELL2": "46~50만",
    "EXIT": "50만위",
    "BELOW": "20만미만",
    "DATA_FAIL": "시세 조회 실패",
}
ACTION = {
    "REDUCE": "감액",
    "BUY1": "진입",
    "BUY2": "진입",
    "WATCH": "관찰",
    "HOLD": "보유",
    "SELL1": "익절",
    "HOLD38": "관찰",
    "NONEW": "관찰",
    "SELL2": "익절",
    "EXIT": "익절",
    "BELOW": "관찰",
    "DATA_FAIL": "관찰",
}
DO_NOW = {
    "REDUCE": "신규 금지. 비중을 줄이세요.",
    "BUY1": "1차 30~40%만. LTA파기나 가이던스 하향이면 쉬세요.",
    "BUY2": "2차 20~30%만. 올인 금지.",
    "WATCH": "추격 금지. 구간 선만 보세요.",
    "HOLD": "보유. 29만 위 비중 증가 금지.",
    "SELL1": "1차 20~30% 익절.",
    "HOLD38": "38만 재이탈하면 감액. 신규 금지.",
    "NONEW": "신규 없음. 38만 재이탈시 감액.",
    "SELL2": "2차 익절.",
    "EXIT": "50만 위 정리.",
    "BELOW": "20만 미만 감액 조건은 꺼져 있습니다. 다른 구간 선만 보세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
WATCH_LINE = {
    "REDUCE": "20만",
    "BUY1": "20만/24만",
    "BUY2": "24만/28만",
    "WATCH": "29만",
    "HOLD": "29만/32만",
    "SELL1": "33만/38만",
    "HOLD38": "38만",
    "NONEW": "38만/41만",
    "SELL2": "46만",
    "EXIT": "50만",
    "BELOW": "20만",
    "DATA_FAIL": "시세",
}

EARN_SOURCES = [
    "https://stock.naver.com/api/domestic/detail/notice?itemCode=005930&page=1&pageSize=20",
    "https://m.stock.naver.com/api/news/stock/005930?pageSize=20&page=1",
    "https://www.samsung.com/sec/ir/financial-information/earnings-release/",
    "https://news.samsung.com/kr/?s=%EC%98%81%EC%97%85%EC%9D%B4%EC%9D%B5",
]


def yahoo_close(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        res = payload["chart"]["result"][0]
        meta = res.get("meta", {})
        quote = (res.get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        last = None
        for val in reversed(closes):
            if val is not None:
                last = float(val)
                break
        price = last or meta.get("previousClose") or meta.get("regularMarketPrice")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def _reduce_cut(bands):
    """Legacy sub-200k REDUCE. Inactive unless bands['reduce_enabled'] is true.

    reduce_lt stays in the config as a record. With the flag off, a price under
    buy1_lo (200000) is BELOW: no 감액 and no 20만하회. Other bands are untouched.
    """
    bands = bands or {}
    if not bands.get("reduce_enabled"):
        return None
    raw = bands.get("reduce_lt")
    if raw is None:
        return None
    return float(raw)


def zone(price, bands=None):
    if price is None:
        return "DATA_FAIL"
    cut = _reduce_cut(bands)
    if cut is not None and price < cut:
        return "REDUCE"
    if price < 200000:
        return "BELOW"
    if price < 240000:
        return "BUY1"
    if price < 280000:
        return "BUY2"
    if price < 290000:
        return "WATCH"
    if price < 320000:
        return "HOLD"
    if price < 330000:
        return "WATCH"
    if price < 380000:
        return "SELL1"
    if price < 410000:
        return "HOLD38"
    if price < 450000:
        return "NONEW"
    if price < 460000:
        return "WATCH"
    if price < 500000:
        return "SELL2"
    return "EXIT"


def zone_label(z, price):
    if price is None:
        return ZONE_KO[z]
    if z == "REDUCE":
        return "20만미만"
    if z == "BELOW":
        return "20만미만"
    if z == "BUY1":
        return "20~24만"
    if z == "BUY2":
        return "24~28만"
    if z == "HOLD":
        return "29~32만"
    if z == "SELL1":
        return "33~38만"
    if z == "HOLD38":
        return "38~41만"
    if z == "NONEW":
        return "41~45만"
    if z == "SELL2":
        return "46~50만"
    if z == "EXIT":
        return "50만위"
    if 280000 <= price < 290000:
        return "28~29만"
    if 320000 <= price < 330000:
        return "32~33만"
    if 450000 <= price < 460000:
        return "45~46만"
    return ZONE_KO[z]


def warnings(price, prev_price, bands=None):
    out = []
    if price is None:
        return out
    cut = _reduce_cut(bands)
    if cut is not None and price < cut:
        out.append("20만하회")
    if price >= 290000:
        out.append("29만돌파(종가)")
    if price >= 330000:
        out.append("33만감액")
    if prev_price is not None and prev_price >= 380000 and price < 380000:
        out.append("38만재이탈")
    if price >= 410000:
        out.append("41만재평가")
    if price >= 460000:
        out.append("46만익절")
    if price >= 500000:
        out.append("50만과열")
    return out


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="ignore")
    if text.lstrip()[:1] in "[{":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import naver_api

            return naver_api.flatten_text(json.loads(text))
        except (ValueError, ImportError):
            pass
    return text


def to_jo(num: float, unit: str) -> float:
    unit = unit.replace(" ", "")
    if "조" in unit:
        return num
    if "억" in unit:
        return num / 10000.0
    if "만억" in unit:
        return num / 10000.0
    return num


def parse_op_eps(text: str) -> dict:
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&nbsp;|&#\d+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    out = {"op": None, "eps": None, "q4_op": None, "q4_note": "", "source_hit": "", "published": False}
    if not re.search(r"삼성전자|005930", clean):
        pass
    earn_hit = re.search(
        r"(3분기|3Q|잠정실적|영업이익).{0,80}(조\s*원|억원)",
        clean,
    )
    if earn_hit:
        out["published"] = True
        out["source_hit"] = earn_hit.group(0)[:180]
    op_pats = [
        r"(?:3분기|3Q|잠정).{0,40}영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
        r"영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?).{0,20}(?:3분기|3Q)",
        r"연결기준\s*영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
    ]
    for pat in op_pats:
        m = re.search(pat, clean, re.I)
        if m:
            val = to_jo(float(m.group(1)), m.group(2))
            if 20 <= val <= 250:
                out["op"] = val
                out["published"] = True
                out["source_hit"] = out["source_hit"] or m.group(0)[:180]
                break
    eps_pats = [
        r"(?:3분기|3Q).{0,40}(?:주당순이익|EPS)[^0-9]{0,12}(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원",
        r"(?:주당순이익|EPS)[^0-9]{0,12}(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원.{0,20}(?:3분기|3Q)",
    ]
    for pat in eps_pats:
        m = re.search(pat, clean, re.I)
        if m:
            raw = m.group(1).replace(",", "")
            val = float(raw)
            if 3000 <= val <= 40000:
                out["eps"] = val
                out["published"] = True
                break
    q4 = re.search(
        r"(?:4분기|4Q|사분기).{0,40}영업이익[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(조\s*원?|억\s*원?)",
        clean,
        re.I,
    )
    if q4:
        val = to_jo(float(q4.group(1)), q4.group(2))
        if 20 <= val <= 300:
            out["q4_op"] = val
            out["q4_note"] = q4.group(0)[:120]
    weak = re.search(r"(가이던스\s*하향|수요\s*둔화|실적\s*피크|전망\s*하향|4분기.{0,20}감소)", clean)
    strong = re.search(r"(공급\s*부족\s*지속|수급\s*타이트|가격\s*상승\s*지속|4분기.{0,20}개선)", clean)
    if weak and not out["q4_note"]:
        out["q4_note"] = "약세 코멘트"
    elif strong and not out["q4_note"]:
        out["q4_note"] = "강세 코멘트"
    return out


def collect_earnings() -> dict:
    merged = {"op": None, "eps": None, "q4_op": None, "q4_note": "", "source": "", "published": False, "fetch_ok": False}
    for url in EARN_SOURCES:
        try:
            text = fetch_text(url)
            merged["fetch_ok"] = True
        except Exception as exc:
            print(f"earn fetch fail {url}: {exc}", file=sys.stderr)
            continue
        parsed = parse_op_eps(text)
        print(f"earn parse {url} op={parsed['op']} eps={parsed['eps']} pub={parsed['published']}")
        if parsed["op"] and merged["op"] is None:
            merged["op"] = parsed["op"]
            merged["source"] = url
        if parsed["eps"] and merged["eps"] is None:
            merged["eps"] = parsed["eps"]
            merged["source"] = merged["source"] or url
        if parsed["q4_op"] and merged["q4_op"] is None:
            merged["q4_op"] = parsed["q4_op"]
        if parsed["q4_note"] and not merged["q4_note"]:
            merged["q4_note"] = parsed["q4_note"]
        if parsed["published"]:
            merged["published"] = True
            merged["source"] = merged["source"] or url
        if merged["op"] is not None:
            break
    return merged


def verdict(cfg_e: dict, got: dict) -> str:
    op, eps, q4 = got.get("op"), got.get("eps"), got.get("q4_op")
    note = got.get("q4_note") or ""
    if op is None and eps is None:
        return "PENDING"
    break_op = op is not None and op <= cfg_e["op_break"]
    break_eps = eps is not None and eps <= cfg_e["eps_break"]
    cut_op = op is not None and op < cfg_e["op_cons"]
    cut_eps = eps is not None and eps < cfg_e["eps_cons"]
    q4_weak = (q4 is not None and q4 < cfg_e["q4_op_floor"]) or ("약세" in note) or ("하향" in note)
    up_op = op is not None and op >= cfg_e["op_aug"]
    up_eps = eps is None or eps >= cfg_e["eps_aug"]
    if break_op or break_eps or (q4 is not None and q4 < cfg_e["q4_op_floor"]):
        return "CUT"
    if cut_op or cut_eps or q4_weak:
        return "CUT"
    if up_op and up_eps and not q4_weak:
        return "UP"
    return "HOLD"


def earn_copy(ver: str, got: dict, cfg_e: dict) -> tuple[str, list[str], int]:
    op = got.get("op")
    eps = got.get("eps")
    q4 = got.get("q4_op")
    op_txt = f"{op:.1f}조" if op is not None else "없음"
    eps_txt = f"{eps:,.0f}원" if eps is not None else "없음"
    q4_txt = f"{q4:.1f}조" if q4 is not None else (got.get("q4_note") or "숫자 없음")
    if ver == "CUT":
        title = f"[SEC] 005930 연간치 하향 | 3Q {op_txt}"
        now_do = "연간 추정치가 내려갈 구간입니다. 29만 위 비중 증가 금지."
        later = "4분기 코멘트와 메모리 가격을 보고 다음 구간을 정하세요."
        pri = 5
    elif ver == "UP":
        title = f"[SEC] 005930 연간치 상향 | 3Q {op_txt}"
        now_do = "연간치는 올라갈 수 있습니다. 그래도 29만 위 추격은 금지."
        later = "4분기가 120조를 지키는지 한 번 더 보세요."
        pri = 4
    else:
        title = f"[SEC] 005930 연간치 유지 | 3Q {op_txt}"
        now_do = "연간치는 유지로 봅니다. 구간 규칙은 그대로입니다."
        later = "4분기 가이던스가 120조 아래로 내려오면 하향으로 바꿉니다."
        pri = 4
    lines = [
        f"이게 뭐 알림: 삼성전자 3분기 실적을 9월 21일 임계값과 대조한 연간치 판정입니다. 투자 권유 아닙니다.",
        f"지금 결과: {ver} | 3Q 영업이익 {op_txt} / EPS {eps_txt} | 4Q {q4_txt}",
        f"임계값: 유지 111조·14,000원, 하향시작 그 미만, 확산 105조·13,300원, 붕괴 100조·13,000원 또는 4Q 120조 아래.",
        f"지금 할 일: {now_do}",
        f"나중에: {later}",
        "익절/줄일 때: 33~38만 1차 20~30%, 46~50만 2차, 50만 위 정리.",
        "하지 말 것: 실적 다음날 추격, 신용/미수, 숫자 없이 연간치 상향으로 단정.",
    ]
    if got.get("source"):
        lines.append(f"출처: {got['source']}")
    return title, lines, pri


def run_earnings(cfg: dict, now: datetime, state: dict) -> dict:
    earn = cfg.get("earnings") or {}
    today = now.strftime("%Y-%m-%d")
    year = now.year
    if year != int(earn.get("year_guard", 2026)):
        print("sec earn year-guard skip")
        return state
    if today < earn.get("window_start", "2099-01-01") or today > earn.get("window_end", "2000-01-01"):
        print("sec earn outside window; silent")
        return state
    if state.get("earn_final_notified"):
        print("sec earn already final; skip")
        return state

    got = collect_earnings()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if not got.get("fetch_ok"):
        if not state.get("earn_fail_notified"):
            title = "[SEC] 005930 실적 조회 실패 | 판정 보류"
            msg = "\n".join(
                [
                    f"{stamp} | SEC | 삼성전자 005930",
                    "이게 뭐 알림: 3분기 실적 조회가 실패했습니다. 실패를 정상으로 처리하지 않습니다.",
                    "지금 결과: 조회 실패. 연간치 판정 없음.",
                    "지금 할 일: 공시와 IR을 직접 확인하세요.",
                    "나중에: 다음 장전 슬롯에서 다시 봅니다.",
                    "익절/줄일 때: 기존 구간 규칙 유지.",
                    "하지 말 것: 조회 실패를 호재/악재로 해석.",
                ]
            )
            ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 4, "warning")
            state["earn_fail_notified"] = True
        state["earn_status"] = "FETCH_FAIL"
        return state

    ver = verdict(earn, got)
    state["earn_op"] = got.get("op")
    state["earn_eps"] = got.get("eps")
    state["earn_q4_op"] = got.get("q4_op")
    state["earn_source"] = got.get("source")
    state["earn_status"] = ver
    state["earn_updated_at"] = stamp

    if ver == "PENDING":
        if today >= earn.get("expected_date", today) and not state.get("earn_pending_notified"):
            title = "[SEC] 005930 실적 아직 없음 | 판정 보류"
            msg = "\n".join(
                [
                    f"{stamp} | SEC | 삼성전자 005930",
                    "이게 뭐 알림: 3분기 영업이익/EPS 숫자가 아직 안 잡혔습니다.",
                    "지금 결과: 미발표 또는 파싱 실패. 연간치 판정 없음.",
                    "지금 할 일: 공시가 나온 뒤에만 보세요. 추격 금지.",
                    "나중에: 창 안(11/3까지) 장전 슬롯에서 다시 봅니다.",
                    "익절/줄일 때: 기존 구간 규칙 유지.",
                    "하지 말 것: 없는 숫자를 맞춰 넣기.",
                ]
            )
            ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, 3, "hourglass")
            state["earn_pending_notified"] = True
        else:
            print("sec earn pending; silent")
        return state

    title, body, pri = earn_copy(ver, got, earn)
    lines = [f"{stamp} | SEC | 삼성전자 005930", *body]
    ntfy_publish("https://ntfy.sh", "cwally-port", title, "\n".join(lines), pri, "chart_with_upwards_trend")
    state["earn_final_notified"] = True
    state["earn_pending_notified"] = True
    return state


def alert_title(z, price) -> str:
    act = ACTION[z]
    man = None if price is None else round(price / 10000, 2)
    man_txt = f"{man}만" if man is not None else "시세없음"
    return f"[SEC] 005930 {act} {man_txt} {zone_label(z, price)}"


def alert_priority(z, warn) -> int:
    warn = warn or []
    if z in {"REDUCE", "EXIT"} or "20만하회" in warn or "50만과열" in warn:
        return 5
    if warn or z in {"SELL1", "SELL2"}:
        return 4
    return 3


def price_event(mode, z, warn, prev_zone, prev_warn) -> bool:
    """True when the price alert has something new to say. Earnings are separate.

    BELOW is the disabled sub-200k REDUCE band: no weekly status and no zone-change
    send. A different warning (38만재이탈 and the rest) can still send.
    """
    warn = warn or []
    prev_warn = prev_warn or []
    if z == "BELOW":
        other = [w for w in warn if w != "20만하회"]
        prev_other = [w for w in prev_warn if w != "20만하회"]
        return bool(other) and other != prev_other
    if z == "DATA_FAIL":
        return True
    if mode == "weekly":
        return True
    zone_changed = bool(prev_zone) and prev_zone != z
    return bool(zone_changed or (warn and warn != prev_warn))


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "auto":
        mode = "weekly" if now.weekday() == 0 else "event"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "price": None,
        "action": None,
        "last_alert_date": None,
        "warn": [],
        "earn_final_notified": False,
        "earn_pending_notified": False,
        "earn_fail_notified": False,
        "earn_status": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_close(cfg["ticker"])
    bands = cfg.get("bands") or {}
    z = zone(price, bands)
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
    act = ACTION[z]
    zname = zone_label(z, price)
    warn = warnings(price, prev.get("price"), bands)
    warn_txt = ", ".join(warn) if warn else "경고 없음."
    man = None if price is None else round(price / 10000, 2)
    man_txt = f"{man}만" if man is not None else "시세없음"
    line1 = f"{act} - {man_txt}, {zname}."
    line2 = f"오늘 장중 {WATCH_LINE[z]} 선을 먼저 보고 {DO_NOW[z]}"

    today = now.strftime("%Y-%m-%d")
    already = prev.get("last_alert_date") == today
    event = price_event(mode, z, warn, prev.get("zone"), prev.get("warn") or [])
    send = event and not already
    if not event:
        print("no sec event; silent")

    display = cfg.get("display", "삼성전자")
    code = cfg.get("code", "005930")
    lines = [
        f"{stamp} | SEC | {display} {code}",
        "이게 뭐 알림: 삼성전자 주간 구간 감시입니다. 투자 권유 아닙니다.",
        f"지금 상태: {line1}",
        f"지금 할 일: {line2}",
        "나중에: 29만 위에서 비중을 늘리지 마세요. LTA, 2027 OP, 환원 숫자는 확인될 때만 하방으로 봅니다.",
        "익절/줄일 때: 33~38만 1차 20~30%, 46~50만 2차, 50만 위 정리. 38만 재이탈시 감액.",
        "하지 말 것: 20만 미만 신규, 29만 위 추격/비중증가, 올인, 신용/미수.",
        f"경고: {warn_txt}",
    ]
    msg = "\n".join(lines)
    print(msg)
    if send:
        title = alert_title(z, price)
        pri = alert_priority(z, warn)
        ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, pri, "chart_with_upwards_trend")

    prev = run_earnings(cfg, now, prev)
    prev.update(
        {
            "updated_at": stamp,
            "zone": z,
            "price": price,
            "action": act,
            "last_alert_date": today if send else prev.get("last_alert_date"),
            "warn": warn,
        }
    )
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest() -> int:
    """Offline. No ntfy, no state write. Proves sub-200k REDUCE is off and the other bands stay."""
    import importlib.util

    sec_before = STATE_PATH.read_bytes()
    p93 = ROOT / "state" / "005930.json"
    b93 = p93.read_bytes() if p93.exists() else None
    sent = {"n": 0}

    def _no_publish(*a, **k):
        sent["n"] += 1
        raise RuntimeError("selftest tried to publish")

    orig_pub = ntfy_publish
    globals()["ntfy_publish"] = _no_publish
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got!r} want={want!r}")

    try:
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        bands = cfg["bands"]
        check("reduce_enabled off", bands.get("reduce_enabled"), False)
        for key, want in {
            "reduce_lt": 200000,
            "buy1_lo": 200000,
            "buy1_hi": 240000,
            "buy2_lo": 240000,
            "buy2_hi": 280000,
            "hold_lo": 290000,
            "hold_hi": 320000,
            "sell1_lo": 330000,
            "sell1_hi": 380000,
            "break38": 380000,
            "nonew_lo": 410000,
            "nonew_hi": 450000,
            "sell2_lo": 460000,
            "sell2_hi": 500000,
            "exit_ge": 500000,
            "chase_ban": 290000,
        }.items():
            check(f"band {key}", bands.get(key), want)

        ladder = [
            (None, "DATA_FAIL"),
            (0, "BELOW"),
            (100000, "BELOW"),
            (176999, "BELOW"),
            (177000, "BELOW"),
            (185000, "BELOW"),
            (198000, "BELOW"),
            (199999, "BELOW"),
            (199999.99, "BELOW"),
            (200000, "BUY1"),
            (239999.99, "BUY1"),
            (240000, "BUY2"),
            (279999.99, "BUY2"),
            (280000, "WATCH"),
            (289999.99, "WATCH"),
            (290000, "HOLD"),
            (319999.99, "HOLD"),
            (320000, "WATCH"),
            (329999.99, "WATCH"),
            (330000, "SELL1"),
            (379999.99, "SELL1"),
            (380000, "HOLD38"),
            (409999.99, "HOLD38"),
            (410000, "NONEW"),
            (449999.99, "NONEW"),
            (450000, "WATCH"),
            (459999.99, "WATCH"),
            (460000, "SELL2"),
            (499999.99, "SELL2"),
            (500000, "EXIT"),
            (600000, "EXIT"),
        ]
        for px, want in ladder:
            check(f"zone {px}", zone(px, bands), want)
            if want != "DATA_FAIL":
                check(f"no 20만하회 at {px}", "20만하회" in warnings(px, None, bands), False)
                title = alert_title(zone(px, bands), px)
                check(f"title not 감액 at {px}", ("감액" in title, title.startswith("[SEC] 005930 ")), (False, True))

        check("200000 label", zone_label("BUY1", 200000), "20~24만")
        check("280000 label", zone_label("WATCH", 280000), "28~29만")
        check("320000 label", zone_label("WATCH", 320000), "32~33만")
        check("450000 label", zone_label("WATCH", 450000), "45~46만")
        check("BUY1 action", ACTION["BUY1"], "진입")
        check("SELL1 action", ACTION["SELL1"], "익절")
        check("EXIT action", ACTION["EXIT"], "익절")
        check("REDUCE copy kept but not selected", (DO_NOW["REDUCE"], ACTION[zone(198000, bands)]), ("신규 금지. 비중을 줄이세요.", "관찰"))

        check("warn 250000", warnings(250000, None, bands), [])
        check("warn 290000", warnings(290000, None, bands), ["29만돌파(종가)"])
        check("warn 330000", warnings(330000, None, bands), ["29만돌파(종가)", "33만감액"])
        check("warn 38만재이탈", warnings(379999, 380000, bands), ["29만돌파(종가)", "33만감액", "38만재이탈"])
        check("warn 380000 no rebreak", "38만재이탈" in warnings(380000, 380000, bands), False)
        check("warn 410000", warnings(410000, None, bands), ["29만돌파(종가)", "33만감액", "41만재평가"])
        check("warn 460000", warnings(460000, None, bands), ["29만돌파(종가)", "33만감액", "41만재평가", "46만익절"])
        check("warn 500000", warnings(500000, None, bands), ["29만돌파(종가)", "33만감액", "41만재평가", "46만익절", "50만과열"])
        w_below = warnings(150000, 400000, bands)
        check("below keeps 38만 only", w_below, ["38만재이탈"])
        check("below 38만 still sends", price_event("event", "BELOW", w_below, "BELOW", []), True)
        check("below 38만 title is not 감액", "감액" in alert_title("BELOW", 150000), False)
        check("below 38만 priority is 4", alert_priority("BELOW", w_below), 4)
        check("below 38만 repeat silent", price_event("event", "BELOW", w_below, "BELOW", w_below), False)

        def would_send(mode, z, warn, prev_z, prev_w, already):
            return price_event(mode, z, warn, prev_z, prev_w) and not already

        check("sub200 event from BUY1 silent", would_send("event", "BELOW", [], "BUY1", [], False), False)
        check("sub200 weekly silent", would_send("weekly", "BELOW", [], "BELOW", [], False), False)
        check("weekly BUY1 still sends", would_send("weekly", "BUY1", [], "BUY1", [], False), True)
        check("weekly already blocks send", would_send("weekly", "HOLD", ["29만돌파(종가)"], "HOLD", ["29만돌파(종가)"], True), False)
        check("weekly HOLD sends", would_send("weekly", "HOLD", ["29만돌파(종가)"], "HOLD", ["29만돌파(종가)"], False), True)
        check("event same zone silent", price_event("event", "BUY1", [], "BUY1", []), False)
        check("event BUY1 to BUY2 sends", price_event("event", "BUY2", [], "BUY1", []), True)
        check("event BELOW back to BUY1 sends", price_event("event", "BUY1", [], "BELOW", []), True)
        check("event warn change sends", price_event("event", "SELL1", ["29만돌파(종가)", "33만감액", "38만재이탈"], "SELL1", ["29만돌파(종가)", "33만감액"]), True)
        check("DATA_FAIL is an event", price_event("event", "DATA_FAIL", [], "BUY1", []), True)
        check("EXIT priority 5", alert_priority("EXIT", warnings(500000, None, bands)), 5)
        check("SELL2 priority 4", alert_priority("SELL2", warnings(460000, None, bands)), 4)
        check("BUY2 priority 3", alert_priority("BUY2", []), 3)

        on = dict(bands, reduce_enabled=True)
        check("flag on restores REDUCE", zone(199999, on), "REDUCE")
        check("flag on 20만하회", warnings(199999, None, on), ["20만하회"])
        check("flag on 200000 still BUY1", zone(200000, on), "BUY1")
        check("flag on zone change would send", price_event("event", zone(199999, on), warnings(199999, None, on), "BUY1", []), True)
        check("flag on title is 감액", "감액" in alert_title("REDUCE", 199999), True)
        check("flag on priority 5", alert_priority("REDUCE", ["20만하회"]), 5)

        earn = cfg["earnings"]
        check("earn CUT", verdict(earn, {"op": 100, "eps": None, "q4_op": None, "q4_note": ""}), "CUT")
        check("earn under cons CUT", verdict(earn, {"op": 110, "eps": 14000, "q4_op": None, "q4_note": ""}), "CUT")
        check("earn UP", verdict(earn, {"op": 114, "eps": 14242, "q4_op": 120, "q4_note": ""}), "UP")
        check("earn HOLD", verdict(earn, {"op": 111, "eps": 14000, "q4_op": 120, "q4_note": ""}), "HOLD")
        check("earn PENDING", verdict(earn, {"op": None, "eps": None}), "PENDING")

        spec = importlib.util.spec_from_file_location("m005930", ROOT / "scripts" / "005930_monitor.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        c93 = mod.load_cfg("005930")
        got_lines = [(ln["key"], ln["op"], ln["level"], ln["title"]) for ln in c93["lines"]]
        check("005930 lines", got_lines, [
            ("buy1", "le", 210000, "1차 매수 30%"),
            ("buy2", "le", 198000, "2차 매수 40%"),
            ("buy3", "le", 185000, "3차 매수 30%"),
            ("trim1", "ge", 260000, "1차 축소 30%"),
            ("trim2", "ge", 295000, "2차 축소 40%"),
            ("stop", "lt", 177000, "손절선 이탈"),
        ])
        ea = c93["earn_auto"]
        check("005930 LTA word", "장기공급계약" in ea["item1"]["lta_words"], True)
        check("005930 CAPEX re", "CAPEX" in ea["item2"]["capex_re"], True)
        check("005930 ASP -10", ea["item3"]["asp_drop_pct"], -10.0)
        check("005930 China names", ("YMTC" in ea["item3"]["china"]["names_re"], "CXMT" in ea["item3"]["china"]["names_re"]), (True, True))
        check("005930 China not auto-negative", ea["item3"]["china"]["counts_as_negative"], False)

        def fired(close):
            st = mod.default_state(c93)
            st["last_day"] = "2026-11-01"
            ev = mod.eval_lines(c93, st, [{"day": "2026-11-02", "close": close}], "2026-11-02", datetime(2026, 11, 6, 19, tzinfo=KST))
            return [(e["kind"], e["key"]) for e in ev]

        check("005930 210000 buy1", fired(210000), [("line", "buy1")])
        check("005930 198000 buy1+buy2", fired(198000), [("line", "buy1"), ("line", "buy2")])
        check("005930 185000 three buys", fired(185000), [("line", "buy1"), ("line", "buy2"), ("line", "buy3")])
        check("005930 177000 stop silent", fired(177000), [("line", "buy1"), ("line", "buy2"), ("line", "buy3")])
        check("005930 176999 stop", fired(176999), [("line", "buy1"), ("line", "buy2"), ("line", "buy3"), ("line", "stop")])
        check("005930 260000 trim1", fired(260000), [("line", "trim1"), ("extra", "up220k"), ("extra", "up230k")])
        check("005930 295000 trim1+trim2", fired(295000), [("line", "trim1"), ("line", "trim2"), ("extra", "up220k"), ("extra", "up230k")])
        check("005930 210001 no line yet", fired(210001), [])

        now = datetime(2026, 10, 30, 19, tzinfo=KST)
        no_china = lambda c, n: {"status": mod.ec.OK, "fail": None, "hits": [], "n": 0}  # noqa: E731
        ok_txt = "\n".join([
            "이에 전분기 대비 회사 D램 평균판매가격(ASP)은 약 20%, 낸드 ASP는 10% 후반 상승했다.",
            "2026년 연간 시설 투자 규모는 84조원으로 전년 대비 증가할 것으로 예상된다.",
            "2027년 HBM 공급 물량은 목표 수준의 가격으로 협상이 완료돼 이미 완판된 상태다.",
            "일부 고객과는 장기공급계약을 완료했고 추가 고객과 협상을 진행 중이다.",
        ])
        warn_txt = "\n".join([
            "이에 전분기 대비 회사 D램 ASP는 10% 초반 하락했고 낸드 ASP는 한 자릿수 후반 하락했다.",
            "2027년 시설 투자는 큰 폭으로 증가할 것으로 예상된다.",
            "4분기 D램 가격은 하락 전환이 불가피할 것으로 보인다.",
            "일부 고객과 2027년 HBM 장기공급계약 물량을 재협상하고 있다.",
        ])
        check("005930 LTA/CAPEX/ASP ok", [i["status"] for i in mod.earn_judge(ea, ok_txt, now, no_china)["items"]], [mod.ec.OK, mod.ec.OK, mod.ec.OK])
        check("005930 LTA/CAPEX/ASP warn", [i["status"] for i in mod.earn_judge(ea, warn_txt, now, no_china)["items"]], [mod.ec.WARN, mod.ec.WARN, mod.ec.WARN])

        check("no publish", sent["n"], 0)
        import hashlib
        check("sec state untouched", hashlib.sha256(STATE_PATH.read_bytes()).hexdigest(), hashlib.sha256(sec_before).hexdigest())
        now93 = p93.read_bytes() if p93.exists() else None
        check("005930 state untouched", hashlib.sha256(now93).hexdigest() if now93 is not None else None, hashlib.sha256(b93).hexdigest() if b93 is not None else None)
    finally:
        globals()["ntfy_publish"] = orig_pub

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
