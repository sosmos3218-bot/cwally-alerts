#!/usr/bin/env python3
"""[LRCX] 램리서치 분할 매수 구간 + 12월 분기 실적 무효화 점검. 평일 09:00 KST morning 묶음.

가격: 미국 전일 종가(Yahoo 일봉). 데이터 거래일(state.last_date)로 중복 방지.
구간(configs/lrcx.json zones): 들어오면 1번 알림, 경계 밖으로 rearm_pct(3%) 넘게 벗어난 뒤 다시 들어오면 다시 알림.
실적: 새 8-K Item 2.02 (seed 이후, 접수번호로 중복 방지) EX-99.1 에서 12월 분기 매출, 3월 분기 매출 가이던스,
  중국 매출 비중을 읽어 무효화(p4) 또는 요약(p3). 읽기 실패는 접수번호마다 cwally-ops 1번.
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
CFG_PATH = ROOT / "configs" / "lrcx.json"
STATE_PATH = ROOT / "state" / "lrcx.json"
SEC_UA = {"User-Agent": "cwally-alerts research monitor research@example.com"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"


def fetch(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------- 구간 판정 (재진입 규칙) ----------
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


def seed_zones(zones: list[dict], p: float) -> dict:
    return {z["key"]: {"armed": not in_zone(p, z)} for z in zones}


def zone_where(zones: list[dict], p: float) -> str:
    for z in zones:
        if in_zone(p, z):
            return z["key"]
    return "구간 밖"


# ---------- 본문 ----------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | LRCX | 램리서치 분할 매수 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def zone_msg(cfg: dict, z: dict, day: str, p: float, stamp: str) -> tuple[str, str, int]:
    pe = p / cfg["fy27_eps"]
    k = z["key"]
    if k == "nochase":
        title = f"[LRCX] 추격 금지선 돌파 | 종가 ${p:.2f} > ${z['lo']:g}, 신규 매수 보류"
        action = "신규 매수 보류. 이미 가진 물량은 이 알림으로 정하지 않기."
    elif k == "z1":
        title = f"[LRCX] 1차 분할 매수 구간 진입 | 종가 ${p:.2f} ({z['lo']:g}~{z['hi']:g}, FY27 P/E {pe:.1f}배)"
        action = "계획한 1차 분할만. 한 번에 다 사지 않기."
    else:
        n = {"z2": "2", "z3": "3"}[k]
        title = f"[LRCX] {n}차 분할 매수 구간 진입 | 종가 ${p:.2f} ({z['lo']:g}~{z['hi']:g})"
        action = f"계획한 {n}차 분할만. 실적 무효화 알림이 왔으면 멈추기."
    status = (f"{day} 미국 종가 ${p:.2f}, FY27 컨센서스 EPS ${cfg['fy27_eps']} 기준 P/E {pe:.1f}배. "
              f"구간: 추격 금지 ${cfg['zones'][0]['lo']:g} 이상, 1차 215~225, 2차 190~200, 3차 165~175.")
    msg = body(stamp, "램리서치 종가가 미리 정한 분할 매수 구간이나 추격 금지선에 들어왔다는 알림. 매매 지시가 아닙니다.", status, action,
               f"같은 구간은 종가가 경계 밖으로 {cfg['rearm_pct']:g}% 넘게 벗어났다가 다시 들어올 때만 다시 옵니다. 12월 분기 실적이 나오면 무효화 점검이 따로 옵니다.",
               "구간 알림만으로 비중을 줄이지 않기.", "추격 금지선 위에서 신규 매수, 무효화 뒤 분할 계속하기.")
    return title, msg, z["priority"]


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
    pick = [n for n in names if re.search(r"ex(hibit)?[-_x]?99[-_x.]?1|ex991", n, re.I) and n.lower().endswith((".htm", ".html"))]
    if not pick:
        raise RuntimeError("EX-99.1 파일 없음")
    return html_to_text(fetch(base + pick[0], SEC_UA)), base + pick[0]


def to_b(v: str, unit: str) -> float:
    x = float(v.replace(",", ""))
    return x / 1000 if unit.lower().startswith("m") else x


def parse_release(t: str) -> dict:
    out: dict = {}
    m = re.search(r"Results for the Quarter Ended (\w+) (\d{1,2}), (\d{4})", t)
    out["q_month"], out["q_year"] = (m.group(1), int(m.group(3))) if m else (None, None)
    m = re.search(r"(?:Revenue of|revenue was) \$([\d.,]+) (billion|million)", t, re.I)
    out["rev_b"] = to_b(m.group(1), m.group(2)) if m else None
    m = re.search(r"Outlook For the quarter ending (\w+) \d{1,2}, \d{4}.{0,200}?Revenue \$([\d.,]+) (Billion|Million) \+/- \$([\d.,]+) (Billion|Million)", t, re.I)
    if m:
        out["g_month"], out["g_mid_b"], out["g_pm_b"] = m.group(1), to_b(m.group(2), m.group(3)), to_b(m.group(4), m.group(5))
    m = re.search(r"Region Revenue China (\d{1,2}(?:\.\d)?)\s*%", t)
    out["china_pct"] = float(m.group(1)) if m else None
    return out


def earn_msg(cfg: dict, f: dict, r: dict, link: str, stamp: str) -> tuple[str, str, int]:
    floor = cfg["invalidation"]["dec_revenue_floor_b"]
    rev, mid = r["rev_b"], r.get("g_mid_b")
    china = f"{r['china_pct']:g}%" if r.get("china_pct") is not None else NA
    gtxt = f"${mid:.2f}B +/- ${r['g_pm_b']:.2f}B ({r.get('g_month')} 분기)" if mid is not None else NA
    bad_rev = rev < floor
    bad_g = mid is not None and mid < rev
    base = cfg["baseline"]["dec_quarter_guidance"]
    status = (f"{f['date']} 8-K(Item 2.02, {f['acc']}): 12월 분기 매출 ${rev:.2f}B (하단 ${floor:g}B, 회사 가이던스 ${base['revenue_b']}B +/- ${base['revenue_pm_b']}B), "
              f"3월 분기 매출 가이던스 {gtxt}, 중국 매출 비중 {china} (9월 분기 {cfg['baseline']['sep_quarter_2026']['china_pct']}%). 링크 {link}")
    if bad_rev or bad_g:
        why = []
        if bad_rev:
            why.append(f"12월 분기 매출 ${rev:.2f}B < ${floor:g}B")
        if bad_g:
            why.append(f"3월 가이던스 중간값 ${mid:.2f}B < 12월 매출 ${rev:.2f}B")
        title = "[LRCX] 무효화 | 12월 분기 매출 하단 미달 또는 3월 가이던스 역성장, 분할 매수 중단"
        return title, body(stamp, "램리서치 실적이 분할 매수 전제를 깼다는 알림.", status + ". 사유: " + ", ".join(why) + ".",
                           "남은 분할 매수 중단. 구간 알림이 와도 새로 사지 않기.", "다음 실적이나 가이던스 회복 때 다시 판단.",
                           "보유분 축소는 따로 정한 기준대로.", "무효화 뒤 구간 알림만 보고 분할 계속하기."), 4
    title = f"[LRCX] 12월 분기 실적 확인 | 매출 ${rev:.2f}B, 3월 가이던스 {('$%.2fB' % mid) if mid is not None else '미기재'}"
    return title, body(stamp, "램리서치 12월 분기 실적이 무효화 조건에 걸리지 않았다는 요약.", status + ". 무효화 조건 미해당.",
                       "분할 매수 계획 유지. 구간 알림을 기다리기.", "다음 실적 때 다시 점검.", "실적 하나로 비중을 크게 바꾸지 않기.",
                       "요약을 매수 신호로 읽기."), 3


def ops_msg(stamp: str, title_tail: str, status: str) -> tuple[str, str, int]:
    return (f"[LRCX] {title_tail}", body(stamp, "램리서치 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 원문을 직접 보세요.",
                                          "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


def earnings_check(cfg: dict, st: dict, data: dict, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    if "earn" not in st:  # 첫 실행: 지금까지 나온 실적 8-K 는 판정 없이 seed
        st["earn"] = {"seen": [f["acc"] for f in earnings_8ks(data)], "fail": [], "seeded_at": stamp}
        print(f"earn seeded: {len(st['earn']['seen'])} accessions")
        return out
    es = st["earn"]
    after = cfg["invalidation"].get("seed_after", "2026-10-26")
    for f in earnings_8ks(data):
        if f["acc"] in es["seen"] or f["acc"] in es["fail"] or f["date"] <= after:
            continue
        try:
            text, link = ex991(cfg["cik"], f["acc"])
            r = parse_release(text)
            if r["rev_b"] is None or r["q_month"] is None:
                raise RuntimeError("매출 또는 분기 표기를 못 읽음")
        except Exception as exc:  # noqa: BLE001
            es["fail"].append(f["acc"])
            out.append(ops_msg(stamp, "실적 8-K 파싱 실패 | 원문 확인 필요", f"{f['date']} 8-K {f['acc']} EX-99.1 읽기 실패: {str(exc)[:150]}. 이 접수번호는 다시 안내하지 않습니다."))
            continue
        es["seen"].append(f["acc"])
        if r["q_month"] != "December":
            print(f"8-K {f['acc']}: {r['q_month']} 분기, 12월 분기 아님 (기록만)")
            continue
        out.append(earn_msg(cfg, f, r, link, stamp))
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
            st["zones"] = seed_zones(cfg["zones"], p)
            st["last_date"] = day
            print(f"seeded zones at {day} ${p}: {st['zones']}")
        if day != st.get("last_date"):
            for z in judge_zones(cfg["zones"], st["zones"], p, cfg["rearm_pct"]):
                out.append(zone_msg(cfg, z, day, p, stamp))
            st["last_date"] = day
        st["last_close"] = [day, p]
        print(f"close {day} ${p:.2f} -> {zone_where(cfg['zones'], p)} (FY27 P/E {p / cfg['fy27_eps']:.1f}배), armed={ {k: v['armed'] for k, v in st['zones'].items()} }")
    elif st.get("quote_fail") != stamp[:10]:
        out.append(ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", f"Yahoo 일봉 {NA} ({qerr})."))
        st["quote_fail"] = stamp[:10]
    try:
        out += earnings_check(cfg, st, json.loads(fetch(cfg["sources"]["sec"], SEC_UA)), stamp)
    except Exception as exc:  # noqa: BLE001
        print(f"sec fail: {exc}")
        if st.get("sec_fail") != stamp[:10] and now.weekday() == 0:
            out.append(ops_msg(stamp, "SEC 조회 실패 | 실적 점검 못 함", f"SEC 공시 목록 {NA} ({str(exc)[:150]})."))
            st["sec_fail"] = stamp[:10]
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, ["chart_with_upwards_trend"], server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- selftest ----------
REL_OK = ("Lam Research Corporation Reports Financial Results for the Quarter Ended December 27, 2026 ... Revenue of $8.71 billion. ... "
          "Region Revenue China 33% Korea 21% ... Outlook For the quarter ending March 28, 2027, Lam is providing the following guidance: "
          "U.S. GAAP Reconciling Items Non-GAAP Revenue $8.90 Billion +/- $500 Million")
REL_BAD = REL_OK.replace("$8.71 billion", "$8.10 billion")
REL_DOWN = REL_OK.replace("$8.90 Billion", "$8.40 Billion")


def selftest(cfg: dict) -> int:
    zones, pct = cfg["zones"], cfg["rearm_pct"]
    stamp = "2026-11-03 09:00 KST"
    # 230 에서 seed: 모두 무장
    zst = seed_zones(zones, 230.0)
    assert all(v["armed"] for v in zst.values())
    seq = [(224.0, ["z1"]), (220.0, []), (228.0, []), (223.0, []), (232.0, []), (224.0, ["z1"]), (199.0, ["z2"]),
           (170.0, ["z3"]), (160.0, []), (172.0, ["z3"]), (181.0, []), (195.0, ["z2"]), (265.0, ["nochase"]), (270.0, []), (255.0, []), (262.5, []), (254.0, []), (263.0, ["nochase"])]
    for p, want in seq:
        got = [z["key"] for z in judge_zones(zones, zst, p, pct)]
        assert got == want, (p, got, want, zst)
    print("zone seq ok (재진입은 3% 넘게 벗어난 뒤만)")
    # z3 재진입: 170 진입 뒤 180.25(175*1.03) 넘게 올라갔다가 다시 들어와야
    zst = seed_zones(zones, 230.0)
    assert [z["key"] for z in judge_zones(zones, zst, 170.0, pct)] == ["z3"]
    assert judge_zones(zones, zst, 180.0, pct) == [] and judge_zones(zones, zst, 174.0, pct) == []
    assert judge_zones(zones, zst, 180.5, pct) == [] and [z["key"] for z in judge_zones(zones, zst, 174.0, pct)] == ["z3"]
    for p in (264.1, 221.37, 195.0, 168.0):
        z = next(z for z in zones if in_zone(p, z))
        t, m, pr = zone_msg(cfg, z, "2026-11-02", p, stamp)
        assert "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port", t
        print(f"-- sample\nTITLE: {t}\nPRIORITY: {pr}\n")
    f = {"acc": "0000707549-27-000005", "date": "2027-01-27"}
    for name, rel, want in (("ok", REL_OK, "실적 확인"), ("rev<8.2B", REL_BAD, "무효화"), ("guide down", REL_DOWN, "무효화")):
        r = parse_release(rel)
        assert r["q_month"] == "December" and r["china_pct"] == 33.0 and r["g_month"] == "March", r
        t, m, pr = earn_msg(cfg, f, r, "https://www.sec.gov/x", stamp)
        assert want in t and "확인 불가" not in t and "조회 실패" not in t, t
        print(f"-- earnings {name}\nTITLE: {t}\nPRIORITY: {pr}\n{m.splitlines()[2]}\n")
    t, _, _ = ops_msg(stamp, "실적 8-K 파싱 실패 | 원문 확인 필요", "x")
    assert route_topic("cwally-port", t) == "cwally-ops", t
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
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트 오류 | 이번 실행 판정 못 함", f"{type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
