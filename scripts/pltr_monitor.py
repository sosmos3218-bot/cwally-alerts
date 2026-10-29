#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

PLTR25_STATE = ROOT / "state" / "pltr25.json"
PLTR25_CFG = ROOT / "configs" / "pltr25.json"
BANNED = ("\u00b7", "\u2014")

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "pltr.json"
STATE_PATH = ROOT / "state" / "pltr.json"
# SEC/BLS block anonymous UAs; they require a contact (https://www.sec.gov/os/accessing-edgar-data)
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}

DATE_RE = re.compile(
    r"announces date of (third|3rd) quarter 2026 earnings|third quarter 2026 earnings release and webcast",
    re.I,
)
RESULT_RE = re.compile(
    r"palantir reports q3 2026|reports third quarter 2026|q3 2026 u\.\?s\.? comm|"
    r"third quarter ended september 30, 2026",
    re.I,
)
Q2_FALSE = re.compile(r"second quarter 2026|q2 2026", re.I)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def visible(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_num(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_unit_bn(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "billion").lower()
    if unit.startswith("million"):
        return val / 1000.0
    return val


def parse_metrics(blob: str) -> dict:
    return {
        "rev_bn": parse_unit_bn(blob, r"revenue[^\.]{0,80}(?:grew|was|of)?[^\.]{0,40}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)"),
        "rev_yoy": parse_num(blob, r"revenue[^\.]{0,80}(?:grew|increased)[^\.]{0,40}?([0-9]+(?:\.[0-9]+)?)%\s*year"),
        "us_comm_bn": parse_unit_bn(blob, r"u\.?s\.?\s*commercial revenue[^\.]{0,80}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(million|billion)"),
        "us_comm_yoy": parse_num(blob, r"u\.?s\.?\s*commercial revenue[^\.]{0,80}?([0-9]+(?:\.[0-9]+)?)%\s*year"),
        "us_comm_qoq": parse_num(blob, r"u\.?s\.?\s*commercial revenue[^\.]{0,80}?([0-9]+(?:\.[0-9]+)?)%\s*(?:quarter|q/q|sequen)"),
        "tcv_bn": parse_num(blob, r"(?:overall |total )?tcv[^\.]{0,60}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*billion"),
        "us_comm_tcv_bn": parse_num(blob, r"u\.?s\.?\s*commercial tcv[^\.]{0,60}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*billion"),
        "deals_1m": parse_num(blob, r"closed\s+([0-9]+)\s+deals of at least \$1 million"),
        "fy_guide_lo": parse_num(blob, r"revenue guidance to between \$([0-9]+(?:\.[0-9]+)?)\s*(?:–|-|to)"),
        "guide_cut": bool(re.search(r"lowering (our )?(full[- ]year|revenue) guidance", blob, re.I)),
        "guide_raise": bool(re.search(r"raising (our )?(full[- ]year|revenue) guidance", blob, re.I)),
    }


def classify(text: str) -> tuple[str, str]:
    blob = visible(text)
    if RESULT_RE.search(blob):
        return "results", blob[:900]
    if DATE_RE.search(blob) and not RESULT_RE.search(blob):
        return "date", blob[:900]
    return "none", blob[:200]


def judge(cfg: dict, metrics: dict) -> tuple[str, str]:
    base = cfg["q2_baseline"]
    th = cfg["thresholds"]
    reasons = []
    met = False
    watch = False
    usy = metrics.get("us_comm_yoy")
    ry = metrics.get("rev_yoy")
    qoq = metrics.get("us_comm_qoq")
    rev = metrics.get("rev_bn")
    if usy is not None:
        if usy < th["us_comm_yoy_met"]:
            met = True
            reasons.append(f"미국 커머셜 YoY {usy:.0f}% < {th['us_comm_yoy_met']}%")
        elif usy < th["us_comm_yoy_watch"]:
            watch = True
            reasons.append(f"미국 커머셜 YoY {usy:.0f}% < 감시 {th['us_comm_yoy_watch']}% (Q2 {base['us_comm_yoy']}%)")
    if ry is not None:
        if ry < th["rev_yoy_met"]:
            met = True
            reasons.append(f"전사 매출 YoY {ry:.0f}% < {th['rev_yoy_met']}%")
        elif ry < th["rev_yoy_watch"]:
            watch = True
            reasons.append(f"전사 매출 YoY {ry:.0f}% < 감시 {th['rev_yoy_watch']}% (Q2 {base['rev_yoy']}%)")
    if qoq is not None and qoq < 0:
        met = True
        reasons.append(f"미국 커머셜 QoQ {qoq:.1f}% 감소")
    if rev is not None and rev < base["q3_guide_rev_lo"]:
        met = True
        reasons.append(f"매출 {rev:.3f}B < Q3 가이던스 {base['q3_guide_rev_lo']}B")
    if metrics.get("guide_cut"):
        met = True
        reasons.append("연간 가이던스 하향 문구")
    if met:
        return "일부 충족", "; ".join(reasons) or "성장 둔화 신호"
    if watch:
        return "미충족, 감시", "; ".join(reasons)
    if any(metrics.get(k) is not None for k in ("rev_yoy", "us_comm_yoy", "rev_bn")):
        return "미충족", "성장, 수주 숫자가 둔화 기준을 넘지 않음"
    return "확인 불가", "실적은 보이나 성장, 수주 숫자를 파싱하지 못함"


def fmt_metric(val, suffix=""):
    if val is None:
        return "파싱 실패"
    if suffix == "%":
        return f"{val:.1f}%"
    if suffix == "B":
        return f"{val:.3f}B"
    if suffix == "n":
        return f"{val:.0f}건"
    return str(val)


# ---------------- 신규 매수 한 줄 (미보유, 2026-10-29 14:00 KST 채팅 승인) ----------------
def pltr25_zone(now: datetime, state_path: Path = PLTR25_STATE, recompute=None) -> dict | None:
    """pltr25 가격 구간. state/pltr25.json 이 오늘(KST) 갱신분이면 그대로, 아니면 pltr25 구간 함수로 다시 계산."""
    st = None
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        st = None
    fresh = bool(st and st.get("zone") and str(st.get("updated_at") or "")[:10] == now.date().isoformat())
    if fresh:
        return {"zone": st["zone"], "close": st.get("close"), "date": st.get("last_date"), "src": "state"}
    try:
        if recompute is None:
            import pltr25_monitor as p25
            cfg25 = json.loads(PLTR25_CFG.read_text(encoding="utf-8"))
            r = p25.replay(p25.parse_yahoo(p25.fetch(cfg25["sources"]["pltr"])), cfg25["zones"])
            return {"zone": r["zone"], "close": round(r["close"], 2), "date": r["date"].isoformat(), "src": "recompute"}
        return recompute()
    except Exception as exc:
        print(f"pltr25 zone recompute fail: {exc}")
    return None


def buy_line(verdict: str, zone: dict | None) -> str:
    z = (zone or {}).get("zone")
    if not verdict or verdict == "확인 불가" or not z:
        text = "확인 필요"
    elif verdict == "일부 충족":
        text = "보류"
    elif z == "추세 이탈":
        text = "보류 (추세 이탈)"
    elif z == "추격 금지":
        text = "관망 (추격 금지)"
    elif z in ("1차 매수", "2차 매수"):
        text = "매수 검토 (분할)"
    elif z == "관망":
        text = "관망 (가격 구간 대기)"
    else:
        text = "확인 필요"
    line = f"신규 매수: {text}"
    if z:
        line += f". 미보유 기준, [PLTR25] 구간 {z}"
        if zone.get("close") is not None:
            line += f" (미국 {zone.get('date')} 종가 ${float(zone['close']):.2f}, 실적 뒤 시간외 움직임은 미반영)"
        line += "."
    else:
        line += ". [PLTR25] 가격 구간을 읽지 못했습니다."
    return line


def body(cfg: dict, now: datetime, kind: str, source: str, metrics: dict | None, verdict: str, why: str, buy: str = "") -> str:
    base = cfg["q2_baseline"]
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | PLTR | Q3 2026",
        "이게 뭐 알림: 팔란티어 3분기 실적이 나와 김승호 매도조건 중 성장, 수주만 다시 보라는 알림입니다.",
    ]
    if kind == "date":
        lines += [
            "지금 결과/상태: 발표일 공지는 있으나 실적 숫자는 아직 없습니다.",
            "지금 할 일: 숫자 나오기 전 재평가 금지. 추정일(11/2 또는 11/9 장후)만 달력에 두세요.",
            "나중에: 실적 알림이 오면 성장, 수주 행만 갱신하세요.",
            "익절/줄일 때: 이번 알림으로 매도하지 마세요.",
            "하지 말 것: 미확정 일정을 확정으로 취급, 주가 등락만으로 조건 충족이라고 단정.",
        ]
    elif kind == "results":
        m = metrics or {}
        lines += [
            f"지금 결과/상태: Q3 실적 공시로 보입니다. 성장, 수주 조건 {verdict}. {why}.",
            f"매출 YoY {fmt_metric(m.get('rev_yoy'), '%')} (Q2 {base['rev_yoy']}%) | 미국 커머셜 YoY {fmt_metric(m.get('us_comm_yoy'), '%')} (Q2 {base['us_comm_yoy']}%) | QoQ {fmt_metric(m.get('us_comm_qoq'), '%')}",
            f"매출 {fmt_metric(m.get('rev_bn'), 'B')} / Q3 가이드 {base['q3_guide_rev_lo']}~{base['q3_guide_rev_hi']}B | TCV {fmt_metric(m.get('tcv_bn'), 'B')} | $1M+ 계약 {fmt_metric(m.get('deals_1m'), 'n')}",
            f"가이던스: {'하향 문구' if m.get('guide_cut') else ('상향 문구' if m.get('guide_raise') else '문구 불명')}",
            buy or buy_line(verdict, None),
            "지금 할 일: 성장, 수주 행만 표에 반영하세요. 경영진, 정책 행은 이 알림만으로 바꾸지 마세요.",
            "나중에: 파싱 실패 칸은 IR/8-K 원문을 직접 확인하세요.",
            "익절/줄일 때: 미보유라 해당 없음. (보유 시) 조건이 '일부 충족'이면 비중 축소 여부를 재검토. '미충족'이면 가격만으로 매도 근거로 쓰지 마세요.",
            "하지 말 것: 주가 반응, 밸류에이션, 미확인 숫자를 조건 충족으로 취급.",
        ]
    elif kind == "miss":
        lines += [
            "지금 결과/상태: 점검 기간이 끝났는데 Q3 실적 문서를 확인하지 못했습니다. 조회 실패/일정 지연 가능.",
            "지금 할 일: IR, SEC를 직접 확인하세요. 조건을 미충족으로 유지하지 말고 '미확인'으로 두세요.",
            "나중에: 실적이 보이면 이 알림을 다시 돌리세요(workflow_dispatch job=pltr).",
            "익절/줄일 때: 이번 알림은 매도 신호가 아닙니다.",
            "하지 말 것: 실패를 정상 발표로 취급.",
        ]
    else:
        lines += [
            "지금 결과/상태: 조회 실패가 반복됐습니다.",
            "지금 할 일: 출처를 직접 확인하세요.",
            "나중에: 다음 평일 장전, 장후 슬롯에서 재시도합니다.",
            "익절/줄일 때: 매도 신호 아님.",
            "하지 말 것: 실패를 실적 발표로 취급.",
        ]
    if source:
        lines += ["", f"출처: {source}"]
    return "\n".join(lines)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    dry = mode == "dry"
    now = datetime.now(KST)
    today = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = {
        "updated_at": None,
        "phase": "idle",
        "date_notified": False,
        "results_notified": False,
        "miss_notified": False,
        "source": None,
        "snippet": None,
        "parsed": None,
        "fetch_fails": 0,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    start = date.fromisoformat(cfg["window_start"])
    end = date.fromisoformat(cfg["window_end"])
    year_ok = today.year == int(cfg["oneshot_year"])
    in_window = year_ok and start <= today <= end
    if mode not in {"force", "event", "dry"} and not in_window:
        print(f"outside window {start}~{end} year={cfg['oneshot_year']}; silent")
        return 0
    if state.get("results_notified"):
        print("already sent results; skip")
        return 0

    kind, source, snippet, metrics = "none", "", "", {}
    fails = 0
    for url in cfg["sources"]:
        try:
            raw = fetch(url)
        except Exception as exc:
            print(f"fetch fail {url}: {exc}")
            fails += 1
            continue
        guess, snip = classify(raw)
        print(f"{url} -> {guess}")
        if guess == "results":
            kind, source, snippet = guess, url, snip
            metrics = parse_metrics(visible(raw) + " " + snip)
            break
        if guess == "date" and kind != "results":
            kind, source, snippet = guess, url, snip
    if fails == len(cfg["sources"]):
        kind = "fail"
    state["fetch_fails"] = fails

    send = False
    title = "[PLTR] 점검 대기"
    verdict, why, buy = "", "", ""
    if kind == "results" and not state.get("results_notified"):
        verdict, why = judge(cfg, metrics)
        buy = buy_line(verdict, pltr25_zone(now))
        send = True
        state["results_notified"] = True
        state["date_notified"] = True
        state["phase"] = "results"
        title = f"[PLTR] Q3 나옴 | 성장, 수주 {verdict}"
    elif kind == "date" and not state.get("date_notified") and not state.get("results_notified"):
        send = True
        state["date_notified"] = True
        state["phase"] = "date"
        title = "[PLTR] 발표일 공지 | 숫자는 아직"
    elif (
        kind in {"none", "fail"}
        and today >= end
        and year_ok
        and not state.get("results_notified")
        and not state.get("miss_notified")
    ):
        send = True
        kind = "miss"
        state["miss_notified"] = True
        state["phase"] = "miss"
        title = "[PLTR] 기간 종료 | 실적 미확인"
    elif kind == "fail" and fails == len(cfg["sources"]) and state.get("fetch_fails_sent") != today.isoformat():
        expected = {date.fromisoformat(x) for x in cfg.get("expected_dates_kst", [])}
        if today in expected:
            send = True
            state["fetch_fails_sent"] = today.isoformat()
            title = "[PLTR] 조회 실패 | 직접 확인"

    msg = body(cfg, now, kind, source, metrics, verdict, why, buy)
    print(msg)
    print(f"title={title}")
    print(f"topic={topic} send={send and not dry}")
    if dry:
        print(f"zone now: {buy_line('미충족', pltr25_zone(now))}")
        print("DRY RUN: no ntfy, no state write")
        return 0
    if send:
        pri = 5 if kind in {"results", "miss", "fail"} else 3
        publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)

    state.update(
        {
            "updated_at": now.isoformat(),
            "source": source or state.get("source"),
            "snippet": snippet[:280] if snippet else state.get("snippet"),
            "parsed": metrics or state.get("parsed"),
        }
    )
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    ok, fails = 0, []

    def check(label, got, want=True):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            fails.append(f"{label}: got {got!r} want {want!r}")

    z = lambda name, close=196.81: {"zone": name, "close": close, "date": "2026-11-02", "src": "state"}
    check("not met + 1차 -> 매수 검토", buy_line("미충족", z("1차 매수")).startswith("신규 매수: 매수 검토 (분할)."))
    check("watch + 2차 -> 매수 검토", buy_line("미충족, 감시", z("2차 매수")).startswith("신규 매수: 매수 검토 (분할)."))
    check("partly met -> 보류", buy_line("일부 충족", z("1차 매수")).startswith("신규 매수: 보류."))
    check("partly met beats chase", buy_line("일부 충족", z("추격 금지")).startswith("신규 매수: 보류."))
    check("not met + 관망", buy_line("미충족", z("관망")).startswith("신규 매수: 관망 (가격 구간 대기)."))
    check("not met + 추격 금지", buy_line("미충족", z("추격 금지")).startswith("신규 매수: 관망 (추격 금지)."))
    check("not met + 추세 이탈", buy_line("미충족", z("추세 이탈")).startswith("신규 매수: 보류 (추세 이탈)."))
    check("zone missing -> 확인 필요", buy_line("미충족", None), "신규 매수: 확인 필요. [PLTR25] 가격 구간을 읽지 못했습니다.")
    check("verdict unparsed -> 확인 필요", buy_line("확인 불가", z("1차 매수")).startswith("신규 매수: 확인 필요."))
    check("line has zone date and price", "미국 2026-11-02 종가 $196.81" in buy_line("미충족", z("관망")))

    # 구간 읽기: 오늘 state 면 그대로, 오래됐으면 다시 계산, 실패면 None
    import tempfile
    now = datetime(2026, 11, 3, 9, 0, tzinfo=KST)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "pltr25.json"
        p.write_text(json.dumps({"zone": "관망", "close": 196.81, "last_date": "2026-11-02", "updated_at": "2026-11-03T09:00:30+09:00"}), encoding="utf-8")
        check("fresh state used", pltr25_zone(now, p, recompute=lambda: {"zone": "X"})["src"], "state")
        p.write_text(json.dumps({"zone": "관망", "close": 196.81, "last_date": "2026-10-29", "updated_at": "2026-10-30T09:00:30+09:00"}), encoding="utf-8")
        check("stale state recomputed", pltr25_zone(now, p, recompute=lambda: {"zone": "1차 매수", "close": 180.0, "date": "2026-11-02", "src": "recompute"})["zone"], "1차 매수")
        def boom():
            raise RuntimeError("yahoo down")
        check("recompute fail -> None", pltr25_zone(now, p, recompute=boom), None)
        check("missing state + fail -> None", pltr25_zone(now, Path(td) / "none.json", recompute=boom), None)

    # judge 문구, 본문 결과 줄, 금지 문자
    met = judge(cfg, {"rev_yoy": 30.0, "us_comm_yoy": 120.0, "rev_bn": 2.2})[0]
    watch = judge(cfg, {"rev_yoy": 70.0, "us_comm_yoy": 70.0, "rev_bn": 2.2})[0]
    ok_v = judge(cfg, {"rev_yoy": 90.0, "us_comm_yoy": 120.0, "rev_bn": 2.2})[0]
    check("judge verdicts", (met, watch, ok_v), ("일부 충족", "미충족, 감시", "미충족"))
    bl = buy_line(ok_v, z("관망"))
    msg = body(cfg, now, "results", "https://x", {"rev_yoy": 90.0}, ok_v, "ok", bl)
    check("results body has buy line", bl in msg)
    check("results trim line says 미보유", "익절/줄일 때: 미보유라 해당 없음." in msg)
    check("holder lines kept", "성장, 수주 행만 표에 반영하세요" in msg)
    texts = [msg] + [body(cfg, now, k, "", None, "", "") for k in ("date", "miss", "fail")]
    texts.append(f"[PLTR] Q3 나옴 | 성장, 수주 {watch}")
    texts += [buy_line(v, z(n)) for v in ("미충족", "일부 충족") for n in ("1차 매수", "2차 매수", "관망", "추격 금지", "추세 이탈")]
    texts.append(CFG_PATH.read_text(encoding="utf-8"))
    texts.append(Path(__file__).read_text(encoding="utf-8"))
    check("no middle dot / em-dash in copy, config, script", [t[:40] for t in texts if any(b in t for b in BANNED)], [])

    for f in fails:
        print("FAIL", f)
    print(f"selftest {ok}/{ok + len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
