#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "sndk.json"
STATE_PATH = ROOT / "state" / "sndk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int = 3) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str) -> dict | list:
    return json.loads(fetch(url))


def ir_headline(url: str) -> tuple[bool, str]:
    try:
        html = fetch(url)
    except Exception as exc:
        return False, str(exc)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    hit = re.search(
        r"(fiscal first quarter|Q1 FY ?2027|first quarter of fiscal 2027|Reports Fiscal First Quarter)(.{0,160})",
        text,
        re.I,
    )
    if hit:
        return True, hit.group(0)[:180].strip()
    return True, ""


def body_common(stamp: str, phase: str) -> list[str]:
    return [
        f"{stamp} | PORT | SNDK 체크 {phase}",
        "이게 뭐 알림: 샌디스크(SNDK) 1분기 가이던스 103~108억달러 점검. 매수·매도 신호가 아닙니다. 삼성전자·SK하이닉스는 스위치 3개가 켜지기 전엔 따로 봅니다.",
    ]


def kr_pre(cfg: dict, stamp: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    title = "[SNDK] 국내 5개부터 | 연결 스위치 꺼짐"
    lines = body_common(stamp, "10/28 국내 선행")
    lines += [
        "지금 결과: 삼성·하이닉스 3분기 숫자 창. SNDK 실적은 아직 11/5 미국 장후.",
        (
            "지금 할 일: 국내 필수 5개만 채우세요. "
            f"①삼성 HBM4 매출 {a['sec_hbm4_qoq']:.0f}배 전후 "
            f"②하반기 HBM4 비중 {a['sec_hbm4_mix']:.0f}% 초과 "
            f"③웨이퍼 {a['sec_wafer_now']:.0f}만→{a['sec_wafer_next']:.0f}만장 "
            "④하이닉스 2027 HBM 계약 "
            "⑤서버 D램 가격·납기."
        ),
        "나중에: 11/5 09:00에 SNDK 5개를 보냅니다. 오늘은 연결하지 마세요.",
        "익절/줄일 때: 국내 실적만으로 SNDK를 따라가지 마세요.",
        "하지 말 것: 목표가 2,400달러를 매매 근거로 쓰기. 헤드라인 영업이익으로 스위치 켜기. 신용·미수·추격.",
    ]
    return title, lines


def sndk_pre(cfg: dict, stamp: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    title = "[SNDK] 오늘 장후 SNDK | 연결 보류"
    lines = body_common(stamp, "11/5 실적 전")
    lines += [
        f"지금 결과: 발표는 미국 장후, KST {cfg['print_kst']} 전후. 숫자는 아직 없음.",
        (
            "지금 할 일: 밤에 볼 SNDK 5개만 준비. "
            f"①매출 {a['rev_lo']}~{a['rev_hi']}억달러 "
            f"②매출총이익률 {a['gm_lo']:.0f}~{a['gm_hi']:.0f}% "
            f"③데이터센터 매출 {a['dc_rev_q4']}억 대비 "
            f"④비트 비중 {a['dc_bit_q4']:.0f}% 대비 "
            "⑤성장이 가격인지 물량인지."
        ),
        "나중에: 11/6 09:00에 발표문 대조 알림. 발표 직후 추격 금지.",
        "익절/줄일 때: 가이던스 하회면 SNDK만 재검토. 국내 자동 매도가 아닙니다.",
        "하지 말 것: 숏츠 목표가, 컨센서스 평균으로 선매매. 신용·미수.",
    ]
    return title, lines


def sndk_post(cfg: dict, stamp: str, ir_ok: bool, headline: str) -> tuple[str, list[str]]:
    a = cfg["anchors"]
    if ir_ok and headline:
        title = "[SNDK] 발표문 열림 | 5개 대조"
        status = f"IR에서 관련 문구를 찾음. {headline}"
    elif ir_ok:
        title = "[SNDK] 숫자 직접 대조 | 연결 기본 꺼짐"
        status = "IR은 열렸으나 Q1 숫자 문구는 확인 불가. 추정하지 않음."
    else:
        title = "[SNDK] IR 조회 실패 | 직접 확인"
        status = f"IR 조회 실패. {headline or '출처 없음'}."
    lines = body_common(stamp, "11/6 실적 후")
    lines += [
        f"지금 결과: {status}",
        (
            "지금 할 일: SNDK 5개 대조. "
            f"①매출 {a['rev_lo']}~{a['rev_hi']} "
            f"②이익률 {a['gm_lo']:.0f}% 이상 "
            f"③데이터센터 {a['dc_rev_q4']}억 "
            f"④비트 {a['dc_bit_q4']:.0f}% "
            "⑤가격 vs 물량. "
            "스위치 3개: "
            f"(1)매출 {a['rev_lo']}억 이상+이익률 {a['gm_lo']:.0f}% 이상 "
            "(2)데이터센터 매출·비트가 Q4 유지/상회이고 물량도 동반 "
            "(3)같은 주 국내 서버 D램·HBM이 타이트. "
            "3개가 모두 켜질 때만 삼성·하이닉스를 같이 보세요."
        ),
        "나중에: 스위치 0~1개면 분리, 2개면 관찰, 3개면 같은 방향. 이 알림은 후속 발송 없음.",
        "익절/줄일 때: 매출만 맞고 데이터센터가 빠지면 국내 비중을 올리지 마세요.",
        f"하지 말 것: 조회 실패를 호재로 읽기. 목표가 2,400달러로 추격. 출처: {cfg['ir_url']}",
    ]
    return title, lines


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _fresh(asof: date | None, today: date, max_age_days: int) -> bool:
    if asof is None:
        return False
    return (today - asof).days <= int(max_age_days)


def fetch_ddr5_weekly(series_url: str, series_id: str) -> tuple[list[str], list[float]]:
    payload = fetch_json(series_url)
    if not isinstance(payload, dict):
        raise ValueError("DDRWatcher series root is not an object")
    labels = payload.get("labels")
    series = payload.get("series")
    if not isinstance(labels, list) or not isinstance(series, list):
        raise ValueError("DDRWatcher series missing labels/series")
    row = next((s for s in series if isinstance(s, dict) and s.get("id") == series_id), None)
    if not row or not isinstance(row.get("prices"), list):
        raise ValueError(f"DDRWatcher series_id not found: {series_id}")
    prices = [float(x) for x in row["prices"]]
    if len(labels) != len(prices) or len(prices) < 5:
        raise ValueError("DDRWatcher series too short or length mismatch")
    return [str(x)[:10] for x in labels], prices


def fetch_ddr5_daily_session_avg(daily_url: str, item: str) -> float | None:
    """Best-effort DRAMeXchange homepage scrape. Failure returns None (does not vote)."""
    try:
        html = fetch(daily_url)
    except Exception:
        return None
    esc = re.escape(item)
    pat = re.compile(
        esc
        + r".*?</a>\s*</td>\s*"
        r"<td[^>]*>\s*([\d.]+)\s*</td>\s*"
        r"<td[^>]*>\s*([\d.]+)\s*</td>\s*"
        r"<td[^>]*>\s*([\d.]+)\s*</td>\s*"
        r"<td[^>]*>\s*([\d.]+)\s*</td>\s*"
        r"<td[^>]*>\s*([\d.]+)\s*</td>",
        re.I | re.S,
    )
    m = pat.search(html)
    if not m:
        return None
    return float(m.group(5))


def eval_spot_ma4(cfg_s3: dict, today: date) -> dict:
    spot = cfg_s3.get("spot") or {}
    min_rise_pct_label = float(spot.get("ma4_min_rise_pct") or 0.0)
    out = {
        "id": "ddr5_ma4",
        "label": f"DDR5 현물 4주 이동평균 +{min_rise_pct_label:.1f}% 이상",
        "valid": False,
        "pass": False,
        "detail": "",
        "ma4_now": None,
        "ma4_prev": None,
        "latest_weekly": None,
        "latest_label": None,
        "daily_session_avg": None,
    }
    try:
        labels, prices = fetch_ddr5_weekly(
            str(spot.get("series_url") or "https://www.ddrwatcher.com/api/series"),
            str(spot.get("series_id") or "ddr5_chip_spot"),
        )
    except Exception as exc:
        out["detail"] = f"주간 시리즈 수집 실패: {exc}"
        return out

    latest_label = _parse_day(labels[-1])
    max_age = int(spot.get("max_age_days") or 10)
    if not _fresh(latest_label, today, max_age):
        out["detail"] = f"주간 시리즈 오래됨: 최신 {labels[-1]} (허용 {max_age}일)"
        out["latest_label"] = labels[-1]
        out["latest_weekly"] = prices[-1]
        return out

    n = int(spot.get("ma_weeks") or 4)
    if len(prices) < n + 1:
        out["detail"] = "주간 시리즈가 MA 계산에 부족"
        return out

    ma4_now = mean(prices[-n:])
    ma4_prev = mean(prices[-(n + 1) : -1])
    min_rise_pct = float(spot.get("ma4_min_rise_pct") or 0.0)
    threshold = ma4_prev * (1.0 + min_rise_pct / 100.0)
    rise_pct = (ma4_now / ma4_prev - 1.0) * 100.0 if ma4_prev else 0.0
    passed = ma4_now >= threshold
    out.update(
        {
            "valid": True,
            "pass": passed,
            "ma4_now": round(ma4_now, 4),
            "ma4_prev": round(ma4_prev, 4),
            "ma4_min_rise_pct": min_rise_pct,
            "ma4_rise_pct": round(rise_pct, 4),
            "latest_weekly": round(prices[-1], 4),
            "latest_label": labels[-1],
            "detail": (
                f"주간평균 {labels[-1]} ${prices[-1]:.3f} "
                f"(전주 ${prices[-2]:.3f}, w/w {(prices[-1] / prices[-2] - 1) * 100:+.2f}%). "
                f"MA{n} {ma4_now:.3f} vs 직전창 {ma4_prev:.3f} "
                f"(+{rise_pct:.2f}%, 기준 +{min_rise_pct:.1f}% 이상) "
                f"({'통과' if passed else '미달'})."
            ),
        }
    )
    daily = fetch_ddr5_daily_session_avg(
        str(spot.get("daily_url") or "https://www.dramexchange.com/"),
        str(spot.get("daily_item") or "DDR5 16Gb (2Gx8) 4800/5600"),
    )
    if daily is not None:
        out["daily_session_avg"] = daily
        out["detail"] += f" 당일 세션평균 ${daily:.3f}."
    return out


def eval_rdimm_lead(cfg_s3: dict, today: date) -> dict:
    lead = cfg_s3.get("rdimm_lead") or {}
    out = {
        "id": "rdimm_lead",
        "label": "RDIMM 리드타임",
        "valid": False,
        "pass": False,
        "detail": "",
    }
    weeks = lead.get("weeks")
    asof = _parse_day(lead.get("asof"))
    min_weeks = float(lead.get("min_weeks") or 16)
    max_age = int(lead.get("max_age_days") or 14)
    if weeks is None or asof is None:
        out["detail"] = "수동 입력 없음 (weeks/asof). HOLD 쪽 무효."
        return out
    if not _fresh(asof, today, max_age):
        out["detail"] = f"리드타임 입력 오래됨: asof {asof} (허용 {max_age}일)"
        return out
    weeks_f = float(weeks)
    passed = weeks_f >= min_weeks
    out.update(
        {
            "valid": True,
            "pass": passed,
            "detail": (
                f"{weeks_f:.0f}주 (기준 {min_weeks:.0f}주 이상). "
                f"asof {asof}. 출처: {lead.get('source') or '미기재'}"
            ),
        }
    )
    return out


def eval_hbm_or_contract(cfg_s3: dict, today: date) -> dict:
    """Core 3: HBM sold-out flag OR server contract QoQ guidance."""
    out = {
        "id": "hbm_or_contract",
        "label": "HBM 품절 또는 서버 계약가",
        "valid": False,
        "pass": False,
        "detail": "",
    }
    parts: list[str] = []
    any_valid = False
    any_pass = False

    hbm = cfg_s3.get("hbm_sold_out") or {}
    hbm_asof = _parse_day(hbm.get("asof"))
    hbm_age = int(hbm.get("max_age_days") or 21)
    if hbm.get("value") is not None and hbm_asof is not None:
        if _fresh(hbm_asof, today, hbm_age):
            any_valid = True
            if bool(hbm.get("value")):
                any_pass = True
                parts.append(f"HBM 품절 플래그 true (asof {hbm_asof})")
            else:
                parts.append(f"HBM 품절 플래그 false (asof {hbm_asof})")
        else:
            parts.append(f"HBM 플래그 오래됨 asof {hbm_asof}")
    else:
        parts.append("HBM 플래그 미입력")

    ctr = cfg_s3.get("server_contract") or {}
    ctr_asof = _parse_day(ctr.get("asof"))
    ctr_age = int(ctr.get("max_age_days") or 21)
    min_qoq = float(ctr.get("min_qoq_pct") or 10.0)
    if ctr.get("qoq_pct") is not None and ctr_asof is not None:
        if _fresh(ctr_asof, today, ctr_age):
            any_valid = True
            qoq = float(ctr["qoq_pct"])
            if qoq >= min_qoq:
                any_pass = True
                parts.append(f"서버 계약가 가이던스 +{qoq:.1f}% QoQ (기준 +{min_qoq:.0f}%)")
            else:
                parts.append(f"서버 계약가 가이던스 +{qoq:.1f}% QoQ < +{min_qoq:.0f}%")
        else:
            parts.append(f"서버 계약가 입력 오래됨 asof {ctr_asof}")
    else:
        parts.append("서버 계약가 미입력")

    out["valid"] = any_valid
    out["pass"] = any_pass
    out["detail"] = "; ".join(parts)
    if not any_valid:
        out["detail"] += ". 둘 다 무효면 이 조건은 투표 안 함."
    return out


def score_switch3(conditions: list[dict], cfg_s3: dict) -> dict:
    valid = [c for c in conditions if c.get("valid")]
    passed = [c for c in valid if c.get("pass")]
    min_valid = int(cfg_s3.get("min_valid") or 2)
    min_pass = int(cfg_s3.get("min_pass_for_on") or 2)
    if len(valid) < min_valid:
        verdict = "HOLD"
    elif len(passed) >= min_pass:
        verdict = "ON"
    else:
        verdict = "OFF"
    return {
        "verdict": verdict,
        "valid_count": len(valid),
        "pass_count": len(passed),
        "min_valid": min_valid,
        "min_pass_for_on": min_pass,
        "conditions": conditions,
    }


def switch3_title(cfg: dict, verdict: str) -> str:
    s3 = cfg.get("switch3") or {}
    tickers = s3.get("tickers_in_title") or ["SNDK", "005930", "000660"]
    joined = "/".join(str(t) for t in tickers)
    if verdict == "ON":
        return f"[SNDK] {joined} 스위치3 켜짐 | 국내 서버 D램/HBM 타이트"
    if verdict == "OFF":
        return f"[SNDK] {joined} 스위치3 꺼짐 | 국내 수급 완화 또는 기준 미달"
    return f"[SNDK] {joined} 스위치3 판단 보류 | 데이터 부족 또는 오래됨"


def switch3_lines(cfg: dict, stamp: str, result: dict) -> list[str]:
    s3 = cfg.get("switch3") or {}
    lines = [
        f"{stamp} | PORT | SNDK 스위치3 숫자 판정",
        "이게 뭐 알림: 스위치3(같은 주 국내 서버 D램/HBM 타이트)를 숫자 기준으로 다시 본 결과입니다. 매수 신호가 아닙니다.",
        (
            f"지금 결과: {result['verdict']} "
            f"(유효 {result['valid_count']}/{result['min_valid']}개, "
            f"통과 {result['pass_count']}/{result['min_pass_for_on']}개)."
        ),
    ]
    for cond in result["conditions"]:
        mark = "통과" if cond.get("pass") else ("무효" if not cond.get("valid") else "실패")
        lines.append(f"- {cond['label']}: {mark}. {cond.get('detail') or ''}")
    exp = s3.get("export_context") or {}
    if exp.get("yoy_pct") is not None:
        lines.append(
            f"참고(투표 아님): 메모리 수출 YoY {float(exp['yoy_pct']):.1f}% "
            f"/ {exp.get('memory_usd_bn')}억달러 (asof {exp.get('asof')})."
        )
    lines += [
        "지금 할 일: ON이면 삼성전자(005930)와 SK하이닉스(000660)를 SNDK와 같은 방향으로 관찰. OFF면 분리. HOLD면 입력값(리드타임/HBM/계약가)을 갱신하세요.",
        "나중에: 주 1회(ISO주 첫 morning)만 재평가. 수집 실패는 꺼짐이 아니라 보류입니다.",
        "익절/줄일 때: 이 알림만으로 비중을 바꾸지 마세요. 스위치 1과 2와 같이 보세요.",
        "하지 말 것: HOLD를 OFF로 읽기. 주간 한 번 현물 하락만으로 스위치 끄기. 신용, 미수, 추격.",
    ]
    return lines


def evaluate_switch3(cfg: dict, today: date) -> dict:
    s3 = cfg.get("switch3") or {}
    conditions = [
        eval_rdimm_lead(s3, today),
        eval_spot_ma4(s3, today),
        eval_hbm_or_contract(s3, today),
    ]
    return score_switch3(conditions, s3)


def switch3_due(cfg: dict, state: dict, today: date, mode: str) -> bool:
    s3 = cfg.get("switch3") or {}
    if not s3.get("enabled", False):
        return False
    active_from = _parse_day(s3.get("active_from")) or date(2026, 11, 6)
    expire = _parse_day(s3.get("expire_date")) or date(2027, 3, 31)
    if not (active_from <= today <= expire):
        return False
    if mode in {"force", "dry", "switch3"}:
        return True
    # ISO week: first weekday morning already gated by workflow; send once per ISO week
    iso = today.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    if state.get("switch3_week_key") == week_key and state.get("switch3_verdict"):
        return False
    return True


def default_state() -> dict:
    return {
        "kr_pre_notified": False,
        "sndk_pre_notified": False,
        "sndk_post_notified": False,
        "fail_notified": False,
        "last_phase": None,
        "updated_at": None,
        "switch3_verdict": None,
        "switch3_week_key": None,
        "switch3_last_result": None,
        "switch3_last_alerted_verdict": None,
    }


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    now = datetime.now(KST)
    today = now.date()
    today_s = today.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    dates = cfg["send_dates"]
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today_s <= cfg["window_end"]
    print(f"{stamp} mode={mode} today={today_s} window={in_window}")

    # One-shot print window (unchanged), then optional weekly switch3.
    phase = None
    if mode not in {"force", "dry", "switch3"} and (not in_year or not in_window):
        # switch3 has its own active_from..expire_date; do not require year_guard==2026
        if switch3_due(cfg, state, today, mode):
            phase = "switch3"
        else:
            print("outside sndk window; silent")
            return 0

    if phase is None and mode != "switch3":
        if today_s == dates["kr_pre"] and (mode in {"force", "dry"} or not state.get("kr_pre_notified")):
            phase = "kr_pre"
        elif today_s == dates["sndk_pre"] and (mode in {"force", "dry"} or not state.get("sndk_pre_notified")):
            phase = "sndk_pre"
        elif today_s == dates["sndk_post"] and (mode in {"force", "dry"} or not state.get("sndk_post_notified")):
            phase = "sndk_post"
        elif mode in {"force", "dry"}:
            if today_s <= dates["kr_pre"]:
                phase = "kr_pre"
            elif today_s <= dates["sndk_pre"]:
                phase = "sndk_pre"
            else:
                phase = "sndk_post"

    if mode == "switch3" or (phase is None and switch3_due(cfg, state, today, mode)):
        phase = "switch3"
    # dry + explicit switch3: python scripts/sndk_monitor.py dry switch3
    if mode == "dry" and len(sys.argv) > 2 and sys.argv[2].lower() == "switch3":
        phase = "switch3"

    if phase is None:
        print("not a send date or already sent; silent")
        return 0

    if phase == "switch3":
        result = evaluate_switch3(cfg, today)
        title = switch3_title(cfg, result["verdict"])
        lines = switch3_lines(cfg, stamp, result)
        s3 = cfg.get("switch3") or {}
        pri = {
            "ON": int(s3.get("on_priority") or 4),
            "OFF": int(s3.get("off_priority") or 4),
            "HOLD": int(s3.get("hold_priority") or 3),
        }[result["verdict"]]
        iso = today.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        print(title)
        print("\n".join(lines))
        print("switch3", json.dumps({"verdict": result["verdict"], "valid": result["valid_count"], "pass": result["pass_count"]}, ensure_ascii=False))
        change_only = bool(s3.get("alert_on_change_only", True))
        should_emit = (not change_only) or (state.get("switch3_last_alerted_verdict") != result["verdict"])
        if mode == "dry":
            print("dry-run; no ntfy")
            return 0
        if should_emit:
            emit(cfg, title, lines, pri)
            state["switch3_last_alerted_verdict"] = result["verdict"]
        else:
            print("switch3 unchanged; ntfy skipped")
        state["switch3_verdict"] = result["verdict"]
        state["switch3_week_key"] = week_key
        state["switch3_last_result"] = result
        state["last_phase"] = "switch3"
        save_state(state, now)
        return 0

    ir_ok, headline = True, ""
    if phase == "sndk_post":
        ir_ok, headline = ir_headline(cfg["ir_url"])

    if phase == "kr_pre":
        title, lines = kr_pre(cfg, stamp)
        flag = "kr_pre_notified"
        pri = 3
    elif phase == "sndk_pre":
        title, lines = sndk_pre(cfg, stamp)
        flag = "sndk_pre_notified"
        pri = 4
    else:
        title, lines = sndk_post(cfg, stamp, ir_ok, headline)
        flag = "sndk_post_notified"
        pri = 4

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0

    emit(cfg, title, lines, pri)
    state[flag] = True
    state["last_phase"] = phase
    if phase == "sndk_post" and not ir_ok:
        state["fail_notified"] = True
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
