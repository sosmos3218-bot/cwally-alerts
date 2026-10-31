#!/usr/bin/env python3
"""[OIL] Brent / refining-margin estimate / USD-KRW boundary watch. Silent unless a band is newly entered.

Weekday 09:00 KST morning slot. Not a trade order.
Modes: auto (default), dry (no send, no state write; OIL_TEST_BRENT / OIL_TEST_MARGIN / OIL_TEST_FX override),
install (one install-confirmation send to cwally-port, no state write), force (ignore valid_until).
First auto run records the current zones only (baseline) and sends nothing.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "oilwatch.json"
STATE_PATH = ROOT / "state" / "oilwatch.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts oilwatch)"}
BRENT_STALE_DAYS = 6
MARGIN_STALE_DAYS = 7
FX_STALE_DAYS = 6


def fetch(url: str) -> str:
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last if last else RuntimeError("fetch failed")


def num(x) -> float:
    return float(str(x).replace(",", "").strip())


def brent_close(cfg: dict) -> tuple[float, str]:
    data = json.loads(fetch(cfg["sources"]["brent"]))
    res = data["chart"]["result"][0]
    stamps = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    off = int((res.get("meta") or {}).get("gmtoffset") or 0)
    for ts, cl in zip(reversed(stamps), reversed(closes)):
        if cl is not None:
            day = datetime.fromtimestamp(int(ts) + off, timezone.utc).date().isoformat()
            return float(cl), day
    raise RuntimeError("no Brent close")


def petronet_series(today: date, cfg: dict) -> dict:
    page = fetch(cfg["sources"]["petronet"])

    def block(name: str) -> str:
        i = page.find(f"const {name} =")
        if i < 0:
            raise RuntimeError(f"{name} not found")
        return page[i : i + 6000]

    def labels(seg: str) -> list[str]:
        m = re.search(r"labels:\s*\[([^\]]*)\]", seg)
        if not m:
            raise RuntimeError("labels not found")
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]

    def series(seg: str, label: str) -> list[float]:
        m = re.search(r"label:\s*[\"']" + re.escape(label) + r"[\"'].*?data\s*:\s*\[([^\]]*)\]", seg, re.S)
        if not m:
            raise RuntimeError(f"series {label} not found")
        return [num(v) for v in m.group(1).split(",") if v.strip()]

    oil = block("interOilPriceChartOpt")
    prod = block("interProdPriceChartOpt")
    lo, lp = labels(oil), labels(prod)
    if lo != lp:
        raise RuntimeError("crude/product dates differ")
    dub = series(oil, "Dubai")
    gas, ker, dsl = series(prod, "휘발유"), series(prod, "등유"), series(prod, "경유")
    n = len(lo)
    if not (len(dub) == len(gas) == len(ker) == len(dsl) == n) or n == 0:
        raise RuntimeError("series length mismatch")
    days = []
    for lab in lo:
        mth, dd = (int(p) for p in lab.split("."))
        yr = today.year if (mth, dd) <= (today.month, today.day) else today.year - 1
        days.append(date(yr, mth, dd).isoformat())
    return {"days": days, "dubai": dub, "gasoline": gas, "kerosene": ker, "diesel": dsl}


def margin_estimate(s: dict, cfg: dict) -> tuple[float, str, str, float]:
    m = cfg["margin"]
    w = m["weights"]
    k = int(m["window_days"])
    vals = [
        w["gasoline"] * s["gasoline"][i] + w["kerosene"] * s["kerosene"][i] + w["diesel"] * s["diesel"][i] - w["dubai"] * s["dubai"][i]
        for i in range(len(s["days"]))
    ][-k:]
    est = sum(vals) / len(vals) - float(m["offset"])
    return est, s["days"][-k], s["days"][-1], s["dubai"][-1]


def usdkrw(cfg: dict) -> tuple[float, str]:
    data = json.loads(fetch(cfg["sources"]["usdkrw"]))
    rows = data if isinstance(data, list) else (data.get("result") or data.get("items") or [])
    if not rows:
        raise RuntimeError("fx empty")
    r = rows[0]
    price = r.get("closePrice") or r.get("closePriceRaw") or r.get("tradePrice")
    day = str(r.get("localTradedAt") or r.get("tradeDate") or "")[:10]
    return num(price), day


def zone(value: float, prev: str, low: float | None, low_rearm: float | None, high: float | None = None, high_rearm: float | None = None) -> str:
    if high is not None and (value >= high or (prev == "high" and value >= high_rearm)):
        return "high"
    if low is not None and (value < low or (prev == "low" and value < low_rearm)):
        return "low"
    return "mid"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | OIL | 유가, 정제마진, 환율 경계",
            "이게 뭐 알림: 에너지 시나리오(브렌트 120/85, 정제마진 15, 원/달러 1,300) 경계에 새로 닿았을 때만 울립니다. 매매 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    install = mode == "install"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except json.JSONDecodeError:
        state = {}
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry", "install"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    zones = dict(state.get("zones") or {})
    fails = dict(state.get("fail_date") or {})
    vals: dict = {}
    errs: dict = {}

    # Brent
    try:
        if dry and os.environ.get("OIL_TEST_BRENT"):
            vals["brent"] = (float(os.environ["OIL_TEST_BRENT"]), today.isoformat())
        else:
            b, bd = brent_close(cfg)
            if (today - date.fromisoformat(bd)).days > BRENT_STALE_DAYS:
                raise RuntimeError(f"Brent 마지막 종가 {bd}가 오래됨")
            vals["brent"] = (b, bd)
    except Exception as exc:
        errs["brent"] = str(exc)
    # Refining margin estimate
    try:
        if dry and os.environ.get("OIL_TEST_MARGIN"):
            vals["margin"] = (float(os.environ["OIL_TEST_MARGIN"]), today.isoformat(), today.isoformat(), 0.0)
        else:
            s = petronet_series(today, cfg)
            est, d0, d1, dub = margin_estimate(s, cfg)
            if (today - date.fromisoformat(d1)).days > MARGIN_STALE_DAYS:
                raise RuntimeError(f"페트로넷 마지막 날짜 {d1}가 오래됨")
            vals["margin"] = (est, d0, d1, dub)
    except Exception as exc:
        errs["margin"] = str(exc)
    # USD/KRW
    try:
        if dry and os.environ.get("OIL_TEST_FX"):
            vals["fx"] = (float(os.environ["OIL_TEST_FX"]), today.isoformat())
        else:
            fx, fd = usdkrw(cfg)
            if fd and (today - date.fromisoformat(fd)).days > FX_STALE_DAYS:
                raise RuntimeError(f"환율 마지막 날짜 {fd}가 오래됨")
            vals["fx"] = (fx, fd)
    except Exception as exc:
        errs["fx"] = str(exc)
    print(f"vals={vals} errs={errs}")

    alerts: list[tuple[str, str, int]] = []
    if install:
        parts = []
        if "brent" in vals:
            parts.append(f"브렌트 {vals['brent'][0]:.2f}달러({vals['brent'][1]})")
        if "margin" in vals:
            parts.append(f"정제마진 추정 {vals['margin'][0]:.1f}달러({vals['margin'][1]}~{vals['margin'][2]} 평균)")
        if "fx" in vals:
            parts.append(f"원/달러 {vals['fx'][0]:,.1f}원")
        miss = ", ".join(errs) if errs else "없음"
        alerts.append(
            (
                "[OIL] BZ=F 설치 확인 | 매매 신호 아님",
                body(
                    stamp,
                    f"설치 확인 발송입니다. 현재 {', '.join(parts)}. 조회 실패 항목: {miss}. 첫 정규 실행(평일 09:00)은 기준값만 기록하고 울리지 않습니다.",
                    "할 일 없음. 이 알림이 왔으면 설치가 된 것입니다.",
                    "브렌트 120달러 이상 또는 85달러 미만, 정제마진 추정 15달러 미만, 원/달러 1,300원 미만에 새로 닿으면 그때만 울립니다.",
                    "해당 없음.",
                    "이 설치 확인 알림을 매매 신호로 보기.",
                ),
                2,
            )
        )
    else:
        names = {"brent": "브렌트 종가", "margin": "정제마진 추정치", "fx": "원/달러 환율"}
        for key, err in errs.items():
            if fails.get(key) != today.isoformat():
                verb = "수집 실패" if key == "margin" else "조회 실패"
                alerts.append(
                    (
                        f"[OIL] BZ=F {names[key]} {verb} | 조건 미확인",
                        body(
                            stamp,
                            f"{names[key]} {verb}. {err}. 조건 미충족이 아닙니다.",
                            "증권 앱이나 증권사 주간 자료에서 값을 직접 확인하세요.",
                            "다음 평일 09:00 KST에 재시도합니다.",
                            "이번 알림으로 비중을 바꾸지 마세요.",
                            "조회 실패를 정상이나 경계 미도달로 취급.",
                        ),
                        3,
                    )
                )
                fails[key] = today.isoformat()

        baseline = not state.get("baseline_done")
        new = {}
        if "brent" in vals:
            b, bd = vals["brent"]
            bc = cfg["brent"]
            new["brent"] = zone(b, zones.get("brent", "mid"), bc["low"], bc["low_rearm"], bc["high"], bc["high_rearm"])
        if "margin" in vals:
            mc = cfg["margin"]
            new["margin"] = zone(vals["margin"][0], zones.get("margin", "mid"), mc["low"], mc["low_rearm"])
        if "fx" in vals:
            fc = cfg["usdkrw"]
            new["fx"] = zone(vals["fx"][0], zones.get("fx", "mid"), fc["low"], fc["low_rearm"])
        print(f"zones prev={zones} new={new} baseline={baseline}")

        if not baseline:
            if new.get("brent") != zones.get("brent") and new.get("brent") == "high":
                b, bd = vals["brent"]
                alerts.append(
                    (
                        "[OIL] BZ=F 브렌트 120달러 이상 | 확전 시나리오 점검",
                        body(
                            stamp,
                            f"브렌트 종가 {b:.2f}달러({bd} 미국장). 확전 시나리오 경계 120달러 이상입니다.",
                            "미국 에너지(218420 또는 XLE) 비중이 계획 범위 안인지 확인하세요. 국내 정유주는 급등 초기에 약했던 사례(2026년 3월)가 있어 추격하지 마세요.",
                            "호르무즈 봉쇄, 사우디 시설 피해가 실제 공급 차질인지 뉴스로 확인하세요. 115달러 아래로 내려갔다가 다시 120달러를 넘으면 또 울립니다.",
                            "미국 에너지가 목표 비중의 1.5배를 넘으면 일부 줄이는 것을 검토하세요.",
                            "한 번에 큰 비중 매수. 국내 에너지화학 ETF(117460, 139250)를 유가 헤지로 착각.",
                        ),
                        4,
                    )
                )
            if new.get("brent") != zones.get("brent") and new.get("brent") == "low":
                b, bd = vals["brent"]
                alerts.append(
                    (
                        "[OIL] BZ=F 브렌트 85달러 미만 | 완화 시나리오 점검",
                        body(
                            stamp,
                            f"브렌트 종가 {b:.2f}달러({bd} 미국장). 완화 시나리오 경계 85달러 미만입니다.",
                            "국내 정유주(S-Oil, SK이노베이션) 비중이 있으면 줄일지 검토하세요. 정제마진 추정치도 같이 보세요.",
                            "88달러 위로 올라갔다가 다시 85달러 아래로 오면 또 울립니다. EIA 월간 전망(STEO) 방향도 확인하세요.",
                            "정유주는 유가 민감도가 가장 커서 먼저 줄일 후보입니다. GS처럼 유가 민감도가 낮은 종목은 덜 급합니다.",
                            "유가 하락만 보고 미국 에너지를 전량 매도. 하루 급락에 반응한 몰빵 매매.",
                        ),
                        3,
                    )
                )
            if new.get("margin") != zones.get("margin") and new.get("margin") == "low":
                est, d0, d1, _ = vals["margin"]
                alerts.append(
                    (
                        "[OIL] BZ=F 정제마진 추정 15달러 미만 | 정유주 비중 점검",
                        body(
                            stamp,
                            f"정제마진 추정치 {est:.1f}달러({d0}~{d1} 평균). 기준 15달러 미만입니다. 공식 복합정제마진이 아니라 페트로넷 가격으로 만든 추정치입니다.",
                            "증권사 주간 복합정제마진으로 한 번 더 확인한 뒤, 맞으면 국내 정유주 비중 축소를 검토하세요.",
                            "18달러 위로 회복했다가 다시 15달러 아래로 내려오면 또 울립니다.",
                            "S-Oil처럼 마진 민감도가 큰 종목부터 줄이는 것을 검토하세요.",
                            "추정치 하나만 보고 전량 매도. 확인 없이 공매도나 인버스 진입.",
                        ),
                        4,
                    )
                )
            if new.get("fx") != zones.get("fx") and new.get("fx") == "low":
                fx, fd = vals["fx"]
                alerts.append(
                    (
                        "[OIL] BZ=F 원/달러 1,300원 미만 | 미국 에너지 원화수익 점검",
                        body(
                            stamp,
                            f"원/달러 {fx:,.1f}원({fd}). 1,300원 미만입니다. [PORT]의 1,340 이하 2일 연속 규칙과는 별개 알림입니다.",
                            "218420이나 XLE 같은 미국 에너지의 원화 수익이 환율로 깎이고 있는지 확인하세요. 신규 환전은 서두르지 마세요.",
                            "1,310원 위로 올라갔다가 다시 1,300원 아래로 내려오면 또 울립니다.",
                            "미국 에너지 수익이 대부분 환율 손실로 상쇄되면 비중 축소를 검토하세요.",
                            "환율 하나만 보고 미국 자산 전량 매도.",
                        ),
                        3,
                    )
                )
        zones.update(new)

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["oil_drum"], server)
    if not alerts:
        print("no alert")
    if dry or install:
        print("state not written")
        return 0
    state["zones"] = zones
    state["fail_date"] = fails
    state["baseline_done"] = True
    state["last"] = {k: list(v) for k, v in vals.items()}
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
