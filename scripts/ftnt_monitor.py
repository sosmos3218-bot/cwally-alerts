#!/usr/bin/env python3
"""[FTNT] 포티넷 분할 매수 구간, 손절, 일부 익절 + 2027 서비스 매출 가이던스, 4분기 빌링 무효화 점검. 평일 09:00 KST morning 묶음 (hwm 뒤).

가격: 미국 전일 종가(Yahoo 일봉). 데이터 거래일(state.last_date)로 중복 방지.
구간(configs/ftnt.json zones): 들어오면 1번 알림, 경계 밖으로 rearm_pct(3%) 넘게 벗어난 뒤 다시 들어오면 다시 알림.
  추격 금지(nochase)는 1차(z1) 알림이 한 번 나기 전까지만 판정(state.z1_hit).
손절: 종가 < stop.below 1번(p4), stop.rearm_above 초과 종가에서 재무장.
익절: 종가 >= take_profit.at_or_above 1번(p3), take_profit.rearm_below 미만 종가에서 재무장.
실적: seed_after 이후 새 8-K Item 2.02 EX-99.1 이 2026 4분기/연간 실적이면 (a) 2027 서비스 매출 가이던스 중간값 성장(같은
  보도자료의 2026 연간 서비스 매출 대비) 10% 미만 또는 (b) 4분기 Total billings < 2,775M 이면 무효화(p4, 이후 구간 알림 중단,
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
CFG_PATH = ROOT / "configs" / "ftnt.json"
STATE_PATH = ROOT / "state" / "ftnt.json"
SEC_UA = {"User-Agent": "cwally-alerts ftnt research monitor research@example.com"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"
TAGS = ["shield"]


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
    return "\n".join([f"{stamp} | FTNT | 포티넷 분할 매수 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
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
    inv = " 무효화 상태(2027 서비스 매출 가이던스 둔화 또는 4분기 빌링 미달)라 분할 매수는 중단 중." if st.get("invalid") else ""
    return (f"{day} 미국 종가 ${p:.2f}, 52주 고점 ${b['high_52w']:g}({b['high_52w_date']}) 대비 {dd:.1f}%. "
            f"P/E 2026 가이던스 {pe26:.1f}배, 2027 컨센서스 {pe27:.1f}배. 계획: {plan_table(cfg)}.{inv}")


def price_msg(cfg: dict, key: str, day: str, p: float, stamp: str, st: dict) -> tuple[str, str, int]:
    status = price_status(cfg, day, p, st)
    pct = cfg["rearm_pct"]
    if key == "stop":
        sc = cfg["stop"]
        return sc["title"], body(stamp, "FTNT 종가가 미리 정한 손절 기준 아래로 내려왔다는 알림. 매매 지시가 아닙니다.", status,
                                 "보유분이 있으면 정리를 검토. 남은 분할 매수는 멈추기.",
                                 f"종가가 ${sc['rearm_above']:g} 를 넘으면 다시 무장합니다. 근거: {sc['basis']}.",
                                 "손절 뒤 재진입은 실적과 가이던스를 다시 본 뒤에만.", "손절선 아래에서 물타기, 반등 기대만으로 버티기."), sc["priority"]
    if key == "tp":
        tc = cfg["take_profit"]
        return tc["title"], body(stamp, "FTNT 종가가 일부 익절 검토 가격에 닿았다는 알림. 매매 지시가 아닙니다.", status,
                                 f"보유분의 {tc['fraction']} 익절을 검토. 나머지는 계속 보유.",
                                 f"종가가 ${tc['rearm_below']:g} 아래로 내려가면 다시 무장합니다. 근거: {tc['basis']}.",
                                 "해외 양도세 22%(연 250만원 공제)와 환율을 같이 보기.", "한 번에 전부 팔기, 이 가격에서 새로 추격 매수."), tc["priority"]
    z = next(z for z in cfg["zones"] if z["key"] == key)
    if key == "nochase":
        action = "신규 매수 보류. 1차 구간까지 기다리기."
    else:
        n = {"z1": "1", "z2": "2", "z3": "3"}[key]
        action = f"계획한 {n}차 {z['budget_pct']}%만 검토. 한 번에 다 사지 않기. 해외 계좌 기준."
    return z["title"], body(stamp, "FTNT 종가가 미리 정한 분할 매수 구간이나 추격 금지선에 들어왔다는 알림. 매매 지시가 아닙니다.", status, action,
                            f"같은 구간은 종가가 경계 밖으로 {pct:g}% 넘게 벗어났다가 다시 들어올 때만 다시 옵니다. 4분기 실적(2027년 2월 첫 주 추정)에서 2027 서비스 매출 가이던스와 4분기 빌링 점검이 따로 옵니다.",
                            "구간 알림만으로 비중을 줄이지 않기. 일부 익절 검토는 종가 $170 이상.",
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


def num(v: str) -> float:
    return float(v.replace(",", ""))


def parse_release(t: str, guide_year: int, base_year: int) -> dict:
    """Fortinet 4분기/연간 보도자료에서 판정 숫자를 읽는다. 못 읽은 값은 None.

    q4: 'Fourth Quarter and Full Year {base_year}' 표기 여부, has_guide: 'For the fiscal year {guide_year}' 블록 여부
    svc_lo_b/svc_hi_b: 그 블록의 Service revenue 가이던스(십억)
    svc_base_m: 손익계산서 'REVENUE: Product $ a $ b $ c $ d Service e f g h' 의 g (당해 연간, 백만)
    bill_m: 4분기 Total billings (백만). 표 값이면 반올림 폭 0, 문장 '$X.XX billion' 이면 마지막 자리 반올림 폭
    """
    out: dict = {"q4": re.search(rf"Fourth Quarter and Full Year {base_year}\b", t) is not None}
    m = re.search(rf"For the fiscal year {guide_year},? Fortinet currently expects:?(.{{0,600}})", t)
    out["has_guide"] = m is not None
    out["svc_lo_b"] = out["svc_hi_b"] = None
    if m:
        g = re.search(r"Service revenue in the range of \$([\d.,]+) (billion|million) to \$([\d.,]+) (billion|million)", m.group(1))
        if g:
            k1 = 1 if g.group(2) == "billion" else 0.001
            k2 = 1 if g.group(4) == "billion" else 0.001
            out["svc_lo_b"], out["svc_hi_b"] = num(g.group(1)) * k1, num(g.group(3)) * k2
    m = re.search(r"REVENUE: Product \$ ?[\d,.]+ \$ ?[\d,.]+ \$ ?[\d,.]+ \$ ?[\d,.]+ Service ([\d,.]+) ([\d,.]+) ([\d,.]+) ([\d,.]+)", t)
    out["svc_base_m"] = num(m.group(3)) if m else None
    out["bill_m"], out["bill_round_m"], out["bill_src"] = None, None, None
    m = re.search(r"Total billings \$ ?([\d,]+\.\d) \$ ?[\d,]+\.\d", t)
    if m:
        out["bill_m"], out["bill_round_m"], out["bill_src"] = num(m.group(1)), 0.0, "표"
    else:
        m = re.search(r"Total billings were \$([\d.]+) (billion|million) for the fourth quarter", t)
        if m:
            v = num(m.group(1))
            dec = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
            if m.group(2) == "billion":
                out["bill_m"], out["bill_round_m"] = v * 1000, 0.5 * 10 ** (-dec) * 1000
            else:
                out["bill_m"], out["bill_round_m"] = v, 0.5 * 10 ** (-dec)
            out["bill_src"] = "문장"
    return out


def judge_release(cfg: dict, r: dict) -> dict:
    """svc/bill: 'ok' | 'bad' | None(판정 불가). growth: 서비스 가이던스 중간값 성장률(%)."""
    inv = cfg["invalidation"]
    res: dict = {"svc": None, "bill": None, "growth": None, "mid_b": None}
    if r["svc_lo_b"] is not None and r["svc_base_m"] and 1000 <= r["svc_base_m"] <= 20000:
        mid = (r["svc_lo_b"] + r["svc_hi_b"]) / 2
        gr = (mid * 1000 / r["svc_base_m"] - 1) * 100
        res.update(mid_b=mid, growth=gr, svc="bad" if gr < inv["service_growth_floor_pct"] else "ok")
    if r["bill_m"] is not None:
        floor, w = inv["q4_billings_floor_m"], r["bill_round_m"] or 0.0
        if r["bill_m"] + w < floor:
            res["bill"] = "bad"
        elif r["bill_m"] - w >= floor:
            res["bill"] = "ok"
    return res


def earn_msg(cfg: dict, f: dict, r: dict, j: dict, link: str, stamp: str) -> tuple[str, str, int, bool]:
    inv = cfg["invalidation"]
    gy, by = inv["guide_year"], inv["base_year"]
    svc = (f"{gy} 서비스 매출 가이던스 ${r['svc_lo_b']:.3f}~{r['svc_hi_b']:.3f}B (중간값 ${j['mid_b']:.3f}B), {by} 연간 서비스 매출 "
           f"${r['svc_base_m']:,.1f}M 대비 {j['growth']:+.1f}% (기준 {inv['service_growth_floor_pct']:g}%)")
    bill = f"4분기 빌링 ${r['bill_m']:,.1f}M ({r['bill_src']}, 기준 ${inv['q4_billings_floor_m']:,.0f}M = 회사 가이던스 하단)"
    status = f"{f['date']} 8-K(Item 2.02, {f['acc']}): {svc}. {bill}. 링크 {link}"
    bad = [x for x, k in (("서비스 매출 가이던스 성장 10% 미만", "svc"), ("4분기 빌링 가이던스 하단 미달", "bill")) if j[k] == "bad"]
    if bad:
        return inv["title"], body(stamp, "FTNT 실적이 분할 매수 전제(서비스 성장 유지, 빌링 가이던스 달성)를 깼다는 알림.",
                                  status + ". 무효화 사유: " + ", ".join(bad) + ".",
                                  "남은 분할 매수 중단. 구간 알림이 와도 새로 사지 않기 (이후 구간 알림은 보내지 않음).",
                                  "다음 실적이나 서비스 성장 회복 때 다시 판단. 손절과 일부 익절 알림은 계속 옵니다.",
                                  "보유분이 있으면 손절 기준(종가 $97 미만)을 그대로 지키기.", "무효화 뒤 가격만 보고 분할 계속하기."), inv["priority"], True
    title = f"[FTNT] FTNT 실적 확인 | {gy} 서비스 매출 가이던스 {j['growth']:+.1f}%, 분할 계획 유지"
    return title, body(stamp, "FTNT 4분기 실적이 무효화 조건(서비스 가이던스 성장 10% 미만, 4분기 빌링 2,775M 미만)에 걸리지 않았다는 요약.",
                       status + ". 무효화 조건 미해당.", "분할 매수 계획 유지. 구간 알림을 기다리기.", "다음 실적 때 다시 점검.",
                       "실적 하나로 비중을 크게 바꾸지 않기.", "요약을 매수 신호로 읽기."), 3, False


def ops_msg(stamp: str, title_tail: str, status: str) -> tuple[str, str, int]:
    return (f"[FTNT] FTNT {title_tail}", body(stamp, "FTNT 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 시세나 원문을 직접 보세요.",
                                                "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


def earnings_check(cfg: dict, st: dict, data: dict, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    if "earn" not in st:  # 첫 실행: 지금까지 나온 실적 8-K 는 판정 없이 seed
        st["earn"] = {"seen": [f["acc"] for f in earnings_8ks(data)], "fail": [], "seeded_at": stamp}
        print(f"earn seeded: {len(st['earn']['seen'])} accessions")
        return out
    es = st["earn"]
    inv = cfg["invalidation"]
    for f in earnings_8ks(data):
        if f["acc"] in es["seen"] or f["acc"] in es["fail"] or f["date"] <= inv["seed_after"]:
            continue
        try:
            text, link = ex991(cfg["cik"], f["acc"])
            r = parse_release(text, inv["guide_year"], inv["base_year"])
        except Exception as exc:  # noqa: BLE001
            es["fail"].append(f["acc"])
            out.append(ops_msg(stamp, "실적 8-K 파싱 실패 | 원문 확인 필요", f"{f['date']} 8-K {f['acc']} EX-99.1 읽기 {NA}: {str(exc)[:150]}. 이 접수번호는 다시 안내하지 않습니다."))
            continue
        if not r["q4"] and not r["has_guide"]:
            es["seen"].append(f["acc"])
            print(f"8-K {f['acc']}: {inv['base_year']} 4분기 실적 아님 (기록만)")
            continue
        j = judge_release(cfg, r)
        if j["svc"] is None or j["bill"] is None:
            es["fail"].append(f["acc"])
            miss = [x for x, k in (("2027 서비스 매출 가이던스 또는 2026 서비스 매출", "svc"), ("4분기 빌링(값 없음 또는 반올림 폭 안)", "bill")) if j[k] is None]
            out.append(ops_msg(stamp, "실적 8-K 파싱 실패 | 원문 확인 필요", f"{f['date']} 8-K {f['acc']} EX-99.1 에서 {', '.join(miss)} {NA}. 통과로 세지 않음. 이 접수번호는 다시 안내하지 않습니다. 원문 {link}"))
            continue
        es["seen"].append(f["acc"])
        t, m, pr, bad = earn_msg(cfg, f, r, j, link, stamp)
        if bad:
            st["invalid"] = {"acc": f["acc"], "date": f["date"], "svc_growth_pct": round(j["growth"], 2), "q4_billings_m": r["bill_m"]}
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
        assert t.startswith("[FTNT]") and "FTNT" in t[6:], t
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
# 과거 2월 실제 보도자료(SEC EX-99.1) 발췌 {guide_year: text}: 2026-02-05, 2025-02-06, 2024-02-06, 2023-02-07, 2022-02-03 공시
FEB_REL = {
    2026: "Reports Strong Fourth Quarter and Full Year 2025 Financial Results Fourth Quarter 2025 Hig ... REVENUE: Product $ 691.1 $ 574.0 $ 2,218.4 $ 1,908.7 Service 1,213.9 1,086.1 4,581.2 4,047.1 Total revenue 1,905.0 1,660.1 6,799.6 5,955.8 C ... Total billings $ 2,370.4 $ 2,002.5 $ 7,553.7 $ 6,532.5 1 A reconciliation of GAAP to non-GAAP measures has been provided in the fi ... For the fiscal year 2026, Fortinet currently expects: • Revenue in the range of $7.500 billion to $7.700 billion • Service revenue in the range of $5.050 billion to $5.150 billion • Billings",
    2025: "Reports Fourth Quarter and Full Year 2024 Financial Results Fourth Quarter 2024 Highlights ... REVENUE: Product $ 574.0 $ 488.1 $ 1,908.7 $ 1,927.3 Service 1,086.1 927.0 4,047.1 3,377.5 Total revenue 1,660.1 1,415.1 5,955.8 5,304.8 COS ... Total billings were $2.00 billion for the fourth quarter of 2024, an increase of 7.4% compared to $1.86 billion for the same quart ... For the fiscal year 2025, Fortinet currently expects: • Revenue in the range of $6.650 billion to $6.850 billion • Service revenue in the range of $4.575 billion to $4.725 billion • Billings",
    2024: "Reports Fourth Quarter and Full Year 2023 Financial Results Fourth Quarter 2023 Highlights ... REVENUE: Product $ 488.1 $ 540.1 $ 1,927.3 $ 1,780.5 Service 927.0 742.9 3,377.5 2,636.9 Total revenue 1,415.1 1,283.0 5,304.8 4,417.4 COST  ... Total billings were $1.86 billion for the fourth quarter of 2023, an increase of 8.5% compared to $1.72 billion for the same quart ... For the fiscal year 2024, Fortinet currently expects: • Revenue in the range of $5.715 billion to $5.815 billion • Service revenue in the range of $3.920 billion to $3.970 billion • Billings",
    2023: "Reports Fourth Quarter and Full Year 2022 Financial Results Fourth Quarter 2022 Highlights ... REVENUE: Product $ 540.1 $ 378.9 $ 1,780.5 $ 1,255.0 Service 742.9 584.7 2,636.9 2,087.2 Total revenue 1,283.0 963.6 4,417.4 3,342.2 COST OF ... Total billings were $1.72 billion for the fourth quarter of 2022, an increase of 31.6% compared to $1.31 billion for the same quar ... For the fiscal year 2023, Fortinet currently expects: • Revenue in the range of $5.370 billion to $5.430 billion • Service revenue in the range of $3.335 billion to $3.365 billion • Billings",
    2022: "Reports Fourth Quarter and Full Year 2021 Financial Results Fourth Quarter 2021 Highlights ... REVENUE: Product $ 378.9 $ 288.4 $ 1,255.0 $ 916.4 Service 584.7 459.6 2,087.2 1,678.0 Total revenue 963.6 748.0 3,342.2 2,594.4 COST OF REV ... Total billings were $1.31 billion for the fourth quarter of 2021, an increase of 35.9% compared to $960.9 million for the same qua ... For the fiscal year 2022, Fortinet currently expects: • Revenue in the range of $4.275 billion to $4.325 billion • Service revenue in the range of $2.685 billion to $2.715 billion • Billings",
}
# 기대값: (서비스 가이던스 중간값 B, 직전 연도 서비스 매출 M, 4분기 빌링 M, 출처)
FEB_WANT = {2026: (5.100, 4581.2, 2370.4, "표"), 2025: (4.650, 4047.1, 2000.0, "문장"), 2024: (3.945, 3377.5, 1860.0, "문장"),
            2023: (3.350, 2636.9, 1720.0, "문장"), 2022: (2.700, 2087.2, 1310.0, "문장")}
# 2027년 2월 형식 가상 (2026-02-05 표 형식 그대로, 숫자만 바꿈)
REL27 = ("Fortinet Reports Strong Fourth Quarter and Full Year 2026 Financial Results ... REVENUE: Product $ 900.0 $ 691.1 $ 3,083.1 $ 2,218.4 "
         "Service 1,360.0 1,213.9 5,152.8 4,581.2 Total revenue ... Total billings $ 2,850.0 $ 2,370.4 $ 9,600.0 $ 7,553.7 1 A reconciliation ... "
         "For the fiscal year 2027, Fortinet currently expects: • Revenue in the range of $9.000 billion to $9.200 billion "
         "• Service revenue in the range of $5.700 billion to $5.740 billion • Billings in the range of")


def selftest(cfg: dict) -> int:
    stamp = "2026-11-03 09:00 KST"
    st = seed_state(cfg, 136.08)
    assert all(v["armed"] for v in st["zones"].values()) and st["stop"]["armed"] and st["tp"]["armed"] and not st["z1_hit"], st
    print("seed 136.08: 모두 무장")
    # 재무장 경계: nochase 150*0.97=145.5 미만, z1 133*1.03=136.99 초과 또는 128*0.97=124.16 미만,
    # z2 121*1.03=124.63 초과, z3 107*1.03=110.21 초과, 손절 99.91 초과, 익절 164.90 미만
    seq = [
        (151.0, ["nochase"]), (155.0, []), (146.0, []), (145.0, []), (150.0, ["nochase"]),
        (132.0, ["z1"]), (129.0, []), (136.5, []), (130.0, []), (137.5, []), (131.0, ["z1"]),
        (160.0, []), (151.0, []),                        # 1차 뒤 추격 금지 꺼짐
        (120.0, ["z2"]), (115.0, []), (124.5, []), (118.0, []), (125.0, []), (119.0, ["z2"]),
        (106.0, ["z3"]), (101.0, []), (110.0, []), (104.0, []), (110.5, []), (103.0, ["z3"]),
        (96.9, ["stop"]), (90.0, []), (99.9, []), (96.0, []), (99.95, []), (96.5, ["stop"]),
        (97.0, []), (99.95, []),                        # 97 정확히는 미만 아님, 99.95 는 3차(100~107) 밖
        (170.0, ["tp"]), (180.0, []), (165.0, []), (171.0, []), (164.8, []), (170.5, ["tp"]),
    ]
    for p, want in seq:
        got = judge_price(cfg, st, p)
        assert got == want, (p, got, want, st)
    print(f"price seq ok: {len(seq)} 종가, 구간/손절/익절 각각 1번 발화와 3%(손절 99.91, 익절 164.90) 재무장 확인")
    st2 = seed_state(cfg, 136.08)
    assert judge_price(cfg, st2, 90.0) == ["stop"], st2
    print("gap 90: 구간 아래라 구간 알림 없음, 손절만")
    for p in (151.0, 130.0, 117.0, 104.0):
        k = zone_where(cfg, p)
        t, m, pr = price_msg(cfg, k, "2026-11-02", p, stamp, {})
        assert t.startswith("[FTNT] FTNT") and "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port", t
        assert "확인 불가" not in t and "조회 실패" not in t, t
        print(f"-- sample {k}\nTITLE: {t}\nPRIORITY: {pr}\n{m.splitlines()[2]}\n")
    for k, p in (("stop", 95.0), ("tp", 172.0)):
        t, m, pr = price_msg(cfg, k, "2026-11-02", p, stamp, {})
        assert t.startswith("[FTNT] FTNT") and route_topic("cwally-port", t) == "cwally-port" and "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"-- sample {k}\nTITLE: {t}\nPRIORITY: {pr}\n")
    # 과거 2월 실제 보도자료로 파서 확인 (연도만 바꿔 호출)
    for gy, rel in FEB_REL.items():
        r = parse_release(rel, gy, gy - 1)
        mid, base, bill, src = FEB_WANT[gy]
        assert r["q4"] and r["has_guide"], (gy, r)
        assert abs((r["svc_lo_b"] + r["svc_hi_b"]) / 2 - mid) < 1e-9 and r["svc_base_m"] == base, (gy, r)
        assert abs(r["bill_m"] - bill) < 1e-6 and r["bill_src"] == src, (gy, r)
        gr = (mid * 1000 / base - 1) * 100
        print(f"parse {gy} 2월 보도자료: 서비스 가이던스 중간값 {mid:.3f}B, 직전 연도 서비스 {base:,.1f}M -> {gr:+.1f}%, 4분기 빌링 {r['bill_m']:,.1f}M ({src})")
    # 2027 판정 (가상 숫자)
    f = {"acc": "0001262039-27-000003", "date": "2027-02-04"}
    cases = [
        ("ok", REL27, "ok", "ok", False),
        ("svc 8.6%", REL27.replace("$5.700 billion to $5.740 billion", "$5.580 billion to $5.620 billion"), "bad", "ok", True),
        ("svc +10.02% 경계 위", REL27.replace("$5.700 billion to $5.740 billion", "$5.662 billion to $5.676 billion"), "ok", "ok", False),
        ("svc +9.98% 경계 아래", REL27.replace("$5.700 billion to $5.740 billion", "$5.660 billion to $5.674 billion"), "bad", "ok", True),
        ("billings 2,700", REL27.replace("Total billings $ 2,850.0", "Total billings $ 2,700.0"), "ok", "bad", True),
        ("billings 2,775 경계", REL27.replace("Total billings $ 2,850.0", "Total billings $ 2,775.0"), "ok", "ok", False),
        ("billings 문장 2.70", REL27.replace("Total billings $ 2,850.0 $ 2,370.4", "Total billings were $2.70 billion for the fourth quarter of 2026"), "ok", "bad", True),
        ("billings 문장 2.78 (2,775~2,785)", REL27.replace("Total billings $ 2,850.0 $ 2,370.4", "Total billings were $2.78 billion for the fourth quarter of 2026"), "ok", "ok", False),
        ("billings 문장 2.77 반올림 폭 (2,765~2,775)", REL27.replace("Total billings $ 2,850.0 $ 2,370.4", "Total billings were $2.77 billion for the fourth quarter of 2026"), "ok", None, None),
        ("svc 없음", REL27.replace("Service revenue in the range", "Product revenue in the range"), None, "ok", None),
    ]
    for name, rel, wsvc, wbill, wbad in cases:
        r = parse_release(rel, 2027, 2026)
        j = judge_release(cfg, r)
        assert (j["svc"], j["bill"]) == (wsvc, wbill), (name, j, r)
        if wbad is None:
            print(f"judge {name}: 판정 불가 -> cwally-ops 1번 (통과로 세지 않음)")
            continue
        t, m, pr, bad = earn_msg(cfg, f, r, j, "https://www.sec.gov/x", stamp)
        assert bad == wbad and pr == (4 if bad else 3) and t.startswith("[FTNT] FTNT") and route_topic("cwally-port", t) == "cwally-port", (name, t)
        assert "\u00b7" not in t + m and "\u2014" not in t + m and "확인 불가" not in t and "조회 실패" not in t
        print(f"judge {name}: svc {j['growth']:+.2f}% bill {r['bill_m']:,.1f}M -> {t} (p{pr})")
    r = parse_release("Fortinet Reports First Quarter 2027 Financial Results ...", 2027, 2026)
    assert not r["q4"] and not r["has_guide"]
    st4 = seed_state(cfg, 136.08)
    st4["invalid"] = {"acc": f["acc"]}
    keys = judge_price(cfg, st4, 130.0)
    assert keys == ["z1"] and [k for k in keys if not (st4.get("invalid") and k in ("nochase", "z1", "z2", "z3"))] == []
    print("invalidated: 구간 알림 생략 확인, 4분기 아닌 실적은 기록만")
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
