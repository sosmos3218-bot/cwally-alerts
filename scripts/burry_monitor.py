#!/usr/bin/env python3
"""박병창 쇼츠 바뀌는 조건. 매수/매도 신호가 아님.

EXIT_REVIEW: HY OAS 수준 또는 4주 확대, IG OAS 4주 확대,
또는 (실적 추정 하향 AND 선행 PER 4주 하락)이 확인될 때.
DELAY: 기준금리 상단 인하와 실적 추정 상향이 같이 확인될 때만.
조회 실패와 이력 부족은 조건 충족이 아니다.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "burry.json"
STATE_PATH = ROOT / "state" / "burry.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    base = dict(default)
    base.update(data)
    return base


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_fred(text: str) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if not raw or raw == ".":
            continue
        try:
            rows.append((parts[0].strip(), float(raw)))
        except ValueError:
            continue
    return rows


def value_on_or_before(rows: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    picked = None
    for d, v in rows:
        if d <= day:
            picked = (d, v)
        else:
            break
    return picked


def latest(rows: list[tuple[str, float]]) -> tuple[str, float] | None:
    return rows[-1] if rows else None


def bp(now: float, past: float) -> float:
    return (now - past) * 100.0


def yahoo_summary(url: str) -> dict:
    text = fetch_text(url, timeout=15)
    data = json.loads(text)
    result = data["quoteSummary"]["result"][0]
    stats = result.get("defaultKeyStatistics") or {}
    trend = result.get("earningsTrend") or {}
    fwd = (stats.get("forwardPE") or {}).get("raw")
    trail = (stats.get("trailingPE") or {}).get("raw")
    revision = "확인불가"
    trends = trend.get("trend") or []
    current = next((row for row in trends if row.get("period") == "0q"), None)
    if current:
        eps = current.get("epsTrend") or {}
        cur = (eps.get("current") or {}).get("raw")
        ago = (eps.get("30daysAgo") or {}).get("raw")
        if cur is None or ago is None:
            revision = "확인불가"
        elif cur > ago:
            revision = "상향"
        elif cur < ago:
            revision = "하향"
        else:
            revision = "유지"
    return {
        "forward_pe": float(fwd) if fwd is not None else None,
        "trailing_pe": float(trail) if trail is not None else None,
        "eps_revision": revision,
        "src": "live",
    }


def decide(hy: dict, ig: dict, fed: dict, earn: dict, rules: dict) -> dict:
    reasons: list[str] = []
    missing: list[str] = []
    exit_hit = False
    delay_hit = False

    if hy.get("latest") is None:
        missing.append("하이일드 OAS 조회 실패")
    else:
        level = hy["latest"]
        if level >= float(rules["hy_level_pct"]):
            exit_hit = True
            reasons.append(f"하이일드 OAS {level:.2f}% (기준 {rules['hy_level_pct']}%)")
        if hy.get("chg_bp") is None:
            missing.append("하이일드 4주 변화 이력 부족")
        elif hy["chg_bp"] >= float(rules["hy_4w_widen_bp"]):
            exit_hit = True
            reasons.append(f"하이일드 OAS 4주 {hy['chg_bp']:+.0f}bp")

    if ig.get("latest") is None:
        missing.append("투자등급 OAS 조회 실패")
    elif ig.get("chg_bp") is None:
        missing.append("투자등급 4주 변화 이력 부족")
    elif ig["chg_bp"] >= float(rules["ig_4w_widen_bp"]):
        exit_hit = True
        reasons.append(f"투자등급 OAS 4주 {ig['chg_bp']:+.0f}bp")

    rev = earn.get("eps_revision") or "확인불가"
    pe_drop = earn.get("pe_drop")
    if rev == "확인불가":
        missing.append("실적 추정 확인불가")
    if pe_drop is None:
        missing.append("선행 PER 4주 변화 확인불가")
    if rev == "하향" and pe_drop is not None and pe_drop >= float(rules["pe_4w_drop"]):
        exit_hit = True
        reasons.append(f"실적 추정 하향, 선행 PER 4주 -{pe_drop:.1f}배")

    cut = fed.get("cut_pct")
    if fed.get("latest") is None:
        missing.append("기준금리 상단 조회 실패")
    elif cut is None:
        missing.append("기준금리 인하 폭 확인불가")
    if cut is not None and cut >= float(rules["fed_cut_pct"]) and rev == "상향":
        delay_hit = True
        reasons.append(f"기준금리 상단 {cut:.2f}%p 인하, 실적 추정 상향")
    elif cut is not None and cut >= float(rules["fed_cut_pct"]) and rev != "상향":
        missing.append("금리 인하만 확인, 실적 상향은 없음")

    if exit_hit:
        signal = "exit_review"
    elif delay_hit:
        signal = "delay"
    else:
        signal = "none"
    core_fail = hy.get("latest") is None and ig.get("latest") is None
    return {
        "signal": signal,
        "reasons": reasons,
        "missing": missing,
        "core_fail": core_fail,
    }


def copy_for(kind: str, cfg: dict, dec: dict, snap: dict) -> tuple[str, list[str], int]:
    hy = snap["hy"]
    ig = snap["ig"]
    fed = snap["fed"]
    earn = snap["earn"]
    hy_txt = "확인불가" if hy.get("latest") is None else f"{hy['latest']:.2f}% ({hy.get('date')})"
    ig_txt = "확인불가" if ig.get("latest") is None else f"{ig['latest']:.2f}% ({ig.get('date')})"
    fed_txt = "확인불가" if fed.get("latest") is None else f"{fed['latest']:.2f}% ({fed.get('date')})"
    pe_txt = "확인불가" if earn.get("forward_pe") is None else f"{earn['forward_pe']:.1f}배"
    status = (
        f"하이일드 OAS {hy_txt}, 4주 {hy.get('chg_bp') if hy.get('chg_bp') is not None else '확인불가'}bp | "
        f"투자등급 OAS {ig_txt}, 4주 {ig.get('chg_bp') if ig.get('chg_bp') is not None else '확인불가'}bp | "
        f"기준금리 상단 {fed_txt} | 선행 PER {pe_txt} | 실적 추정 {earn.get('eps_revision')}"
    )
    missing = " / ".join(dec.get("missing") or []) or "없음"
    if kind == "FETCH_FAIL":
        return (
            "[FLOW] 조회 실패 | 판단 보류",
            [
                f"이게 뭐 알림: {cfg['explain']}",
                "지금 결과: 하이일드와 투자등급 스프레드를 모두 못 읽었습니다. 실패를 정상이나 위기 신호로 보지 마세요.",
                "지금 할 일: 포지션을 이 알림 때문에 바꾸지 마세요. FRED OAS를 직접 확인하세요.",
                "나중에: 다음 평일 09:00 KST에 다시 읽습니다. 같은 날 실패 알림은 한 번만 갑니다.",
                "익절/줄일 때: 해당 없음.",
                "하지 말 것: 조회 실패를 청산 신호로 보기. 신용, 미수 확대.",
            ],
            4,
        )
    if kind == "exit_review":
        why = " / ".join(dec["reasons"]) or "조건 충족"
        return (
            "[FLOW] 신용·실적 조건 | 청산 재평가",
            [
                f"이게 뭐 알림: {cfg['explain']}",
                f"지금 결과: 청산 재평가. {why}. {status}.",
                "지금 할 일: 신규 비중을 늘리지 마세요. 이미 정한 손실 한도를 넘는 포지션만 줄일지 보세요.",
                "나중에: 스프레드가 기준 아래로 내려오고 4주 확대가 풀리면 해제 알림이 갑니다.",
                "익절/줄일 때: 이 알림만으로 전량 매도하지 마세요. 정해 둔 손실 한도만 실행하세요.",
                "하지 말 것: 이 알림을 매도 명령으로 보기. 신용, 미수 확대. 확인불가 항목을 충족으로 세기.",
                f"확인불가: {missing}",
            ],
            4,
        )
    if kind == "delay":
        why = " / ".join(dec["reasons"]) or "금리 인하와 실적 상향"
        return (
            "[FLOW] 금리인하+실적상향 | 시점 후퇴",
            [
                f"이게 뭐 알림: {cfg['explain']}",
                f"지금 결과: 위기 시점 전망을 뒤로 미룸. {why}. {status}.",
                "지금 할 일: 보유는 유지하세요. 레버리지는 그대로 쓰지 마세요.",
                "나중에: 스프레드가 다시 확대되면 청산 재평가로 바뀝니다.",
                "익절/줄일 때: 시점 후퇴를 익절 신호로 쓰지 마세요.",
                "하지 말 것: 금리 인하만으로 위기 해소로 단정. 신용, 미수 확대.",
                f"확인불가: {missing}",
            ],
            3,
        )
    return (
        "[FLOW] 조건 해제 | 유지",
        [
            f"이게 뭐 알림: {cfg['explain']}",
            f"지금 결과: 앞서 켜졌던 조건이 해제됐습니다. {status}.",
            "지금 할 일: 추가 행동 없음. 기존 비중 규칙을 유지하세요.",
            "나중에: 조건이 다시 맞으면 그때만 알림이 갑니다.",
            "익절/줄일 때: 해당 없음.",
            "하지 말 것: 해제를 매수 신호로 보기. 신용, 미수 확대.",
            f"확인불가: {missing}",
        ],
        3,
    )


def series_view(rows: list[tuple[str, float]] | None, lookback_days: int, asof: datetime) -> dict:
    if not rows:
        return {"latest": None, "date": None, "past": None, "past_date": None, "chg_bp": None}
    last = latest(rows)
    assert last is not None
    past_day = (asof.date() - timedelta(days=lookback_days)).isoformat()
    prev = value_on_or_before(rows, past_day)
    chg = None if prev is None else round(bp(last[1], prev[1]), 1)
    return {
        "latest": last[1],
        "date": last[0],
        "past": None if prev is None else prev[1],
        "past_date": None if prev is None else prev[0],
        "chg_bp": chg,
    }


def fed_view(rows: list[tuple[str, float]] | None) -> dict:
    if not rows:
        return {"latest": None, "date": None, "prev": None, "prev_date": None, "cut_pct": None}
    last_d, last_v = rows[-1]
    prev = None
    for d, v in reversed(rows[:-1]):
        if abs(v - last_v) >= 0.01:
            prev = (d, v)
            break
    cut = None
    if prev is not None and prev[1] > last_v:
        cut = round(prev[1] - last_v, 2)
    elif prev is not None:
        cut = 0.0
    return {
        "latest": last_v,
        "date": last_d,
        "prev": None if prev is None else prev[1],
        "prev_date": None if prev is None else prev[0],
        "cut_pct": cut,
    }


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = load_json(CFG_PATH, {})
    rules = cfg.get("rules") or {}
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_json(
        STATE_PATH,
        {"last_signal": None, "last_sent_signal": None, "fail_sent_date": None, "updated_at": None},
    )
    lookback = int(rules.get("lookback_days", 28))
    errors: list[str] = []

    hy_rows = ig_rows = fed_rows = None
    try:
        hy_rows = parse_fred(fetch_text(cfg["sources"]["hy_oas"]))
    except Exception as exc:
        errors.append(f"하이일드 OAS 조회 실패: {exc}")
    try:
        ig_rows = parse_fred(fetch_text(cfg["sources"]["ig_oas"]))
    except Exception as exc:
        errors.append(f"투자등급 OAS 조회 실패: {exc}")
    try:
        fed_rows = parse_fred(fetch_text(cfg["sources"]["fed_upper"]))
    except Exception as exc:
        errors.append(f"기준금리 조회 실패: {exc}")

    earn = {"forward_pe": None, "trailing_pe": None, "eps_revision": "확인불가", "pe_drop": None, "src": "확인불가"}
    try:
        earn = yahoo_summary(cfg["sources"]["spx_summary"])
    except Exception as exc:
        errors.append(f"실적/PER 조회 실패: {exc}")
        earn["src"] = "확인불가"

    hist = state.get("pe_history") or []
    if earn.get("forward_pe") is not None:
        hist = [row for row in hist if row.get("date") != today]
        hist.append({"date": today, "forward_pe": earn["forward_pe"]})
        hist = hist[-40:]
    past_day = (now.date() - timedelta(days=lookback)).isoformat()
    old_pe = None
    for row in hist:
        if row.get("date") <= past_day and row.get("forward_pe") is not None:
            old_pe = float(row["forward_pe"])
    if earn.get("forward_pe") is not None and old_pe is not None:
        earn["pe_drop"] = round(old_pe - float(earn["forward_pe"]), 2)
    else:
        earn["pe_drop"] = None

    hy = series_view(hy_rows, lookback, now)
    ig = series_view(ig_rows, lookback, now)
    fed = fed_view(fed_rows)
    dec = decide(hy, ig, fed, earn, rules)
    kind = "FETCH_FAIL" if dec["core_fail"] else dec["signal"]
    snap = {"hy": hy, "ig": ig, "fed": fed, "earn": earn, "errors": errors}
    title, body, pri = copy_for(kind, cfg, dec, snap)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | FLOW | 바뀌는 조건", *body]
    if errors:
        lines.append("조회 메모: " + " / ".join(errors))
    msg = "\n".join(lines)
    print(msg)
    print(f"kind={kind} signal={dec['signal']} dry={dry}")

    send = False
    if kind == "FETCH_FAIL":
        send = state.get("fail_sent_date") != today
    elif kind in {"exit_review", "delay"}:
        send = state.get("last_sent_signal") != kind
    elif kind == "none" and state.get("last_sent_signal") in {"exit_review", "delay"}:
        send = True

    if mode == "force":
        send = True
    if dry:
        print("dry-run; no ntfy")
        return 0
    if send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["warning"] if kind in {"exit_review", "FETCH_FAIL"} else ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        if kind == "FETCH_FAIL":
            state["fail_sent_date"] = today
        else:
            state["last_sent_signal"] = kind
            state["fail_sent_date"] = None
    else:
        print("burry silent; signal unchanged or condition not met")

    state["last_signal"] = dec["signal"]
    state["updated_at"] = now.isoformat()
    state["pe_history"] = hist
    state["last_snapshot"] = {
        "kind": kind,
        "signal": dec["signal"],
        "reasons": dec["reasons"],
        "missing": dec["missing"],
        "hy": hy,
        "ig": ig,
        "fed": fed,
        "earn": {k: earn.get(k) for k in ("forward_pe", "eps_revision", "pe_drop", "src")},
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
