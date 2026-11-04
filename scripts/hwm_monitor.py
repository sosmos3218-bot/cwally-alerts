#!/usr/bin/env python3
"""[HWM] 하우멧 에어로스페이스 분할 매수 구간, 손절, 일부 익절 + 2027 매출 가이던스 무효화 점검. 평일 09:00 KST morning 묶음 (xbi, ibb 뒤).

가격: 미국 전일 종가(Yahoo 일봉). 데이터 거래일(state.last_date)로 중복 방지.
구간(configs/hwm.json zones): 들어오면 1번 알림, 경계 밖으로 rearm_pct(3%) 넘게 벗어난 뒤 다시 들어오면 다시 알림.
  추격 금지(nochase)는 1차(z1) 알림이 한 번 나기 전까지만 판정(state.z1_hit).
손절: 종가 < stop.below 1번(p4), stop.rearm_above 초과 종가에서 재무장.
익절: 종가 >= take_profit.at_or_above 1번(p3), take_profit.rearm_below 미만 종가에서 재무장.
실적: seed_after 이후 새 8-K Item 2.02 EX-99.1 에서 2027 매출 가이던스를 읽어 11.5B 미만이면 무효화(p4, 이후 구간 알림 중단,
  손절/익절은 계속), 아니면 실적 확인 요약(p3). 못 읽으면 접수번호마다 cwally-ops 1번. 조회 실패는 cwally-ops 하루 1번.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import html
import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "hwm.json"
STATE_PATH = ROOT / "state" / "hwm.json"
SEC_UA = {"User-Agent": "cwally-alerts hwm research monitor research@example.com"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"
TAGS = ["airplane"]


def fetch(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------- 구간 판정 (lrcx 와 같은 재진입 규칙) ----------
def in_zone(p: float, z: dict) -> bool:
    return p >= z["lo"] and (z["hi"] is None or p <= z["hi"])


def far_out(p: float, z: dict, pct: float) -> bool:
    k = pct / 100
    return p < z["lo"] * (1 - k) or (z["hi"] is not None and p > z["hi"] * (1 + k))


def judge_zones(zones: list[dict], zst: dict, p: float, pct: float) -> list[dict]:
    fired = []
    for z in zones:
        s = zst.setdefault(z["key"], {"armed": True})
        if in_zone(p, z):
            if s["armed"]:
                s["armed"] = False
                fired.append(z)
        elif not s["armed"] and far_out(p, z, pct):
            s["armed"] = True
    return fired


def seed_state(cfg: dict, p: float) -> dict:
    return {"zones": {z["key"]: {"armed": not in_zone(p, z)} for z in cfg["zones"]},
            "stop": {"armed": not p < cfg["stop"]["below"]},
            "tp": {"armed": not p >= cfg["take_profit"]["at_or_above"]},
            "z1_hit": False}


def judge_price(cfg: dict, st: dict, p: float) -> list[str]:
    """종가 하나를 판정해 발화한 키 목록을 돌려준다 (nochase, z1, z2, z3, stop, tp). st 를 고친다."""
    zones = [z for z in cfg["zones"] if not (z["key"] == "nochase" and st.get("z1_hit"))]
    keys = [z["key"] for z in judge_zones(zones, st["zones"], p, cfg["rearm_pct"])]
    if "z1" in keys:
        st["z1_hit"] = True
    s, sc = st["stop"], cfg["stop"]
    if s["armed"] and p < sc["below"]:
        s["armed"] = False
        keys.append("stop")
    elif not s["armed"] and p > sc["rearm_above"]:
        s["armed"] = True
    t, tc = st["tp"], cfg["take_profit"]
    if t["armed"] and p >= tc["at_or_above"]:
        t["armed"] = False
        keys.append("tp")
    elif not t["armed"] and p < tc["rearm_below"]:
        t["armed"] = True
    return keys


def zone_where(cfg: dict, p: float) -> str:
    for z in cfg["zones"]:
        if in_zone(p, z):
            return z["key"]
    return "구간 밖"


# ---------- 본문 ----------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | HWM | 하우멧 분할 매수 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def plan_table(cfg: dict) -> str:
    zs = {z["key"]: z for z in cfg["zones"]}
    return (f"추격 금지 ${zs['nochase']['lo']:g} 이상, 1차 {zs['z1']['lo']:g}~{zs['z1']['hi']:g}({zs['z1']['budget_pct']}%), "
            f"2차 {zs['z2']['lo']:g}~{zs['z2']['hi']:g}({zs['z2']['budget_pct']}%), 3차 {zs['z3']['lo']:g}~{zs['z3']['hi']:g}({zs['z3']['budget_pct']}%), "
            f"손절 종가 ${cfg['stop']['below']:g} 미만, 일부 익절 종가 ${cfg['take_profit']['at_or_above']:g} 이상")


def price_status(cfg: dict, day: str, p: float, st: dict) -> str:
    b = cfg["baseline"]
    dd = (p / b["high_52w"] - 1) * 100
    pe26, pe27 = p / cfg["eps_2026_guide"], p / cfg["eps_2027_consensus"]
    inv = " 무효화 상태(2027 매출 가이던스 하향)라 분할 매수는 중단 중." if st.get("invalid") else ""
    return (f"{day} 미국 종가 ${p:.2f}, 52주 고점 ${b['high_52w']:g}({b['high_52w_date']}) 대비 {dd:.1f}%. "
            f"P/E 2026 가이던스 {pe26:.1f}배, 2027 컨센서스 {pe27:.1f}배. 계획: {plan_table(cfg)}.{inv}")


def price_msg(cfg: dict, key: str, day: str, p: float, stamp: str, st: dict) -> tuple[str, str, int]:
    status = price_status(cfg, day, p, st)
    pct = cfg["rearm_pct"]
    if key == "stop":
        sc = cfg["stop"]
        return sc["title"], body(stamp, "HWM 종가가 미리 정한 손절 기준 아래로 내려왔다는 알림. 매매 지시가 아닙니다.", status,
                                 "보유분이 있으면 정리를 검토. 남은 분할 매수는 멈추기.",
                                 f"종가가 ${sc['rearm_above']:g} 를 넘으면 다시 무장합니다. 근거: {sc['basis']}.",
                                 "손절 뒤 재진입은 실적과 가이던스를 다시 본 뒤에만.", "손절선 아래에서 물타기, 반등 기대만으로 버티기."), sc["priority"]
    if key == "tp":
        tc = cfg["take_profit"]
        return tc["title"], body(stamp, "HWM 종가가 일부 익절 검토 가격에 닿았다는 알림. 매매 지시가 아닙니다.", status,
                                 f"보유분의 {tc['fraction']} 익절을 검토. 나머지는 계속 보유.",
                                 f"종가가 ${tc['rearm_below']:g} 아래로 내려가면 다시 무장합니다. 근거: {tc['basis']}.",
                                 "해외 양도세 22%(연 250만원 공제)와 환율을 같이 보기.", "한 번에 전부 팔기, 이 가격에서 새로 추격 매수."), tc["priority"]
    z = next(z for z in cfg["zones"] if z["key"] == key)
    if key == "nochase":
        action = "신규 매수 보류. 1차 구간까지 기다리기."
    else:
        n = {"z1": "1", "z2": "2", "z3": "3"}[key]
        action = f"계획한 {n}차 {z['budget_pct']}%만 검토. 한 번에 다 사지 않기. 해외 계좌 기준."
    return z["title"], body(stamp, "HWM 종가가 미리 정한 분할 매수 구간이나 추격 금지선에 들어왔다는 알림. 매매 지시가 아닙니다.", status, action,
                            f"같은 구간은 종가가 경계 밖으로 {pct:g}% 넘게 벗어났다가 다시 들어올 때만 다시 옵니다. 4분기 실적(2027년 2월 중순 추정)에서 2027 매출 가이던스 점검이 따로 옵니다.",
                            "구간 알림만으로 비중을 줄이지 않기. 일부 익절 검토는 종가 $248 이상.",
                            "추격 금지선 위에서 신규 매수, 손절선 아래에서 물타기."), z["priority"]


# ---------- 실적 8-K ----------
def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " ")).strip()


def earnings_8ks(data: dict) -> list[dict]:
    r = (data.get("filings") or {}).get("recent") or {}
    out = []
    for i, form in enumerate(r.get("form") or []):
        if form == "8-K" and "2.02" in (r["items"][i] or ""):
            out.append({"acc": r["accessionNumber"][i], "date": r["filingDate"][i]})
    return sorted(out, key=lambda x: x["date"])


def ex991(cik: str, acc: str) -> tuple[str, str]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    names = [x.get("name", "") for x in json.loads(fetch(base + "index.json", SEC_UA)).get("directory", {}).get("item", [])]
    pick = [n for n in names if re.search(r"ex(hibit)?[-_x]?99[-_x.]?0?1(?!\d)|ex991", n, re.I) and n.lower().endswith((".htm", ".html"))]
    if not pick:
        raise RuntimeError("EX-99.1 파일 없음")
    return html_to_text(fetch(base + pick[0], SEC_UA)), base + pick[0]


NUM_RE = re.compile(r"\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(B|M|billion|million)?(?![\w%])", re.I)


def to_b(v: str, unit: str | None) -> float:
    x = float(v.replace(",", ""))
    u = (unit or "").lower()
    if u.startswith("m") or (not u and x >= 1000):
        return x / 1000
    return x


def table_numbers(s: str) -> list[float]:
    vals, pos = [], 0
    while len(vals) < 6:
        m = NUM_RE.match(s, pos)
        if not m:
            break
        vals.append(to_b(m.group(1), m.group(2)))
        pos = m.end()
    return vals


def parse_guide_2027(t: str) -> tuple[float | None, str]:
    """2027 매출 가이던스(십억 달러). 표 Baseline 우선, 없으면 문장. 못 읽으면 (None, 이유)."""
    for m in re.finditer(r"(?:FY|Full Year)\s*2027\s*Guidance", t, re.I):
        seg = t[m.end():m.end() + 400]
        r = re.search(r"Revenue", seg)
        if not r:
            continue
        vals = table_numbers(seg[r.end():])
        q_first = re.search(r"Q\d\s*2027\s*Guidance", t[max(0, m.start() - 80):m.start()], re.I) is not None
        v = None
        if len(vals) >= 6:
            v = vals[4] if q_first else vals[1]
        elif len(vals) >= 3:
            v = vals[1]
        if v is not None and 5 <= v <= 40:
            return round(v, 3), "표 Baseline"
    pats = [r"(?:Full Year|FY)\s*2027 Revenue Guidance:?\s*(?:of\s*)?(?:Approximately|About|~)?\s*\$\s*([\d.]+)\s*Billion",
            r"2027,? we (?:now )?(?:envisage|expect|anticipate|project|see|forecast) revenue of (?:approximately |about |~)?\$\s*([\d.]+) billion",
            r"2027 revenue (?:guidance )?(?:of|to be|is|at) (?:approximately |about |~)?\$\s*([\d.]+) billion"]
    for pat in pats:
        m = re.search(pat, t, re.I)
        if m:
            v = float(m.group(1))
            if 5 <= v <= 40:
                return v, "문장"
    return None, "2027 매출 가이던스 문구 없음"


def release_label(t: str) -> str:
    m = re.search(r"Reports ((?:First|Second|Third|Fourth) Quarter(?: and Full Year)? \d{4}) Results", t)
    return m.group(1) if m else "실적"


def earn_msg(cfg: dict, f: dict, g: float, src: str, label: str, link: str, stamp: str) -> tuple[str, str, int, bool]:
    inv = cfg["invalidation"]
    floor = inv["rev_2027_floor_b"]
    status = (f"{f['date']} 8-K(Item 2.02, {f['acc']}) {label}: 2027 매출 가이던스 ${g:g}B ({src}), "
              f"기준 ${floor:g}B (10/29 첫 제시 약 11.5B). 링크 {link}")
    if g < floor:
        return inv["title"], body(stamp, "HWM 실적에서 2027 매출 가이던스가 처음 제시한 11.5B 아래로 내려갔다는 알림.", status + ". 무효화 조건 해당.",
                                  "남은 분할 매수 중단. 구간 알림이 와도 새로 사지 않기 (이후 구간 알림은 보내지 않음).",
                                  "다음 실적이나 가이던스 회복 때 다시 판단. 손절과 일부 익절 알림은 계속 옵니다.",
                                  "보유분이 있으면 손절 기준(종가 $155 미만)을 그대로 지키기.", "무효화 뒤 가격만 보고 분할 계속하기."), inv["priority"], True
    title = f"[HWM] HWM 실적 확인 | 2027 매출 가이던스 ${g:g}B, 분할 계획 유지"
    return title, body(stamp, "HWM 실적이 무효화 조건(2027 매출 가이던스 11.5B 미만)에 걸리지 않았다는 요약.", status + ". 무효화 조건 미해당.",
                       "분할 매수 계획 유지. 구간 알림을 기다리기.", "다음 실적 때 다시 점검.", "실적 하나로 비중을 크게 바꾸지 않기.",
                       "요약을 매수 신호로 읽기."), 3, False


def ops_msg(stamp: str, title_tail: str, status: str) -> tuple[str, str, int]:
    return (f"[HWM] HWM {title_tail}", body(stamp, "HWM 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 시세나 원문을 직접 보세요.",
                                              "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


def earnings_check(cfg: dict, st: dict, data: dict, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    if "earn" not in st:  # 첫 실행: 지금까지 나온 실적 8-K 는 판정 없이 seed
        st["earn"] = {"seen": [f["acc"] for f in earnings_8ks(data)], "fail": [], "seeded_at": stamp}
        print(f"earn seeded: {len(st['earn']['seen'])} accessions")
        return out
    es = st["earn"]
    after = cfg["invalidation"]["seed_after"]
    for f in earnings_8ks(data):
        if f["acc"] in es["seen"] or f["acc"] in es["fail"] or f["date"] <= after:
            continue
        try:
            text, link = ex991(cfg["cik"], f["acc"])
            g, src = parse_guide_2027(text)
            if g is None:
                raise RuntimeError(src)
        except Exception as exc:  # noqa: BLE001
            es["fail"].append(f["acc"])
            out.append(ops_msg(stamp, "실적 8-K 파싱 실패 | 원문 확인 필요", f"{f['date']} 8-K {f['acc']} EX-99.1 에서 2027 매출 가이던스 {NA}: {str(exc)[:150]}. 통과로 세지 않음. 이 접수번호는 다시 안내하지 않습니다."))
            continue
        es["seen"].append(f["acc"])
        t, m, pr, bad = earn_msg(cfg, f, g, src, release_label(text), link, stamp)
        if bad:
            st["invalid"] = {"acc": f["acc"], "date": f["date"], "rev_2027_b": g}
        out.append((t, m, pr))
    return out


# ---------- 가격 ----------
def last_close(url: str, now: datetime) -> tuple[str, float] | None:
    d = json.loads(fetch(url, WEB_UA))
    res = ((d.get("chart") or {}).get("result") or [None])[0] or {}
    ts = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    et_now = now.astimezone(ET)
    best = None
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, ET).date()
        if day < et_now.date() or (day == et_now.date() and (et_now.hour, et_now.minute) >= (16, 15)):
            best = (day.isoformat(), round(float(c), 2))
    return best


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    out: list[tuple[str, str, int]] = []
    try:
        lc = last_close(cfg["sources"]["quote"], now)
    except Exception as exc:  # noqa: BLE001
        lc, qerr = None, str(exc)[:150]
    else:
        qerr = None if lc else "종가 없음"
    if lc:
        day, p = lc
        if "zones" not in st:
            st.update(seed_state(cfg, p))
            st["last_date"] = day
            print(f"seeded at {day} ${p}: {st}")
        if day != st.get("last_date"):
            for k in judge_price(cfg, st, p):
                if st.get("invalid") and k in ("nochase", "z1", "z2", "z3"):
                    print(f"{k} fired but invalidated: not sent")
                    continue
                out.append(price_msg(cfg, k, day, p, stamp, st))
            st["last_date"] = day
        st["last_close"] = [day, p]
        print(f"close {day} ${p:.2f} -> {zone_where(cfg, p)}, zones armed={ {k: v['armed'] for k, v in st['zones'].items()} }, "
              f"stop armed={st['stop']['armed']}, tp armed={st['tp']['armed']}, z1_hit={st.get('z1_hit')}, invalid={bool(st.get('invalid'))}")
    elif st.get("quote_fail") != stamp[:10]:
        out.append(ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", f"Yahoo 일봉 {NA} ({qerr})."))
        st["quote_fail"] = stamp[:10]
    try:
        out += earnings_check(cfg, st, json.loads(fetch(cfg["sources"]["sec"], SEC_UA)), stamp)
    except Exception as exc:  # noqa: BLE001
        print(f"sec fail: {exc}")
        if st.get("sec_fail") != stamp[:10]:
            out.append(ops_msg(stamp, "SEC 조회 실패 | 실적 점검 못 함", f"SEC 공시 목록 {NA} ({str(exc)[:150]})."))
            st["sec_fail"] = stamp[:10]
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        assert t.startswith("[HWM]") and "HWM" in t[5:], t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, TAGS, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- selftest ----------
# 10/29 실제 EX-99.1 문구 일부 (표 + 문장), 2027년 2월 형식 가상 표
REL_Q3 = ("Howmet Aerospace Reports Third Quarter 2026 Results Revenue up 29% Full Year 2027 Revenue Guidance: Approximately $11.5 Billion, Up ~12% Year over Year "
          "Looking forward to 2027, we envisage revenue of approximately $11.5 billion, representing approximately 12% growth from the increased 2026 guidance. "
          "2026 Guidance Dollars in Millions; Per share amounts in dollars, diluted Q4 2026 Guidance FY 2026 Guidance Low Baseline High Low Baseline High "
          "Revenue $ 2,740 $ 2,750 $ 2,760 $ 10,290 $ 10,300 $ 10,310 Baseline Change +$250")
REL_FEB = ("Howmet Aerospace Reports Fourth Quarter and Full Year 2026 Results 2027 Guidance Q1 2027 Guidance FY 2027 Guidance Low Baseline High Low Baseline High "
           "Revenue $2.800B $2.810B $2.820B $11.550B $11.650B $11.750B Adj. EBITDA *1 $920M $925M $930M")
REL_FEB_M = REL_FEB.replace("$11.550B $11.650B $11.750B", "$ 11,200 $ 11,300 $ 11,400").replace("$2.800B $2.810B $2.820B", "$ 2,800 $ 2,810 $ 2,820")
REL_FY_ONLY = "Howmet Aerospace Reports Fourth Quarter and Full Year 2026 Results FY 2027 Guidance Low Baseline High Revenue $11.400B $11.500B $11.600B Adj. EBITDA"
REL_NONE = "Howmet Aerospace Reports Fourth Quarter and Full Year 2026 Results Revenue of $2.8 billion. No outlook."


def selftest(cfg: dict) -> int:
    stamp = "2026-11-03 09:00 KST"
    st = seed_state(cfg, 199.22)
    assert all(v["armed"] for v in st["zones"].values()) and st["stop"]["armed"] and st["tp"]["armed"] and not st["z1_hit"], st
    print("seed 199.22: 모두 무장")
    # z1 192~197: 재무장은 197*1.03=202.91 초과 또는 192*0.97=186.24 미만
    # nochase 215: 1차 전에는 판정, 1차가 한 번 나면 꺼짐
    seq = [
        (216.0, ["nochase"]), (220.0, []), (209.0, []), (206.0, []), (216.0, ["nochase"]),
        (196.0, ["z1"]), (193.0, []), (201.0, []), (195.0, []), (203.0, []), (196.5, ["z1"]),
        (230.0, []), (216.0, []),                       # 1차 뒤 추격 금지 꺼짐
        (184.0, ["z2"]), (180.0, []), (186.0, []), (179.0, []), (191.0, []), (184.5, ["z2"]),
        (169.0, ["z3"]), (163.0, []), (175.0, []), (168.0, []), (175.2, []), (165.0, ["z3"]),
        (154.9, ["stop"]), (150.0, []), (159.6, []), (154.0, []), (159.7, []), (154.5, ["stop"]),
        (155.0, []), (159.7, []),                        # 155 정확히는 미만 아님
        (248.0, ["tp"]), (260.0, []), (240.6, []), (249.0, []), (240.5, []), (248.5, ["tp"]),
    ]
    for p, want in seq:
        got = judge_price(cfg, st, p)
        assert got == want, (p, got, want, st)
    print(f"price seq ok: {len(seq)} 종가, 구간/손절/익절 각각 1번 발화와 3%(손절 159.65, 익절 240.56) 재무장 확인")
    # 같은 날 여러 구간: 3% 밖에서 바로 z3 아래로 갭 하락하면 z3 만, 손절은 별도
    st2 = seed_state(cfg, 199.22)
    assert judge_price(cfg, st2, 150.0) == ["stop"], st2
    print("gap 150: 구간 아래라 구간 알림 없음, 손절만")
    st3 = seed_state(cfg, 199.22)
    sent = []
    for k in judge_price(cfg, st3, 195.0):
        sent.append(price_msg(cfg, k, "2026-11-02", 195.0, stamp, st3))
    assert len(sent) == 1 and sent[0][0] == cfg["zones"][1]["title"] and sent[0][2] == 3
    for p in (216.0, 194.0, 181.0, 165.0):
        k = zone_where(cfg, p)
        t, m, pr = price_msg(cfg, k, "2026-11-02", p, stamp, {})
        assert t.startswith("[HWM] HWM") and "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port", t
        assert "확인 불가" not in t and "조회 실패" not in t, t
        print(f"-- sample {k}\nTITLE: {t}\nPRIORITY: {pr}\n{m.splitlines()[2]}\n")
    for k, p in (("stop", 152.0), ("tp", 250.0)):
        t, m, pr = price_msg(cfg, k, "2026-11-02", p, stamp, {})
        assert t.startswith("[HWM] HWM") and route_topic("cwally-port", t) == "cwally-port" and "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"-- sample {k}\nTITLE: {t}\nPRIORITY: {pr}\n")
    for name, rel, want in (("q3 sentence", REL_Q3, 11.5), ("feb table B", REL_FEB, 11.65), ("feb table M", REL_FEB_M, 11.3), ("fy only", REL_FY_ONLY, 11.5)):
        g, src = parse_guide_2027(rel)
        assert g == want, (name, g, src)
        print(f"parse {name}: {g} ({src})")
    assert parse_guide_2027(REL_NONE)[0] is None
    f = {"acc": "0001104659-27-000001", "date": "2027-02-11"}
    for g, want_bad in ((11.65, False), (11.5, False), (11.3, True)):
        t, m, pr, bad = earn_msg(cfg, f, g, "표 Baseline", "Fourth Quarter and Full Year 2026", "https://www.sec.gov/x", stamp)
        assert bad == want_bad and pr == (4 if bad else 3) and t.startswith("[HWM] HWM") and route_topic("cwally-port", t) == "cwally-port", t
        assert "\u00b7" not in t + m and "\u2014" not in t + m
        print(f"-- earnings {g}\nTITLE: {t}\nPRIORITY: {pr}\n")
    # 무효화 뒤 구간 알림 생략 (run 과 같은 필터)
    st4 = seed_state(cfg, 199.22)
    st4["invalid"] = {"acc": f["acc"]}
    keys = judge_price(cfg, st4, 195.0)
    assert keys == ["z1"] and [k for k in keys if not (st4.get("invalid") and k in ("nochase", "z1", "z2", "z3"))] == []
    print("invalidated: 구간 알림 생략 확인")
    for tail in ("종가 조회 실패 | 구간 판정 못 함", "실적 8-K 파싱 실패 | 원문 확인 필요", "SEC 조회 실패 | 실적 점검 못 함"):
        t, _, _ = ops_msg(stamp, tail, "x")
        assert route_topic("cwally-port", t) == "cwally-ops", t
    print("ops notices route to cwally-ops")
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            st = load_state()
            if st.get("crash_day") == now.strftime("%Y-%m-%d"):
                return 0
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트 오류 | 이번 실행 판정 못 함", f"{type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
            st["crash_day"] = now.strftime("%Y-%m-%d")
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
