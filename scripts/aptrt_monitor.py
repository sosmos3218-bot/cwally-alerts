#!/usr/bin/env python3
"""[APT-RT] 관심 아파트 매매 실거래 주간 요약. 기존 평일 09:00 morning 슬롯, 월요일 1번(놓치면 그 주 첫 평일 슬롯).
출처: 국토교통부 아파트 매매 실거래가 API(data.go.kr, secret DATA_GO_KR_KEY). 이번 달 + 지난 달 계약분을 읽고
configs/aptrt.json complexes(법정동 + 지번 + 전용면적 구간)만 남김. 새 신고는 거래 키(단지|계약일|층|면적|금액)로 1번.
메시지: p3 주간 묶음 1건(계약일, 층, 금액, 같은 면적 직전 거래 대비, 12개월 최고가, 신고가 표시). 해제는 표시만, 비교와 최고가에서 뺌.
전세(2026-11-02 추가): 전월세 API(RTMSDataSvcAptRent) 같은 단지, 같은 면적, monthlyRent == 0 만. 계약일, 층, 보증금, 신규/갱신(종전 보증금).
단지별 전세가율 = 최근 3개월 전세 보증금 중앙값 / 최근 3개월 매매 중앙값. 월세는 무시. 전세 첫 실행은 이력만.
전세 API 실패는 cwally-ops 로 따로 알리고 매매 요약은 그대로 보냄.
첫 실행은 지난 12개월 + 이번 달을 기준과 이력으로 기록만. 키 없음, API 실패는 cwally-ops 하루 1번, 그 주 다음 평일 슬롯에서 다시 시도.
modes: auto | dry(발송 없음, state 안 씀; --as-of "YYYY-MM-DD HH:MM", --force-week) | install | selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[APT-RT]"
NAME = "aptrt"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
MAX_BYTES = 3800


# ---------- fetch ----------

def _tag(it: str, name: str) -> str:
    m = re.search(rf"<{name}>([^<]*)</{name}>", it)
    return (m.group(1) if m else "").strip()


def parse_xml(xml: str) -> tuple[list[dict], int]:
    code = _tag(xml, "resultCode")
    if code and code not in ("000", "00"):
        raise ValueError(f"API resultCode {code} {_tag(xml, 'resultMsg')[:60]}")
    if "<OpenAPI_ServiceResponse>" in xml or "returnReasonCode" in xml:
        raise ValueError(f"API 오류 {_tag(xml, 'returnAuthMsg') or _tag(xml, 'errMsg')}"[:120])
    if "<response>" not in xml:
        raise ValueError("API 응답 형식 아님")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, flags=re.S):
        try:
            d = date(int(_tag(it, "dealYear")), int(_tag(it, "dealMonth")), int(_tag(it, "dealDay")))
            out.append({"date": d.isoformat(), "apt": _tag(it, "aptNm"), "umd": _tag(it, "umdNm"), "jibun": _tag(it, "jibun"),
                        "area": float(_tag(it, "excluUseAr")), "floor": _tag(it, "floor"),
                        "amount": int(_tag(it, "dealAmount").replace(",", "") or 0),
                        "deposit": int(_tag(it, "deposit").replace(",", "") or 0),
                        "rent": int(_tag(it, "monthlyRent").replace(",", "") or 0),
                        "ctype": _tag(it, "contractType"), "pre": int(_tag(it, "preDeposit").replace(",", "") or 0),
                        "cancel": _tag(it, "cdealType") == "O", "cday": _tag(it, "cdealDay"),
                        "gbn": _tag(it, "dealingGbn")})
        except ValueError:
            continue
    total = int(_tag(xml, "totalCount") or 0)
    return out, total


def fetch_month(api: str, key: str, lawd: str, ym: str) -> list[dict]:
    rows, page = [], 1
    while True:
        q = urllib.parse.urlencode({"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ym, "numOfRows": "1000", "pageNo": str(page)})
        last = None
        for i in range(4):
            try:
                req = urllib.request.Request(f"{api}?{q}", headers={"User-Agent": "Mozilla/5.0 (cwally-alerts)"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    xml = r.read().decode("utf-8", errors="ignore")
                break
            except Exception as exc:  # noqa: BLE001
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="ignore")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
                if "SERVICE_KEY" in body or "returnReasonCode" in body:
                    raise ValueError(f"API 키 오류 {_tag(body, 'errMsg') or _tag(body, 'returnAuthMsg')}") from None
                last = f"{type(exc).__name__}"  # 키가 든 URL 은 남기지 않음
                time.sleep(3 + 3 * i)
        else:
            raise ValueError(f"{lawd} {ym} 조회 실패({last})")
        got, total = parse_xml(xml)
        rows += got
        if page * 1000 >= total or not got:
            return rows
        page += 1


def months_back(today: date, n: int) -> list[str]:
    y, m, out = today.year, today.month, []
    for _ in range(n + 1):
        out.append(f"{y}{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def match(cfg: dict, rows: list[dict], lawd: str) -> dict[str, dict]:
    out = {}
    for c in cfg["complexes"]:
        if lawd not in c["lawd"]:
            continue
        for r in rows:
            if r["umd"] == c["umd"] and r["jibun"] == c["jibun"] and c["area_min"] <= r["area"] < c["area_max"]:
                k = f"{c['key']}|{r['date']}|{r['floor']}|{r['area']:.4f}|{r['amount']}"
                out[k] = {**r, "cx": c["key"]}
    return out


def match_rent(cfg: dict, rows: list[dict], lawd: str) -> tuple[dict[str, dict], dict[str, int]]:
    """(전세 dict, 단지별 월세 건수). 전세 = monthlyRent 0, 보증금 > 0."""
    out, wol = {}, {}
    for c in cfg["complexes"]:
        if lawd not in c["lawd"]:
            continue
        for r in rows:
            if r["umd"] == c["umd"] and r["jibun"] == c["jibun"] and c["area_min"] <= r["area"] < c["area_max"] and r["deposit"] > 0:
                if r["rent"] == 0:
                    k = f"{c['key']}|R|{r['date']}|{r['floor']}|{r['area']:.4f}|{r['deposit']}|{r['ctype']}"
                    out[k] = {"cx": c["key"], "date": r["date"], "floor": r["floor"], "area": r["area"], "deposit": r["deposit"],
                              "ctype": r["ctype"], "pre": r["pre"]}
                else:
                    wk = f"{c['key']}|{r['date']}|{r['floor']}|{r['area']:.4f}|{r['deposit']}|{r['rent']}"
                    wol[wk] = wol.get(wk, 0) + 1
    return out, wol


def fetch_rent(cfg: dict, key: str, yms: list[str], fetch=fetch_month) -> tuple[dict[str, dict], dict[str, str]]:
    lawds = sorted({x for c in cfg["complexes"] for x in c["lawd"]})
    out: dict[str, dict] = {}
    wol: dict[str, str] = {}
    for lw in lawds:
        for ym in yms:
            o, w = match_rent(cfg, fetch(cfg["rent_api"], key, lw, ym), lw)
            out.update(o)
            wol.update({k: k.split("|")[1] for k in w})
    return out, wol


def fetch_all(cfg: dict, key: str, yms: list[str], fetch=fetch_month) -> dict[str, dict]:
    lawds = sorted({x for c in cfg["complexes"] for x in c["lawd"]})
    out: dict[str, dict] = {}
    for lw in lawds:
        for ym in yms:
            out.update(match(cfg, fetch(cfg["api"], key, lw, ym), lw))
    return out


# ---------- evaluate ----------

def won(man: int) -> str:
    eok, rest = divmod(int(man), 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}만원"
    return f"{eok}억원" if eok else f"{rest:,}만원"


def diff(man: int) -> str:
    s = "+" if man > 0 else "-" if man < 0 else ""
    return "변동 없음" if man == 0 else s + won(abs(man))


def evaluate(cfg: dict, deals: dict, fresh: dict) -> tuple[list[dict], list[dict]]:
    """deals(state, 이력) 와 이번에 읽은 fresh 비교. (새 거래 목록, 새 해제 목록). deals 를 고침."""
    new, cancels = [], []
    for k in sorted(fresh, key=lambda x: (fresh[x]["date"], x)):
        r = fresh[k]
        old = deals.get(k)
        if old is None:
            rec = {x: r[x] for x in ("cx", "date", "floor", "area", "amount", "cancel", "cday")}
            hist = [v for kk, v in deals.items() if v["cx"] == r["cx"] and not v.get("cancel") and v["date"] <= r["date"]]
            prev = max(hist, key=lambda v: v["date"], default=None)
            d0 = (date.fromisoformat(r["date"]) - timedelta(days=365)).isoformat()
            yr = [v["amount"] for v in hist if v["date"] >= d0]
            hi = max(yr) if yr else None
            deals[k] = rec
            item = {**rec, "key": k, "prev": prev, "high": hi, "record": (not r["cancel"]) and hi is not None and r["amount"] > hi}
            (cancels if r["cancel"] else new).append({**item, "late_cancel": False} if r["cancel"] else item)
        elif r["cancel"] and not old.get("cancel"):
            old["cancel"], old["cday"] = True, r["cday"]
            cancels.append({**old, "key": k, "late_cancel": True})
    return new, cancels


def evaluate_rent(rents: dict, fresh: dict) -> list[dict]:
    new = []
    for k in sorted(fresh, key=lambda x: (fresh[x]["date"], x)):
        if k not in rents:
            rents[k] = fresh[k]
            new.append({**fresh[k], "key": k})
    return new


def median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def jeonse_ratio(cx: str, deals: dict, rents: dict, today: date) -> dict:
    d3 = (today - timedelta(days=91)).isoformat()
    js = [v["deposit"] for v in rents.values() if v["cx"] == cx and v["date"] >= d3]
    ms = [v["amount"] for v in deals.values() if v["cx"] == cx and not v.get("cancel") and v["date"] >= d3]
    mj, mm = median(js), median(ms)
    return {"jeonse": mj, "nj": len(js), "sale": mm, "ns": len(ms), "ratio": (mj / mm * 100) if mj and mm else None}


def ratio_text(r: dict) -> str:
    j = f"전세 중앙 {won(int(r['jeonse']))}({r['nj']}건)" if r["jeonse"] else "전세 3개월 거래 없음"
    m = f"매매 중앙 {won(int(r['sale']))}({r['ns']}건)" if r["sale"] else "매매 3개월 거래 없음"
    return f"{j}, {m}, 전세가율 " + (f"{r['ratio']:.1f}%" if r["ratio"] else "계산 불가")


def prune(deals: dict, today: date, months: int) -> None:
    cut = (today - timedelta(days=31 * (months + 1))).isoformat()
    for k in [k for k, v in deals.items() if v["date"] < cut]:
        deals.pop(k)


# ---------- messages ----------

def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 아파트 실거래 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "국토교통부 실거래가에 새로 신고된 관심 단지 매매와 전세(주 1회 묶음). 호가 아님, 매매 신호 아님."


def line(e: dict) -> str:
    s = f"{e['date'][5:].replace('-', '/')} {e['floor']}층 {e['area']:.2f}㎡ {won(e['amount'])}"
    p = e.get("prev")
    s += f", 직전 {p['date'][5:].replace('-', '/')} {won(p['amount'])} 대비 {diff(e['amount'] - p['amount'])}" if p else ", 직전 거래 없음"
    s += f", 12개월 최고 {won(e['high'])}" if e.get("high") else ", 12개월 최고 없음"
    return s + (" [신고가]" if e.get("record") else "")


def rline(e: dict) -> str:
    s = f"{e['date'][5:].replace('-', '/')} {e['floor']}층 {won(e['deposit'])}"
    if e.get("ctype"):
        s += f" {e['ctype']}"
        if e["ctype"] == "갱신" and e.get("pre"):
            s += f"(종전 {won(e['pre'])})"
    return s


def summary(cfg: dict, new: list[dict], cancels: list[dict], stamp: str, rnew: list[dict] | None = None,
            ratios: dict | None = None, wol: dict | None = None, rent_note: str = "") -> tuple[str, str] | None:
    rnew = rnew or []
    if not new and not cancels and not rnew:
        return None
    rec = sum(1 for e in new if e.get("record"))
    lim = cfg.get("max_lines_per_complex", 6)

    def build(lim: int) -> str:
        parts = []
        for c in cfg["complexes"]:
            ns = [e for e in new if e["cx"] == c["key"]]
            cs = [e for e in cancels if e["cx"] == c["key"]]
            rs = [e for e in rnew if e["cx"] == c["key"]]
            if not ns and not cs and not rs:
                continue
            seg = [f"[{c['name']} {c['area_label']}] 매매 새 신고 {len(ns)}건"]
            seg += [line(e) for e in sorted(ns, key=lambda x: x["date"], reverse=True)[:lim]]
            if len(ns) > lim:
                seg.append(f"매매 외 {len(ns) - lim}건")
            seg += [f"해제: {e['date'][5:].replace('-', '/')} {e['floor']}층 {won(e['amount'])}"
                    + (f" (해제일 {e['cday']})" if e.get("cday") else "") + ", 비교와 최고가에서 뺌" for e in cs]
            if rs:
                seg.append(f"전세 새 신고 {len(rs)}건: " + ", ".join(rline(e) for e in sorted(rs, key=lambda x: x["date"], reverse=True)[:lim])
                           + (f" 외 {len(rs) - lim}건" if len(rs) > lim else ""))
            parts.append(" / ".join(seg))
        st = " || ".join(parts)
        if ratios:
            st += " || 전세가율(최근 3개월 중앙값): " + "; ".join(f"{c['name']} {ratio_text(ratios[c['key']])}" for c in cfg["complexes"] if c["key"] in ratios)
        if rent_note:
            st += f" || {rent_note}"
        return st

    title = (f"{TAG} 주간 실거래 매매 {len(new)}건, 전세 {len(rnew)}건" + (f", 신고가 {rec}건" if rec else "")
             + (f", 해제 {len(cancels)}건" if cancels else "") + f" | {cfg['index_ticker']}")
    later = "다음 주 월요일 09:00 에 이번 달 + 지난 달 신고분을 다시 봅니다(신고 기한 30일이라 늦은 신고도 잡힘)."
    avoid = "한두 건 거래만 보고 시세를 단정하기, 해제 거래를 시세로 보기."
    for l in (lim, 3, 1):
        st = build(l)
        msg = body(stamp, "주간 요약", WHAT, st, "확인만 하세요. 관심 단지 시세 흐름 참고용.", later, "해당 없음.", avoid)
        if len(msg.encode("utf-8")) <= MAX_BYTES:
            return title, msg
    msg = body(stamp, "주간 요약", WHAT, st[:900] + " (길이 제한으로 일부 생략)", "확인만 하세요.", later, "해당 없음.", avoid)
    return title, msg


def ops_msg(cfg: dict, stamp: str, status: str, what: str = "매매") -> tuple[str, str]:
    return (f"{TAG} {what} 실거래 조회 실패 | {cfg['index_ticker']} | " + ("이번 주 판정 보류" if what == "매매" else "매매 요약은 그대로"),
            body(stamp, "조회 실패", "관심 아파트 실거래 주간 요약의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "data.go.kr 키(secret DATA_GO_KR_KEY)와 API 상태를 확인하세요.",
                 "그 주 다음 평일 09:00 슬롯에서 다시 시도합니다.", "해당 없음.", "이 안내로 결정 바꾸기."))


def install_message(cfg: dict, stamp: str) -> tuple[str, str]:
    names = ", ".join(f"{c['name']} {c['area_label']}" for c in cfg["complexes"])
    return (f"{TAG} 설치 확인 | {cfg['index_ticker']} | 매매 신호 아님",
            body(stamp, "설치 확인", "관심 아파트 매매 실거래 주간 요약 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 f"매주 월요일 09:00 KST 에 국토교통부 실거래가(data.go.kr) 매매와 전세(월세 제외) 이번 달 + 지난 달 신고분을 읽어 새 거래만 묶어 p3 1건. 단지: {names}.",
                 "알림이 도착했는지 확인만 하세요.",
                 "매매: 직전 같은 면적 거래 대비, 12개월 최고가, 신고가 표시, 해제는 표시만. 전세: 보증금, 신규/갱신, 단지별 전세가율(3개월 중앙값). 2027-03-31 09:00 에 끝납니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


# ---------- run ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def run(mode: str, now: datetime, cfg: dict, pub=publish, fetch=fetch_month, key: str | None = None, force_week: bool = False) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(cfg, stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    today = now.date()
    if now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"]:
        print(f"감시 끝({cfg['ends_kst']}): 조용히 종료")
        return 0
    st = load_state()
    wk = week_label(today)
    if today.weekday() >= 5 or today.isoformat() < cfg["window_start"]:
        print("주말 또는 시작 전: 조용히 종료")
        return 0
    if st.get("last_week") == wk and not force_week:
        print(f"{wk} 이미 처리: 조용히 종료")
        return 0
    key = key if key is not None else os.environ.get("DATA_GO_KR_KEY", "")
    seeding = "deals" not in st
    out, ops = None, None
    try:
        if not key:
            raise ValueError("DATA_GO_KR_KEY secret 없음")
        yms = months_back(today, cfg["history_months"] if seeding else 1)
        fresh = fetch_all(cfg, key, yms, fetch)
        print(f"읽은 달 {yms[-1]}~{yms[0]}, 관심 단지 해당 거래 {len(fresh)}건")
        for k in sorted(fresh, key=lambda x: fresh[x]["date"], reverse=True)[:12]:
            r = fresh[k]
            print(f"  {r['cx']} {r['date']} {r['floor']}층 {r['area']}㎡ {won(r['amount'])}{' 해제' if r['cancel'] else ''}")
        if seeding:
            st = {"deals": {}, "seeded_at": stamp}
            evaluate(cfg, st["deals"], fresh)
            print(f"first run: 기준과 이력만 기록, 알림 없음 ({len(st['deals'])}건)")
            new, cancels = [], []
        else:
            new, cancels = evaluate(cfg, st["deals"], fresh)
        # 전세 (실패해도 매매는 진행)
        rnew, wol, rent_note, ratios = [], {}, "", None
        try:
            rseed = "rent" not in st
            rf, wol = fetch_rent(cfg, key, months_back(today, cfg.get("rent_history_months", 6) if rseed else 1), fetch)
            print(f"전세 해당 거래 {len(rf)}건, 월세 {len(wol)}건")
            for k in sorted(rf, key=lambda x: rf[x]["date"], reverse=True)[:8]:
                r = rf[k]
                print(f"  {r['cx']} {r['date']} {r['floor']}층 {r['area']}㎡ 전세 {won(r['deposit'])} {r['ctype']}")
            if rseed:
                st["rent"] = {}
                evaluate_rent(st["rent"], rf)
                wol = {}
                print(f"전세 첫 실행: 이력만 기록 ({len(st['rent'])}건)")
            else:
                rnew = evaluate_rent(st["rent"], rf)
            cut = (today - timedelta(days=31 * (cfg.get("rent_history_months", 6) + 1))).isoformat()
            st["rent"] = {k: v for k, v in st["rent"].items() if v["date"] >= cut}
            ratios = {c["key"]: jeonse_ratio(c["key"], st["deals"], st["rent"], today) for c in cfg["complexes"]}
            for c in cfg["complexes"]:
                print(f"  전세가율 {c['name']}: {ratio_text(ratios[c['key']])}")
        except Exception as exc:  # noqa: BLE001
            rerr = str(exc)[:200].replace(key, "***")
            print("RENT ERR:", rerr)
            rent_note = "전세 조회 실패로 이번 주 전세 생략"
            if st.get("rent_ops_day") != today.isoformat():
                rent_ops = ops_msg(cfg, stamp, f"전월세 API: {rerr}. 매매 요약은 그대로 보냄, 전세 state 는 그대로.", "전세")
                st["rent_ops_day"] = today.isoformat()
                ops = rent_ops
        if not seeding:
            out = summary(cfg, new, cancels, stamp, rnew, ratios, wol, rent_note)
        prune(st["deals"], today, cfg["history_months"])
        st["last_week"] = wk
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        if key:
            err = err.replace(key, "***")
        print("ERR:", err)
        if st.get("ops_day") != today.isoformat():
            ops = ops_msg(cfg, stamp, f"{err}. 이번 주 판정 건너뜀, state 는 그대로.")
            st["ops_day"] = today.isoformat()
    if out:
        check_text(*out)
        print(f"TITLE: {out[0]}\nPRIORITY: {cfg['priority']} TOPIC: {route_topic(topic, out[0])}\n{out[1]}\n")
    elif not ops:
        print("보낼 것 없음")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> ops\n{ops[1]}\n")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    if out:
        pub(topic, out[0], out[1], cfg["priority"], ["house"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    global STATE_PATH
    import tempfile
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    def it(y, m, d, amt, area="84.6861", fl="8", jb="1200", umd="이의동", cancel=False):
        return (f"<item><aptNm>X</aptNm><dealAmount>{amt:,}</dealAmount><dealYear>{y}</dealYear><dealMonth>{m}</dealMonth><dealDay>{d}</dealDay>"
                f"<excluUseAr>{area}</excluUseAr><floor>{fl}</floor><jibun>{jb}</jibun><umdNm>{umd}</umdNm>"
                f"<cdealType>{'O' if cancel else ' '}</cdealType><cdealDay>{'26.11.05' if cancel else ' '}</cdealDay></item>")
    def ri(y, m, d, dep, rent=0, fl="10", area="84.6861", jb="1200", umd="이의동", ct="갱신", pre=59000):
        return (f"<item><deposit>{dep:,}</deposit><monthlyRent>{rent}</monthlyRent><dealYear>{y}</dealYear><dealMonth>{m}</dealMonth><dealDay>{d}</dealDay>"
                f"<excluUseAr>{area}</excluUseAr><floor>{fl}</floor><jibun>{jb}</jibun><umdNm>{umd}</umdNm><contractType>{ct}</contractType><preDeposit>{pre:,}</preDeposit></item>")
    rdb = {"41117": {"202610": [ri(2026, 10, 27, 61950), ri(2026, 10, 20, 30000, rent=150)]}}
    rent_fail = {"on": False}
    db = {"41117": {"202610": [it(2026, 10, 8, 119000), it(2026, 10, 3, 109200, area="122.051", jb="1184"),
                               it(2026, 10, 2, 150000, area="84.9424", jb="606", umd="원천동")],
                    "202609": [it(2026, 9, 29, 112500, fl="12")]}}
    def fake(api, key, lw, ym):
        src = rdb if "Rent" in api else db
        if "Rent" in api and rent_fail["on"]:
            raise ValueError("API 키 오류 SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
        xml = "<response><header><resultCode>000</resultCode></header><body><items>" + "".join(src.get(lw, {}).get(ym, [])) + f"</items><totalCount>{len(src.get(lw, {}).get(ym, []))}</totalCount></body></response>"
        return parse_xml(xml)[0]
    sent = []
    pub = lambda *a, **k: sent.append(a)  # noqa: E731
    n = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
    with tempfile.TemporaryDirectory() as td:
        STATE_PATH = Path(td) / "a.json"
        run("auto", n("2026-11-02 09:00"), cfg, pub, fake, "K")
        s = load_state()
        assert len(s["deals"]) == 2 and len(s["rent"]) == 1 and not sent and s["last_week"] == "2026-W45", s
        run("auto", n("2026-11-03 09:00"), cfg, pub, fake, "K")
        assert not sent
        db["41117"]["202611"] = [it(2026, 11, 4, 121000, fl="15"), it(2026, 11, 5, 98000, area="84.1638", jb="1211")]
        db["41117"]["202610"][0] = it(2026, 10, 8, 119000, cancel=True)
        rdb["41117"]["202611"] = [ri(2026, 11, 3, 70000, fl="3", ct="신규", pre=0)]
        db["41465"] = {"202611": [it(2026, 11, 6, 128000, area="101.9783", jb="842", umd="상현동"), it(2026, 11, 6, 135000, area="157.1754", jb="842", umd="상현동")]}
        run("auto", n("2026-11-09 09:00"), cfg, pub, fake, "K")
        assert len(sent) == 1, sent
        t, m = sent[0][1], sent[0][2]
        assert t == "[APT-RT] 주간 실거래 매매 3건, 전세 1건, 신고가 1건, 해제 1건 | KOSPI", t
        assert "전세 새 신고 1건: 11/03 3층 7억원 신규" in m and "전세가율(최근 3개월 중앙값): 광교2차e편한세상 전세 중앙 6억 5,975만원(2건)" in m, m
        assert "10/27" not in m.split("전세 새 신고")[1].split("||")[0], m
        assert "12억 1,000만원, 직전 09/29 11억 2,500만원 대비 +8,500만원, 12개월 최고 11억 2,500만원" in m, m
        assert "해제: 10/08 8층 11억 9,000만원" in m and "157" not in m and "수지센트럴아이파크 101.98㎡] 매매 새 신고 1건" in m, m
        sent.clear()
        rent_fail["on"] = True
        db["41117"]["202611"].append(it(2026, 11, 12, 118000, fl="9"))
        run("auto", n("2026-11-16 09:00"), cfg, pub, fake, "K")
        assert len(sent) == 2, sent
        assert "매매 1건, 전세 0건" in sent[0][1] and "전세 조회 실패로 이번 주 전세 생략" in sent[0][2], sent[0]
        assert "전세 실거래 조회 실패" in sent[1][1] and "매매 요약은 그대로" in sent[1][1], sent[1]
        rent_fail["on"] = False
        sent.clear()
        run("auto", n("2026-11-17 09:00"), cfg, pub, lambda *a: (_ for _ in ()).throw(ValueError("x")), "K")
        assert not sent
        run("auto", n("2026-11-23 09:00"), cfg, pub, lambda *a: (_ for _ in ()).throw(ValueError("boom")), "K")
        assert len(sent) == 1 and sent[0][0] != "cwally-port" or "실패" in sent[0][1], sent
        sent.clear()
        run("auto", n("2026-11-24 09:00"), cfg, pub, fake, "")
        assert len(sent) == 1 and "secret 없음" in sent[0][2], sent
    assert won(105000) == "10억 5,000만원" and won(110000) == "11억원" and diff(-5500) == "-5,500만원"
    for tt, mm in (ops_msg(cfg, "T", "x"), install_message(cfg, "T")):
        check_text(tt, mm)
    try:
        parse_xml("<OpenAPI_ServiceResponse><errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg><returnReasonCode>30</returnReasonCode></OpenAPI_ServiceResponse>")
        raise AssertionError("no raise")
    except ValueError as e:
        assert "API 오류" in str(e)
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM'] [--force-week]")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if ("--as-of" in args or "--force-week" in args) and mode != "dry":
        print("--as-of, --force-week 는 dry 에서만")
        return 2
    if "--as-of" in args:
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg, force_week="--force-week" in args)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(cfg, now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
