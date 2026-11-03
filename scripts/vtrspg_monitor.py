#!/usr/bin/env python3
"""VTR vs SPG stance watch. Silent unless a tracked band flips. Not a buy order."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "vtrspg.json"
STATE_PATH = ROOT / "state" / "vtrspg.json"
UA = {"User-Agent": "cwally-alerts vtrspg research monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def price_of(url: str) -> float | None:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    meta = result[0].get("meta") or {}
    raw = meta.get("regularMarketPrice")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | VTR SPG | 변경조건 점검",
            "이게 뭐 알림: Ventas(VTR)와 Simon(SPG)의 12개월, 3년 금리 시나리오에서 구간이 바뀌면 보내는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def yield_band(y: float, th: dict) -> str:
    if y <= float(th["yield_low"]):
        return "low"
    if y < float(th["yield_mid"]):
        return "mid"
    if y < float(th["yield_stress"]):
        return "high"
    return "stress"


def mult_zone(mult: float, cheap: float, rich: float) -> str:
    if mult < cheap:
        return "cheap"
    if mult > rich:
        return "rich"
    return "base"


def spg_entry_level(base: dict, th: dict) -> tuple[float, str, float, float]:
    """Stricter (lower) of the 4.5% dividend-yield price and 15x RE FFO midpoint."""
    div = float(base["spg_dividend_annual"])
    ffo = float(base["spg_ffo"])
    y = float(th["spg_entry_yield"])
    m = float(th["spg_entry_mult"])
    by_yield = round(div / (y / 100.0), 2)
    by_mult = round(m * ffo, 2)
    if by_mult <= by_yield:
        return by_mult, f"{m:g}배 (RE FFO {ffo:g}달러 x {m:g})", by_yield, by_mult
    return by_yield, f"배당 {y:g}% (연 {div:g}달러 / {y:g}%)", by_yield, by_mult


def spg_entry_check(spg: float, state_entry: dict | None, base: dict, th: dict) -> tuple[bool, dict]:
    level = spg_entry_level(base, th)[0]
    st = dict(state_entry or {"armed": True})
    rearm = level * (1 + float(th["spg_entry_rearm_pct"]) / 100.0)
    fire = False
    if st.get("armed", True):
        if spg <= level:
            fire = True
            st["armed"] = False
    elif spg > rearm:
        st["armed"] = True
    st["level"] = level
    st["rearm_above"] = round(rearm, 2)
    return fire, st


def spg_entry_alert(stamp: str, spg: float, y10: float | None, base: dict, th: dict, install: bool = False) -> tuple[str, str]:
    level, leg, by_yield, by_mult = spg_entry_level(base, th)
    ffo = float(base["spg_ffo"])
    div = float(base["spg_dividend_annual"])
    cheap = float(th["spg_mult_cheap"]) * ffo
    rearm = level * (1 + float(th["spg_entry_rearm_pct"]) / 100.0)
    ytxt = f"{y10:.2f}%" if y10 is not None else "미확인"
    status = (
        f"SPG 정규장 종가 {spg:.2f}달러, 진입가 {level:.2f}달러(더 낮은 쪽: {leg}). "
        f"배당 4.5% 가격 {by_yield:.2f}달러, 15배 가격 {by_mult:.2f}달러. "
        f"P/FFO {spg / ffo:.2f}배, 배당수익률 {div / spg * 100:.2f}%, 미국 10년물 {ytxt}. "
        f"14배 cheap 구간 {cheap:.2f}달러까지 {(cheap / spg - 1) * 100:+.1f}%."
    )
    if install:
        title = "[VTRSPG] SPG 진입 단계 설치 확인 | 매매 신호 아님 | SPG"
        status = "설치 확인용 1회 발송입니다. " + status
    else:
        title = f"[VTRSPG] SPG ${spg:.2f}, 진입 단계 도달 (15배, 배당 4.5%) | SPG"
    msg = "\n".join(
        [
            f"{stamp} | VTR SPG | SPG 진입 단계",
            "이게 뭐 알림: SPG 종가가 진입가(배당 4.5% 가격과 RE FFO 15배 가격 중 낮은 쪽) 이하로 처음 내려오면 1번 보내는 알림입니다. 분할 매수 1차 검토 구간, 투자 권유 아님.",
            f"지금 결과/상태: {status}",
            "지금 할 일: 분할 매수 1차 검토 구간입니다. 한 번에 다 사지 말고 1차 분량만 검토하세요. 자동 주문은 아닙니다.",
            f"나중에: 14배 {cheap:.2f}달러 아래면 cheap 구간 알림이 따로 옵니다. 종가가 {rearm:.2f}달러(진입가 +3%) 위로 올라가면 다시 무장합니다. 실적 뒤 배당과 가이던스가 바뀌면 진입가도 바뀝니다.",
            "익절/줄일 때: 17배(rich) 위로 올라가면 인컴 우위가 줄어듭니다. 그때 비중을 다시 보세요.",
            "하지 말 것: 신용, 미수, 한 번에 전량 매수. 이 알림을 FFO 가이던스 유지 확인으로 쓰기.",
        ]
    )
    return title, msg


def selftest() -> int:
    base = {"spg_dividend_annual": 9.0, "spg_ffo": 13.25}
    th = {"spg_entry_yield": 4.5, "spg_entry_mult": 15.0, "spg_entry_rearm_pct": 3.0, "spg_mult_cheap": 14.0}
    lv, leg, by, bm = spg_entry_level(base, th)
    assert (lv, by, bm) == (198.75, 200.0, 198.75) and "15" in leg, (lv, leg)
    lv2 = spg_entry_level({"spg_dividend_annual": 8.8, "spg_ffo": 13.4}, th)
    assert lv2[0] == 195.56 and "4.5" in lv2[1], lv2
    f, st = spg_entry_check(221.73, None, base, th)
    assert not f and st["armed"]
    f, st = spg_entry_check(198.75, st, base, th)
    assert f and not st["armed"]
    f, st = spg_entry_check(190.0, st, base, th)
    assert not f and not st["armed"]
    f, st = spg_entry_check(204.0, st, base, th)
    assert not f and not st["armed"]
    f, st = spg_entry_check(205.0, st, base, th)
    assert not f and st["armed"]
    f, st = spg_entry_check(198.0, st, base, th)
    assert f
    t, m = spg_entry_alert("x", 198.0, 4.9, base, th)
    assert t.startswith("[VTRSPG] SPG $198.00, 진입 단계 도달") and t.endswith("| SPG"), t
    keys = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    pos = [m.index(k + ":") for k in keys]
    assert pos == sorted(pos), pos
    assert "\u00b7" not in m + t and "\u2014" not in m + t
    print("selftest ok")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry" or os.environ.get("VTRSPG_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "install":
        b, t = cfg["baseline"], cfg["thresholds"]
        spg_i = price_of(cfg["sources"]["spg"])
        tnx = price_of(cfg["sources"]["tnx"])
        y_i = None if tnx is None else (tnx / 10.0 if tnx > 20 else tnx)
        title, msg = spg_entry_alert(datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), spg_i, y_i, b, t, install=True)
        print(title)
        print(msg)
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, msg, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")))
        return 0
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "yield_band": None,
            "vtr_zone": None,
            "spg_zone": None,
            "earnings_sent": [],
            "fetch_fail_date": None,
            "last_yield": None,
            "last_vtr": None,
            "last_spg": None,
        },
    )
    now = datetime.now(KST)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    th = cfg["thresholds"]
    base = cfg["baseline"]
    src = cfg["sources"]
    alerts: list[tuple[str, str, int]] = []
    vtr = spg = y10 = None
    err = None
    try:
        vtr = price_of(src["vtr"])
        spg = price_of(src["spg"])
        tnx = price_of(src["tnx"])
        # Yahoo ^TNX is sometimes yield*10 and sometimes the yield.
        y10 = None if tnx is None else (tnx / 10.0 if tnx > 20 else tnx)
    except Exception as exc:
        err = str(exc)
    print(f"vtr={vtr} spg={spg} y10={y10} err={err}")

    if vtr is None or spg is None or y10 is None:
        if state.get("fetch_fail_date") != today:
            alerts.append(
                (
                    "[VTRSPG] VTR/SPG 시세 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"VTR, SPG, 10년물 중 일부를 못 읽었습니다. {err or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 VTR, SPG 종가와 미국 10년물 수익률을 직접 보세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 비중을 바꾸지 마세요.",
                        "조회 실패를 금리 하락이나 매수 신호로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today
    else:
        vtr_m = vtr / float(base["vtr_ffo"])
        spg_m = spg / float(base["spg_ffo"])
        band = yield_band(y10, th)
        vz = mult_zone(vtr_m, float(th["vtr_mult_cheap"]), float(th["vtr_mult_rich"]))
        sz = mult_zone(spg_m, float(th["spg_mult_cheap"]), float(th["spg_mult_rich"]))
        prev_band = state.get("yield_band")
        status = (
            f"10년물 {y10:.2f}%(구간 {band}). "
            f"VTR {vtr:.2f}달러, 2026 FFO {base['vtr_ffo']} 기준 배수 {vtr_m:.1f}({vz}). "
            f"SPG {spg:.2f}달러, 2026 FFO {base['spg_ffo']} 기준 배수 {spg_m:.1f}({sz}). "
            f"기준일 가격 {base['price_date']}, 금리 {base['yield_date']} {base['yield_10y']}%."
        )
        if prev_band is None:
            print(f"baseline band={band} vtr={vz} spg={sz}; no first alert")
        elif band != prev_band:
            if band == "low":
                title = "[VTRSPG] VTR/SPG 10년물 4.5% 이하 | 성장 창 점검"
                action = "VTR 성장 조건을 점검하세요. 배수가 19배 위면 아직 비쌉니다. 실적 전 비중 확정은 보류."
                later = "SHOP 점유와 NOI가 가이던스를 넘고 배수가 유지될 때만 성장 쪽으로 옮기세요."
            elif band == "mid":
                title = "[VTRSPG] VTR/SPG 10년물 4.5~5.0% | 관망"
                action = "SPG 인컴 유지를 기본으로 두세요. VTR은 실적 확인 전 비중을 늘리지 마세요."
                later = "4.5% 아래로 내려가면 성장 창, 5.0% 위로 올라가면 인컴 고수로 다시 알립니다."
            elif band == "high":
                title = "[VTRSPG] VTR/SPG 10년물 5%대 | 인컴 유지"
                action = "신규 매수는 SPG 버퍼가 더 큽니다. VTR 추격 매수는 하지 마세요."
                later = "5.5% 이상이면 하향 성장 가정에서 국채가 앞섭니다."
            else:
                title = "[VTRSPG] VTR/SPG 10년물 5.5% 이상 | 국채 우선"
                action = "추가매수를 보류하세요. 성장이 하향이면 VTR과 SPG 모두 국채 대비 초과가 없습니다."
                later = "5.0% 아래로 돌아오거나 FFO 가이던스가 올라올 때 다시 보세요."
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        status,
                        action,
                        later,
                        "이번 구간 변경만으로 익절하거나 전량 교체하지 마세요.",
                        "스프레드 고정 계산을 확정 수익으로 단정. 신용, 미수, 추격.",
                    ),
                    4,
                )
            )
        else:
            print(f"yield band unchanged {band}")

        if state.get("vtr_zone") not in (None, vz):
            alerts.append(
                (
                    f"[VTRSPG] VTR/SPG VTR 배수 {vz} | 성장 베팅 재점검",
                    body(
                        stamp,
                        status,
                        "VTR 배수가 구간을 넘었습니다. 19배 아래면 압축, 23배 위면 프리미엄 확대입니다. 금리 구간과 같이 보세요.",
                        "3분기 실적(2026-10-27)에서 SHOP NOI 16%와 FFO 가이던스를 확인하세요.",
                        "배수 하락만으로 사지 마세요. 성장 둔화일 수 있습니다.",
                        "2026 가이던스 FFO를 확정 실적으로 단정.",
                    ),
                    3,
                )
            )
        if state.get("spg_zone") not in (None, sz):
            alerts.append(
                (
                    f"[VTRSPG] VTR/SPG SPG 배수 {sz} | 인컴 가격 재점검",
                    body(
                        stamp,
                        status,
                        "SPG 배수가 구간을 넘었습니다. 14배 아래면 가격 매력, 17배 위면 인컴 우위가 줄어듭니다.",
                        "실적 창 2026-11-02~05에 점유 96%와 임대료 증가를 확인하세요.",
                        "배수 하락만으로 비중을 확정하지 마세요.",
                        "리테일 점유를 이 알림이 확인한 것처럼 쓰지 마세요.",
                    ),
                    3,
                )
            )
        fire, est = spg_entry_check(spg, state.get("spg_entry"), base, th)
        print(f"spg entry: close {spg:.2f} vs entry {est['level']:.2f} ({spg_entry_level(base, th)[1]}), armed={est['armed']}, fire={fire}")
        if fire:
            t_e, m_e = spg_entry_alert(stamp, spg, y10, base, th)
            alerts.append((t_e, m_e, 4))
            est["fired_on"] = today
            est["fired_close"] = round(spg, 2)
        state["spg_entry"] = est
        state["yield_band"] = band
        state["vtr_zone"] = vz
        state["spg_zone"] = sz
        state["last_yield"] = round(y10, 3)
        state["last_vtr"] = round(vtr, 2)
        state["last_spg"] = round(spg, 2)
        state["last_vtr_mult"] = round(vtr_m, 2)
        state["last_spg_mult"] = round(spg_m, 2)

    sent = set(state.get("earnings_sent") or [])
    earn = cfg["earnings"]
    if today == earn["vtr"] and "vtr" not in sent:
        alerts.append(
            (
                "[VTRSPG] VTR/SPG VTR 실적일 | 성장 조건 확인",
                body(
                    stamp,
                    "VTR 3분기 실적일 2026-10-27. 자동 판정 없음.",
                    "SHOP 동일점포 NOI가 16% 가이던스를 넘는지, FFO 가이던스가 3.85~3.90달러에 머무는지 공시에서 보세요.",
                    "넘기면 성장 조건이 살아 있습니다. 배수가 21배 근처면 이미 반영된 성장입니다.",
                    "실적 헤드라인만으로 사지 마세요.",
                    "가이던스 유지를 상향으로 읽기.",
                ),
                3,
            )
        )
        sent.add("vtr")
    if earn["spg_from"] <= today <= earn["spg_to"] and "spg" not in sent:
        alerts.append(
            (
                "[VTRSPG] VTR/SPG SPG 실적 창 | 인컴 조건 확인",
                body(
                    stamp,
                    "SPG 실적 창 2026-11-02~05. 날짜가 바뀌면 공시를 우선하세요. 자동 판정 없음.",
                    "점유 96%, 기본임대료 증가, Real Estate FFO 13.20~13.30달러 유지를 확인하세요.",
                    "점유가 빠지거나 임대료 증가가 멈추면 인컴 우위를 재점검하세요.",
                    "매출 증가만으로 비중을 늘리지 마세요.",
                    "실적 창 알림을 매수 신호로 쓰기.",
                ),
                3,
            )
        )
        sent.add("spg")
    state["earnings_sent"] = sorted(sent)

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
