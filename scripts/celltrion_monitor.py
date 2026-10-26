#!/usr/bin/env python3
"""[셀트리온] 068270 분할 매수 구간 + 3분기 분기보고서, 4분기 잠정실적 무효화 점검.

price  : 평일 15:40 KST (0090b0 슬롯에 부착). 네이버 수정주가 일봉(fchart requestType=0) 확정 종가.
         구간에 들어오면 1번 알림, 경계 밖으로 rearm_pct(3%) 넘게 벗어난 뒤 다시 들어오면 다시 알림. 데이터 거래일로 중복 방지.
filing : 평일 19:00 KST (k203780 체인에 부착).
         3분기 분기보고서(KIND, krhcfile_monitor 의 kind_list/Book/analyze 재사용) 3개월 단독 매출, 영업이익 vs 잠정. 1번.
         4분기 연결 영업(잠정)실적 공정공시(네이버 공시 본문) 매출 전년동기대비, 영업이익률. 공시마다 1번.
         표를 못 읽으면 추측하지 않고 cwally-ops 로 원문 확인 안내 1번.
usage: celltrion_monitor.py <auto|dry> <price|filing|all> | selftest
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "celltrion.json"
STATE_PATH = ROOT / "state" / "celltrion.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"
TAG = "[셀트리온]"


def fetch(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------- 구간 판정 (lrcx 와 같은 규칙) ----------
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
    return next((z["key"] for z in zones if in_zone(p, z)), "구간 밖")


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 068270 | 셀트리온 분할 매수 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def won(v: float) -> str:
    return f"{v:,.0f}"


def zone_msg(cfg: dict, z: dict, day: str, p: float, stamp: str) -> tuple[str, str, int]:
    k = z["key"]
    if k == "nochase":
        title = f"{TAG} 추격 금지선 돌파 | 종가 {won(p)}원 > {won(z['lo'])}원, 신규 매수 보류"
        action = "신규 매수 보류. 이미 가진 물량은 이 알림으로 정하지 않기."
    elif k == "z1":
        title = f"{TAG} 1차 분할 매수 구간 진입 | 종가 {won(p)}원 ({won(z['lo'])}~{won(z['hi'])}, 자사주 평균 {won(cfg['buyback_avg_price'])}원)"
        action = "계획한 1차 분할만. 한 번에 다 사지 않기."
    else:
        n = {"z2": "2", "z3": "3"}[k]
        title = f"{TAG} {n}차 분할 매수 구간 진입 | 종가 {won(p)}원 ({won(z['lo'])}~{won(z['hi'])})"
        action = f"계획한 {n}차 분할만. 무효화 알림이 왔으면 멈추기."
    status = (f"{day} 확정 종가 {won(p)}원 (수정주가). 회사 자사주 평균 매입가 {won(cfg['buyback_avg_price'])}원. "
              "구간: 추격 금지 200,000원 이상, 1차 181,000~185,000, 2차 173,000~176,000, 3차 159,000~168,000.")
    msg = body(stamp, "셀트리온 종가가 미리 정한 분할 매수 구간이나 추격 금지선에 들어왔다는 알림. 매매 지시가 아닙니다.", status, action,
               f"같은 구간은 종가가 경계 밖으로 {cfg['rearm_pct']:g}% 넘게 벗어났다가 다시 들어올 때만 다시 옵니다. 분기보고서와 4분기 잠정실적 점검은 19:00 에 따로 옵니다.",
               "구간 알림만으로 비중을 줄이지 않기.", "추격 금지선 위에서 신규 매수, 무효화 뒤 분할 계속하기.")
    return title, msg, z["priority"]


def ops_msg(stamp: str, tail: str, status: str) -> tuple[str, str, int]:
    return (f"{TAG} {tail}", body(stamp, "셀트리온 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 KIND 또는 DART 원문을 직접 보세요.",
                                   "다음 실행 때 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


# ---------- 가격 ----------
def price_rows(code: str) -> list[tuple[str, float]]:
    raw = fetch(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=10&requestType=0")
    out = []
    for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw):
        out.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)))
    return sorted(out)


def confirmed_close(rows: list[tuple[str, float]], now: datetime) -> tuple[str, float] | None:
    today = now.strftime("%Y-%m-%d")
    ok = [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]
    return ok[-1] if ok else None


def price_part(cfg: dict, st: dict, now: datetime, stamp: str) -> list[tuple[str, str, int]]:
    out = []
    try:
        lc = confirmed_close(price_rows(cfg["code"]), now)
        err = None if lc else "확정 종가 없음"
    except Exception as exc:  # noqa: BLE001
        lc, err = None, str(exc)[:150]
    if not lc:
        if st.get("price_fail") != stamp[:10]:
            st["price_fail"] = stamp[:10]
            out.append(ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", f"네이버 수정주가 일봉 {NA} ({err})."))
        return out
    day, p = lc
    if "zones" not in st:
        st["zones"], st["last_date"] = seed_zones(cfg["zones"], p), day
        print(f"seeded zones at {day} {p}: {st['zones']}")
    if day != st.get("last_date"):
        for z in judge_zones(cfg["zones"], st["zones"], p, cfg["rearm_pct"]):
            out.append(zone_msg(cfg, z, day, p, stamp))
        st["last_date"] = day
    st["last_close"] = [day, p]
    print(f"close {day} {won(p)}원 -> {zone_where(cfg['zones'], p)}, armed={ {k: v['armed'] for k, v in st['zones'].items()} }")
    return out


# ---------- 3분기 분기보고서 ----------
def q3_msg(cfg: dict, rep: dict, cur: tuple[float, float], link: str, stamp: str) -> tuple[str, str, int]:
    q = cfg["q3_report"]
    dr = (cur[0] / q["prelim_rev_eok"] - 1) * 100
    do = (cur[1] / q["prelim_op_eok"] - 1) * 100
    status = (f"{rep['title']} 제출 {rep['filed']} KST. 3분기 3개월 단독 확정: 매출 {won(cur[0])}억, 영업이익 {won(cur[1])}억 "
              f"(잠정 매출 {won(q['prelim_rev_eok'])}억, 영업이익 {won(q['prelim_op_eok'])}억). 잠정 대비 매출 {dr:+.1f}%, 영업이익 {do:+.1f}%. 링크 {link}")
    if min(dr, do) < q["invalidate_below_pct"]:
        return (f"{TAG} 무효화 | 확정치 잠정 대비 -10% 초과 또는 성장 둔화, 분할 매수 중단",
                body(stamp, "셀트리온 3분기 확정치가 분할 매수 전제를 깼다는 알림.", status, "남은 분할 매수 중단. 구간 알림이 와도 새로 사지 않기.",
                     "4분기 잠정실적 때 다시 판단.", "보유분 축소는 따로 정한 기준대로.", "무효화 뒤 구간 알림만 보고 분할 계속하기."), 4)
    return (f"{TAG} 3분기 분기보고서 확인 | 잠정 대비 매출 {dr:+.1f}%, 영업이익 {do:+.1f}%",
            body(stamp, "셀트리온 3분기 분기보고서 확정치를 잠정치와 비교한 결과. 무효화 조건(-10% 초과) 미해당.", status,
                 "분할 매수 계획 유지.", "4분기 잠정실적이 나오면 다시 점검합니다.", "보고서 하나로 비중을 크게 바꾸지 않기.", "확인 알림을 매수 신호로 읽기."), 3)


def q3_part(cfg: dict, st: dict, now: datetime, stamp: str) -> list[tuple[str, str, int]]:
    import krhcfile_monitor as kh
    qs = st.setdefault("q3", {"done": None, "fail": []})
    if qs.get("done"):
        return []
    co = {"code": cfg["code"], "name_kr": cfg["name_kr"]}
    try:
        reps = kh.kind_list(co, (now - timedelta(days=500)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001
        if st.get("kind_fail") != stamp[:10]:
            st["kind_fail"] = stamp[:10]
            return [ops_msg(stamp, "분기보고서 목록 조회 실패 | 공시 판정 못 함", f"KIND 정기보고서 목록 {NA} ({str(exc)[:150]}).")]
        return []
    tgt = [r for r in reps if r["period"] == cfg["q3_report"]["period"] and r["kind"] == "분기보고서"]
    print("q3 report:", [(r["acptno"], r["title"], r["filed"]) for r in tgt] or "아직 없음")
    if not tgt:
        return []
    rep = sorted(tgt, key=lambda r: r["acptno"])[0]
    if rep["acptno"] in qs["fail"]:
        return []
    try:
        res = kh.analyze(rep, kh.Book(reps), 0.5)
        cur = res["cur"]
        if not (cur[0] > 0):
            raise RuntimeError("매출 0 이하")
    except Exception as exc:  # noqa: BLE001
        qs["fail"].append(rep["acptno"])
        return [ops_msg(stamp, "3분기 분기보고서 파싱 실패 | 원문 확인 필요",
                        f"{rep['title']} 제출 {rep['filed']} KST, 표 읽기 실패 ({str(exc)[:150]}). 잠정(매출 15,000억, 영업이익 5,100억)과 직접 비교하세요. KIND 접수번호 {rep['acptno']}.")]
    qs["done"] = {"acptno": rep["acptno"], "rev": cur[0], "op": cur[1], "at": stamp}
    return [q3_msg(cfg, rep, cur, kh.link_for(co, rep), stamp)]


# ---------- 4분기 잠정실적 ----------
PRE_ROW = r"{name}\s*당해실적\s*([\-\d,.]+)\s+([\-\d,.]+)\s+([\-\d,.]+)\s+\S+\s+([\-\d,.]+)\s+([\-\d,.]+)"


def fnum(s: str) -> float | None:
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_prelim(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text)
    per = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    rv = re.search(PRE_ROW.format(name="매출액"), t)
    op = re.search(PRE_ROW.format(name="영업이익"), t)
    if not (per and rv and op):
        return None
    out = {"start": per.group(1), "end": per.group(2), "rev": fnum(rv.group(1)), "rev_yoy_base": fnum(rv.group(4)),
           "rev_yoy": fnum(rv.group(5)), "op": fnum(op.group(1)), "op_yoy_base": fnum(op.group(4))}
    if out["rev"] is None or out["op"] is None or not out["rev"]:
        return None
    if out["rev_yoy"] is None and out["rev_yoy_base"]:
        out["rev_yoy"] = (out["rev"] / out["rev_yoy_base"] - 1) * 100
    out["margin"] = out["op"] / out["rev"] * 100
    return out


def q4_msg(cfg: dict, n: dict, r: dict, stamp: str) -> tuple[str, str, int]:
    q = cfg["q4_prelim"]
    yoy = f"{r['rev_yoy']:+.1f}%" if r.get("rev_yoy") is not None else NA
    status = (f"{n['datetime'][:16].replace('T', ' ')} {n['title']}. 4분기({r['start']}~{r['end']}) 잠정 매출 {won(r['rev'])}억 "
              f"(전년동기 {won(r['rev_yoy_base']) if r.get('rev_yoy_base') else NA}억, {yoy}), 영업이익 {won(r['op'])}억, 영업이익률 {r['margin']:.1f}%. "
              f"기준: 매출 전년동기대비 {q['min_rev_yoy_pct']:g}% 이상, 영업이익률 {q['min_op_margin_pct']:g}% 이상. 원문 {n.get('url') or NA}")
    bad = (r.get("rev_yoy") is not None and r["rev_yoy"] < q["min_rev_yoy_pct"]) or r["margin"] < q["min_op_margin_pct"]
    if bad:
        return (f"{TAG} 무효화 | 확정치 잠정 대비 -10% 초과 또는 성장 둔화, 분할 매수 중단",
                body(stamp, "셀트리온 4분기 잠정실적이 성장 둔화 조건에 걸렸다는 알림.", status + ". 성장 둔화 조건 해당.",
                     "남은 분할 매수 중단. 구간 알림이 와도 새로 사지 않기.", "다음 실적 때 다시 판단.", "보유분 축소는 따로 정한 기준대로.",
                     "무효화 뒤 구간 알림만 보고 분할 계속하기."), 4)
    return (f"{TAG} 4분기 잠정실적 확인 | 매출 전년비 {yoy}, 영업이익률 {r['margin']:.1f}%",
            body(stamp, "셀트리온 4분기 잠정실적이 무효화 조건에 걸리지 않았다는 요약.", status + ". 무효화 조건 미해당.",
                 "분할 매수 계획 유지.", "알림 종료일 2027-02-27 까지 구간 알림은 계속 옵니다.", "잠정치 하나로 비중을 크게 바꾸지 않기.",
                 "확인 알림을 매수 신호로 읽기."), 3)


def q4_part(cfg: dict, st: dict, stamp: str) -> list[tuple[str, str, int]]:
    import naver_api
    qs = st.setdefault("q4", {"seen": [], "fail": []})
    try:
        notes = [n for n in naver_api.notices(cfg["code"], 40) if "잠정" in n["title"] and "실적" in n["title"]]
    except Exception as exc:  # noqa: BLE001
        print(f"notices fail: {exc}")
        return []
    if "seeded_at" not in qs:
        qs["seen"] = sorted({f"{n['datetime']}|{n['title']}" for n in notes})
        qs["seeded_at"] = stamp
        print(f"q4 seeded {len(qs['seen'])} notices")
        return []
    out = []
    for n in sorted(notes, key=lambda n: n["datetime"]):
        key = f"{n['datetime']}|{n['title']}"
        if key in qs["seen"] or key in qs["fail"]:
            continue
        r = parse_prelim(n.get("text") or "")
        if r is None:
            qs["fail"].append(key)
            out.append(ops_msg(stamp, "잠정실적 파싱 실패 | 원문 확인 필요", f"{n['datetime'][:16]} {n['title']} 본문 표 읽기 실패. 매출 전년비와 영업이익률을 직접 확인하세요."))
            continue
        qs["seen"].append(key)
        if r["start"] == cfg["q4_prelim"]["period_start"] and r["end"] == cfg["q4_prelim"]["period_end"]:
            out.append(q4_msg(cfg, n, r, stamp))
        else:
            print(f"prelim {key}: 기간 {r['start']}~{r['end']} (4분기 아님, 기록만)")
    return out


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, part: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    trading = is_trading_day(now.date())
    print(f"now={stamp} part={part} trading={trading} mode={mode}")
    if not trading and not dry:
        print("not a KRX trading day: skip")
        return 0
    st = load_state()
    out: list[tuple[str, str, int]] = []
    if part in ("price", "all"):
        out += price_part(cfg, st, now, stamp)
    if part in ("filing", "all"):
        out += q3_part(cfg, st, now, stamp)
        out += q4_part(cfg, st, stamp)
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
PRE_Q4 = ("연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-10-01 ~ 2026-12-31 전기실적 2026-07-01 ~ 2026-09-30 "
          "1. 연결실적내용 단위 : 억원, % 구분 당기실적 전기실적 전기대비 전년동기실적 전년동기대비 (26.4Q) (26.3Q) 증감율(%) 흑자적자전환여부 (25.4Q) 증감율(%) "
          "흑자적자전환여부 매출액 당해실적 16,200 15,000 8.00 - 12,000 35.00 - 누계실적 56,587 - - - 40,323 40.33 - "
          "영업이익 당해실적 5,500 5,100 7.84 - 3,500 57.14 - 누계실적 18,337 - - - 10,433 75.76 -")


def selftest(cfg: dict) -> int:
    zones, pct = cfg["zones"], cfg["rearm_pct"]
    stamp = "2026-11-03 15:40 KST"
    zst = seed_zones(zones, 188200)
    assert all(v["armed"] for v in zst.values())
    seq = [(184000, ["z1"]), (182000, []), (188000, []), (183000, []), (191000, []), (183500, ["z1"]), (175000, ["z2"]),
           (165000, ["z3"]), (160000, []), (154000, []), (162000, ["z3"]), (201000, ["nochase"]), (195000, []), (199000, []), (193000, []), (200500, ["nochase"])]
    for p, want in seq:
        got = [z["key"] for z in judge_zones(zones, zst, p, pct)]
        assert got == want, (p, got, want)
    print("zone seq ok (재진입은 3% 넘게 벗어난 뒤만)")
    for p in (201500, 183000, 174500, 160500):
        z = next(z for z in zones if in_zone(p, z))
        t, m, pr = zone_msg(cfg, z, "2026-11-02", p, stamp)
        assert route_topic("cwally-port", t) == "cwally-port" and "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"-- sample\nTITLE: {t}\nPRIORITY: {pr}\n")
    rep = {"title": "분기보고서(일반법인) (2026.09)", "filed": "2026-11-14 16:50", "acptno": "20261114000001"}
    for name, cur, want in (("ok", (14800.0, 4950.0), "3분기 분기보고서 확인"), ("op -12%", (14900.0, 4480.0), "무효화")):
        t, m, pr = q3_msg(cfg, rep, cur, "https://dart.fss.or.kr/x", stamp)
        assert want in t, t
        print(f"-- q3 {name}\nTITLE: {t}\nPRIORITY: {pr}\n{m.splitlines()[2]}\n")
    n = {"datetime": "2027-02-05T07:00:00", "title": "(주)셀트리온 연결재무제표기준영업(잠정)실적(공정공시)", "url": "https://m.stock.naver.com/x"}
    r = parse_prelim(PRE_Q4)
    assert r and r["rev"] == 16200 and r["rev_yoy"] == 35.0 and abs(r["margin"] - 33.95) < 0.01, r
    for name, txt, want in (("ok", PRE_Q4, "4분기 잠정실적 확인"),
                            ("yoy 10%", PRE_Q4.replace("12,000 35.00", "14,727 10.00"), "무효화"),
                            ("margin 20%", PRE_Q4.replace("영업이익 당해실적 5,500", "영업이익 당해실적 3,240"), "무효화")):
        t, m, pr = q4_msg(cfg, n, parse_prelim(txt), stamp)
        assert want in t and "확인 불가" not in t and "조회 실패" not in t, t
        print(f"-- q4 {name}\nTITLE: {t}\nPRIORITY: {pr}\n{m.splitlines()[2]}\n")
    assert parse_prelim("본문 없음") is None
    for tail in ("3분기 분기보고서 파싱 실패 | 원문 확인 필요", "잠정실적 파싱 실패 | 원문 확인 필요", "종가 조회 실패 | 구간 판정 못 함"):
        assert route_topic("cwally-port", ops_msg(stamp, tail, "x")[0]) == "cwally-ops", tail
    rows = [("2026-11-02", 183000.0), ("2026-11-03", 179000.0)]
    assert confirmed_close(rows, datetime(2026, 11, 3, 15, 20, tzinfo=KST)) == ("2026-11-02", 183000.0)
    assert confirmed_close(rows, datetime(2026, 11, 3, 15, 40, tzinfo=KST)) == ("2026-11-03", 179000.0)
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    part = (args[1] if len(args) > 1 else "all").lower()
    if mode not in {"auto", "dry"} or part not in {"price", "filing", "all"}:
        print("usage: celltrion_monitor.py <auto|dry> <price|filing|all> | selftest")
        return 2
    now = datetime.now(KST)
    try:
        return run(mode, part, now, cfg)
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
