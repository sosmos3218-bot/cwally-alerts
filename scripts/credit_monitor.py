#!/usr/bin/env python3
"""[CREDIT] 미국 BBB 회사채 스프레드 (FRED BAMLC0A4CBBB, ICE BofA BBB US Corporate Index OAS, %p). 평일 09:00 KST morning 묶음 (hwm 뒤, xlu 앞).

메모리 호황 전제의 돈줄 신호. 오라클 BBB- 투기등급 강등이나 BBB 스프레드 1.9%p 초과는 AI 설비투자 자금조달을 위협한다.
데이터: FRED fredgraph.csv (키 없음), '.' 값은 건너뜀, 최신 관측값 하나로 판정. 최신 관측일이 state.last_date 와 같으면 아무것도 안 함.
단계(configs/credit.json tiers): 1.50 p3 주의, 1.90 p4 경고, 2.50 p5 위험. 위로 넘을 때 1번(한 번에 여러 단계를 넘으면 가장 높은 단계
  1건, 넘은 단계는 모두 해제). 그 단계보다 rearm_gap(0.15%p) 아래(1.35 / 1.75 / 2.35 이하)로 내려가야 다시 무장.
  재무장(2026-11-02 조정): 1.50 은 1.40 미만, 1.90 은 1.70 미만 (tiers.rearm_below), 2.50 은 2.35 이하 (rearm_gap).
완화: 1.90 이상을 찍은 뒤 1.50 아래로 내려오면 p3 1번.
급등(configs/credit.json jump): 최신값 - 5관측 전 값(FRED 영업일 기준, '.' 결측은 건너뜀) >= +0.25%p 이면 p3 1번, +0.10 미만에서 재무장.
  같은 관측에 단계 돌파가 있으면 단계 메시지 한 줄로 합침. state.jump 가 없으면 그 실행에서 조용히 seed.
조회 실패와 스크립트 오류는 cwally-ops 하루 1번. ends_kst(2027-02-26 23:59) 뒤에는 조용히 끝남.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "credit.json"
STATE_PATH = ROOT / "state" / "credit.json"
# FRED 는 브라우저 흉내 UA 요청을 멈춰 두는 경우가 있어(2026-11-02 확인) xlu 와 같은 UA, 실패하면 기본 UA 로 1번 더.
UAS = ({"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}, {"User-Agent": "Python-urllib/3"})
NA = "확인 불가"
TAGS = ["bank"]
HEAD = "LQD 000660 005930 ORCL"
TK = "LQD 000660 005930"


def fetch(url: str, rounds: int = 2, wait: float = 8.0) -> str:
    """UA 2개 x rounds 번 재시도 (라운드 사이 wait 초). 응답이 CSV 가 아니면 실패로 본다."""
    err: Exception | None = None
    for r in range(rounds):
        for ua in UAS:
            try:
                req = urllib.request.Request(url, headers=ua)
                with urllib.request.urlopen(req, timeout=25) as resp:
                    txt = resp.read().decode("utf-8", errors="ignore")
                if "," not in txt.split("\n", 1)[0]:
                    raise ValueError("not a FRED csv")
                return txt
            except Exception as exc:  # noqa: BLE001
                err = exc
                print(f"fetch fail round {r + 1} ({ua['User-Agent'][:20]}): {exc}")
        if r + 1 < rounds:
            time.sleep(wait)
    raise RuntimeError(str(err))


def parse_csv(txt: str) -> list[tuple[str, float]]:
    """FRED fredgraph.csv -> [(YYYY-MM-DD, 값)] 날짜순. '.' 과 빈 값은 건너뜀."""
    out = {}
    for line in txt.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) != 2 or parts[1].strip() in ("", "."):
            continue
        try:
            d = date.fromisoformat(parts[0].strip()).isoformat()
            out[d] = float(parts[1])
        except ValueError:
            continue
    return sorted(out.items())


def fred_series(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    start = (now - timedelta(days=int(cfg.get("lookback_days", 70)))).strftime("%Y-%m-%d")
    return parse_csv(fetch(f"{cfg['sources']['fred_csv']}&cosd={start}"))


# ---------- 판정 ----------
def month_back(d: date) -> date:
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def value_on_or_before(series: list[tuple[str, float]], target: date) -> tuple[str, float] | None:
    best = None
    for d, v in series:
        if d <= target.isoformat():
            best = (d, v)
    return best


def changes(series: list[tuple[str, float]]) -> dict:
    d, v = series[-1]
    dd = date.fromisoformat(d)
    w = value_on_or_before(series, dd - timedelta(days=7))
    m = value_on_or_before(series, month_back(dd))
    return {"w": (w[0], round(v - w[1], 2)) if w else None, "m": (m[0], round(v - m[1], 2)) if m else None}


def jump_of(cfg: dict, series: list[tuple[str, float]]) -> tuple[str, float] | None:
    """최신값 - n관측 전 값 (FRED 관측일 = 미국 영업일, 결측 '.' 은 이미 빠짐). 돌려줌: (기준일, 변화) 또는 None."""
    n = int(cfg.get("jump", {}).get("obs", 5))
    if len(series) <= n:
        return None
    d0, v0 = series[-1 - n]
    return d0, round(series[-1][1] - v0, 2)


def seed_jump(cfg: dict, series: list[tuple[str, float]]) -> dict:
    j = jump_of(cfg, series)
    lvl = float(cfg["jump"]["level"])
    return {"armed": j is None or j[1] < lvl, "last_jump": j[1] if j else None}


def seed_state(cfg: dict, d: str, v: float, series: list[tuple[str, float]] | None = None) -> dict:
    return {"tiers": {t["key"]: {"armed": v < t["level"]} for t in cfg["tiers"]},
            "reached_190": v >= cfg["relief"]["after_reaching"], "last_date": d, "last_value": v,
            "jump": seed_jump(cfg, series or [(d, v)])}


def rearm_ok(cfg: dict, t: dict, v: float) -> bool:
    if "rearm_below" in t:
        return v < float(t["rearm_below"])
    return v <= round(t["level"] - float(cfg["rearm_gap"]), 4)


def rearm_text(cfg: dict, t: dict) -> str:
    if "rearm_below" in t:
        return f"{float(t['rearm_below']):.2f}%p 미만"
    return f"{t['level'] - float(cfg['rearm_gap']):.2f}%p 이하"


def judge_jump(cfg: dict, st: dict, series: list[tuple[str, float]]) -> tuple[str, float] | None:
    """5영업일 급등. 위로 넘을 때 1번, rearm_below 미만에서 재무장. st 를 고친다. 돌려줌: 발동 시 (기준일, 변화)."""
    jc = cfg["jump"]
    s = st.setdefault("jump", {"armed": True})
    j = jump_of(cfg, series)
    if j is None:
        return None
    s["last_jump"] = j[1]
    if s.get("armed", True) and j[1] >= float(jc["level"]):
        s["armed"] = False
        return j
    if not s.get("armed", True) and j[1] < float(jc["rearm_below"]):
        s["armed"] = True
    return None


def judge(cfg: dict, st: dict, v: float) -> dict:
    """값 하나를 판정한다. st 를 고친다. 돌려줌: {"tier": 넘은 가장 높은 단계 dict 또는 None, "crossed": [키], "relief": bool, "rearmed": [키]}"""
    crossed, rearmed = [], []
    for t in cfg["tiers"]:
        s = st["tiers"].setdefault(t["key"], {"armed": True})
        if s["armed"] and v >= t["level"]:
            s["armed"] = False
            crossed.append(t)
        elif not s["armed"] and rearm_ok(cfg, t, v):
            s["armed"] = True
            rearmed.append(t["key"])
    rel = cfg["relief"]
    relief = False
    if v >= rel["after_reaching"]:
        st["reached_190"] = True
    elif v < rel["below"] and st.get("reached_190"):
        st["reached_190"] = False
        relief = True
    top = max(crossed, key=lambda t: t["level"]) if crossed else None
    return {"tier": top, "crossed": [t["key"] for t in crossed], "relief": relief, "rearmed": rearmed}


def current_tier(cfg: dict, v: float) -> str:
    hit = [t for t in cfg["tiers"] if v >= t["level"]]
    if not hit:
        return f"단계 없음 ({cfg['tiers'][0]['level']:g}%p 미만, 정상 범위)"
    t = max(hit, key=lambda x: x["level"])
    return f"{t['label']} ({t['level']:g}%p 이상)"


# ---------- 본문 ----------
def fmt_chg(c: tuple[str, float] | None, name: str) -> str:
    return f"{name} {c[1]:+.2f}%p ({c[0]} 대비)" if c else f"{name} 비교값 {NA}"


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str, oracle: str) -> str:
    return "\n".join([f"{stamp} | {HEAD} | 미국 BBB 회사채 스프레드 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"오라클 등급: {oracle}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def status_line(cfg: dict, d: str, v: float, ch: dict, meaning: str) -> str:
    j = ch.get("j")
    jt = f"5영업일 {j[1]:+.2f}%p ({j[0]} 대비)" if j else f"5영업일 비교값 {NA}"
    return (f"{d} BBB 회사채 스프레드 {v:.2f}%p (FRED {cfg['series']}). {jt}, {fmt_chg(ch['w'], '1주')}, {fmt_chg(ch['m'], '1개월')}. "
            f"단계: {current_tier(cfg, v)}. 의미: {meaning}.")


def tier_title(t: dict, v: float) -> str:
    return f"[CREDIT] LQD 000660 005930 | BBB 회사채 스프레드 {v:.2f}%p, {t['level']:g}%p {t['label']}선 돌파"


def relief_title(cfg: dict, v: float) -> str:
    return f"[CREDIT] LQD 000660 005930 | BBB 회사채 스프레드 {v:.2f}%p, {cfg['relief']['below']:g}%p 아래로 복귀 (경고 해제)"


def jump_title(v: float, j: tuple[str, float]) -> str:
    return f"[CREDIT] LQD 000660 005930 | BBB 회사채 스프레드 5영업일 {j[1]:+.2f}%p 급등, {v:.2f}%p"


ACTIONS = {
    "t150": ("삼성전자/SK하이닉스 계획은 그대로. 스프레드가 1.9%p 쪽으로 계속 오르는지만 지켜보기.",
             "다음 경고선은 1.9%p.", "이 단계에서는 줄이지 않기.", "주의 단계 하나로 메모리 비중 줄이기, 공포 매도."),
    "t190": ("삼성전자/SK하이닉스 신규 분할매수 보류를 검토. 이미 산 물량은 유지.",
             "다음 위험선은 2.5%p. 1.5%p 아래로 내려오면 완화 알림이 옵니다.", "아직 비중 축소 단계는 아님. 2.5%p 위험 단계에서 검토.",
             "경고 단계에서 분할매수 계속 밀어붙이기, 한 번에 전부 정리."),
    "t250": ("분할매수 중단. 삼성전자/SK하이닉스 보유 비중 축소를 검토.",
             "1.5%p 아래로 내려오면 완화 알림이 옵니다. 그 전에는 새로 사지 않기.", "보유 비중 축소 검토. 한 번에 다 팔지 말고 나눠서.",
             "위험 단계에서 물타기, 반등 기대만으로 버티기."),
}


def tier_msg(cfg: dict, j: dict, d: str, v: float, ch: dict, stamp: str) -> tuple[str, str, int]:
    t = j["tier"]
    act, later, trim, avoid = ACTIONS[t["key"]]
    extra = ""
    if len(j["crossed"]) > 1:
        extra = " 한 번에 여러 단계를 넘어 가장 높은 단계로 1건만 보냅니다."
    if j.get("jump"):
        extra += f" 같은 관측에 5영업일 {j['jump'][1]:+.2f}%p 급등도 발생 (급등 알림은 이 메시지에 합침)."
    later = f"{later} 같은 단계는 {rearm_text(cfg, t)}으로 내려갔다가 다시 넘을 때만 다시 옵니다.{extra}"
    what = f"미국 BBB 회사채 스프레드가 {t['level']:g}%p {t['label']}선을 넘었다는 알림. 메모리 호황 전제의 돈줄 신호. 매매 지시가 아닙니다."
    return (tier_title(t, v), body(stamp, what, status_line(cfg, d, v, ch, t["meaning"]), act, later, trim, avoid, cfg["oracle_line"]), t["priority"])


def relief_msg(cfg: dict, d: str, v: float, ch: dict, stamp: str) -> tuple[str, str, int]:
    rel = cfg["relief"]
    what = f"BBB 회사채 스프레드가 {rel['after_reaching']:g}%p 경고선을 찍은 뒤 {rel['below']:g}%p 아래로 내려왔다는 알림. 매매 지시가 아닙니다."
    return (relief_title(cfg, v),
            body(stamp, what, status_line(cfg, d, v, ch, rel["meaning"]),
                 "보류했던 삼성전자/SK하이닉스 분할매수를 원래 계획대로 재개할지 검토. 오라클 등급이 그대로인지 먼저 확인.",
                 "다시 1.9%p 를 찍은 뒤 1.5%p 아래로 내려올 때만 다시 옵니다.", "완화 알림만으로 비중을 늘리거나 줄이지 않기.",
                 "완화 하루 만에 보류분을 한꺼번에 사기.", cfg["oracle_line"]),
            rel["priority"])


def jump_msg(cfg: dict, jv: tuple[str, float], d: str, v: float, ch: dict, stamp: str) -> tuple[str, str, int]:
    jc = cfg["jump"]
    what = (f"미국 BBB 회사채 스프레드가 5영업일 만에 {float(jc['level']):.2f}%p 이상 벌어졌다는 조기 경보. 메모리 호황 전제의 돈줄 신호. "
            "매매 지시가 아닙니다.")
    return (jump_title(v, jv),
            body(stamp, what, status_line(cfg, d, v, ch, jc["meaning"]),
                 "삼성전자/SK하이닉스 계획은 그대로. 다음 며칠 스프레드가 1.5%p, 1.9%p 쪽으로 이어지는지 확인.",
                 f"5영업일 변화가 +{float(jc['rearm_below']):.2f}%p 미만으로 내려갔다가 다시 +{float(jc['level']):.2f}%p 이상 벌어질 때만 다시 옵니다.",
                 "급등 알림 하나로는 줄이지 않기.", "급등 하루로 메모리 비중 정리, 공포 매도.", cfg["oracle_line"]),
            int(jc["priority"]))


def ops_msg(stamp: str, title_tail: str, status: str, oracle: str) -> tuple[str, str, int]:
    return (f"[CREDIT] LQD 000660 005930 | {title_tail}",
            body(stamp, "BBB 회사채 스프레드 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 FRED BAMLC0A4CBBB 를 직접 보세요.",
                 "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 스프레드 안정으로 읽기.", oracle), 3)


def evaluate(cfg: dict, st: dict, series: list[tuple[str, float]], stamp: str) -> tuple[list[tuple[str, str, int]], bool]:
    """최신 관측값으로 판정. 돌려줌: (메시지, state 바뀜 여부). 관측일이 그대로(또는 과거)면 ([], False)."""
    d, v = series[-1]
    if "tiers" not in st:
        st.update(seed_state(cfg, d, v, series))
        print(f"seeded at {d} {v:.2f}%p (판정 없음): {st}")
        return [], True
    if st.get("last_date") and d <= st["last_date"]:
        print(f"latest observation {d} {v:.2f}%p unchanged since last run ({st['last_date']}): nothing to do")
        return [], False
    ch = changes(series)
    ch["j"] = jump_of(cfg, series)
    j = judge(cfg, st, v)
    if "jump" not in st:
        st["jump"] = seed_jump(cfg, series)
        jv = None
        print(f"jump state seeded silently: {st['jump']}")
    else:
        jv = judge_jump(cfg, st, series)
    j["jump"] = jv
    out = []
    if j["tier"]:
        out.append(tier_msg(cfg, j, d, v, ch, stamp))
    elif jv:
        out.append(jump_msg(cfg, jv, d, v, ch, stamp))
    if j["relief"]:
        out.append(relief_msg(cfg, d, v, ch, stamp))
    st["last_date"], st["last_value"] = d, v
    print(f"{d} {v:.2f}%p -> {current_tier(cfg, v)}, crossed={j['crossed']}, rearmed={j['rearmed']}, relief={j['relief']}, "
          f"armed={ {k: s['armed'] for k, s in st['tiers'].items()} }, reached_190={st.get('reached_190')}, jump={ch['j']} fired={bool(jv)} "
          f"jump_armed={st['jump'].get('armed')}")
    return out, True


def ended(cfg: dict, now: datetime) -> bool:
    raw = cfg.get("ends_kst")
    if not raw:
        return False
    return now > datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check_text(t: str, m: str) -> None:
    assert "\u00b7" not in t + m and "\u2014" not in t + m, t
    assert t.startswith(f"[CREDIT] LQD 000660 005930 |"), t


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    if ended(cfg, now):
        print(f"ended {cfg['ends_kst']} KST: silent")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    out: list[tuple[str, str, int]] = []
    try:
        series = fred_series(cfg, now)
        err = None if series else "관측값 없음"
    except Exception as exc:  # noqa: BLE001
        series, err = [], str(exc)[:150]
    changed = False
    if series:
        out, changed = evaluate(cfg, st, series, stamp)
    elif st.get("fetch_fail") != stamp[:10]:
        out.append(ops_msg(stamp, "BBB 스프레드 조회 실패, 판정 못 함", f"FRED {cfg['series']} {NA} ({err}).", cfg["oracle_line"]))
        st["fetch_fail"] = stamp[:10]
        changed = True
    else:
        print(f"fetch fail ({err}); ops notice already sent today")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, TAGS, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    if changed:
        st["updated_at"] = now.isoformat()
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- selftest ----------
def selftest(cfg: dict) -> int:
    stamp = "2026-11-03 09:00 KST"
    oracle = cfg["oracle_line"]
    csv = "observation_date,BAMLC0A4CBBB\n2026-09-29,1.02\n2026-10-22,1.11\n2026-10-23,.\n2026-10-26,1.12\n2026-10-29,1.10\n2026-10-30,\n"
    s = parse_csv(csv)
    assert s == [("2026-09-29", 1.02), ("2026-10-22", 1.11), ("2026-10-26", 1.12), ("2026-10-29", 1.10)], s
    ch = changes(s)
    assert ch["w"] == ("2026-10-22", -0.01) and ch["m"] == ("2026-09-29", 0.08), ch
    assert month_back(date(2026, 3, 31)) == date(2026, 2, 28)
    print(f"csv parse ok ('.' 와 빈 값 건너뜀), 1주 {ch['w']}, 1개월 {ch['m']}")
    # 설치 seed 와 같은 값: 모두 무장, 첫 실행은 판정 없음
    st: dict = {}
    out, changed = evaluate(cfg, st, s, stamp)
    assert out == [] and changed and all(x["armed"] for x in st["tiers"].values()) and not st["reached_190"], st
    out, changed = evaluate(cfg, st, s, stamp)
    assert out == [] and not changed
    print("seed 1.10: 모두 무장, 같은 관측일 재실행은 아무것도 안 함")
    # 단계 순서: (값, 기대 단계 키 또는 None, 완화). 급등 판정은 따로 시험하므로 여기서는 끈다(level 99).
    full_cfg, cfg = cfg, {**cfg, "jump": {**cfg["jump"], "level": 99}}
    st = seed_state(cfg, "2026-10-29", 1.10)
    seq = [
        (1.49, None, False), (1.50, "t150", False), (1.60, None, False), (1.40, None, False), (1.52, None, False),
        (1.39, None, False), (1.50, "t150", False),                       # 1.40 미만으로 내려가야 재무장 (1.40 은 아님)
        (1.93, "t190", False), (1.80, None, False), (1.95, None, False), (1.70, None, False), (1.91, None, False), (1.69, None, False), (1.90, "t190", False),
        (2.49, None, False), (2.50, "t250", False), (2.40, None, False), (2.60, None, False),   # 2.5 는 2.35 이하에서 재무장
        (1.49, None, True), (1.60, None, False), (1.45, None, False),     # 완화는 1.9 를 다시 찍기 전까지 1번
        (1.30, None, False), (2.55, "t250", False), (1.40, None, True),   # 한 번에 세 단계: 가장 높은 단계 1건
        (1.50, None, False),                                              # 1.40 은 1.5 재무장 아님 -> 1.50 재돌파 조용
    ]
    day = date(2026, 11, 2)
    series = [("2026-10-29", 1.10)]
    for v, want, wrel in seq:
        day += timedelta(days=1)
        series.append((day.isoformat(), v))
        out, _ = evaluate(cfg, st, list(series), stamp)
        got = [m[0] for m in out]
        tiers = [m for m in out if "돌파" in m[0]]
        rel = [m for m in out if "복귀" in m[0]]
        assert len(tiers) == (1 if want else 0) and len(rel) == (1 if wrel else 0), (v, got, st)
        if want:
            t = next(x for x in cfg["tiers"] if x["key"] == want)
            assert tiers[0][0] == tier_title(t, v) and tiers[0][2] == t["priority"], (v, tiers[0][0])
        for t_, m_, p_ in out:
            check_text(t_, m_)
            assert route_topic("cwally-port", t_) == "cwally-port" and "조회 실패" not in t_ and NA not in t_, t_
            assert "1주" in m_ and "1개월" in m_ and "오라클 등급:" in m_ and "단계:" in m_, m_
    print(f"tier seq ok: {len(seq)} 관측, 1.5/1.9/2.5 각 1번, 재무장 1.40 미만 / 1.70 미만 / 2.35 이하, 완화 1번, 여러 단계 동시 돌파는 1건")
    cfg = full_cfg
    # 5영업일 급등: (값, 급등 알림 기대). 기준 1.10 다섯 개 뒤부터.
    st = seed_state(cfg, "2026-10-29", 1.10)
    base = [(f"2026-10-{d:02d}", 1.10) for d in (23, 26, 27, 28, 29)]
    st = seed_state(cfg, base[-1][0], 1.10, base)
    assert st["jump"] == {"armed": True, "last_jump": None}, st
    jseq = [(1.20, False), (1.34, False), (1.35, True), (1.40, False), (1.45, False),   # +0.24 조용, +0.25 발동, 유지 중 조용
            (1.44, False), (1.42, False), (1.45, False),                                 # 5일 변화 +0.10 이상 유지: 무장 안 됨
            (1.46, False), (1.47, False), (1.50, "t150"),                                # 1.50 은 단계 메시지 (급등 아님, 무장도 아님)
            (1.48, False), (1.47, False), (1.49, False), (1.50, False), (1.45, False),  # 변화 +0.10 미만 -> 재무장 (1.45 vs 1.46 = -0.01)
            (1.60, False), (1.72, True)]                                                 # 1.72 vs 5관측 전 1.47 = +0.25 재발동
    series = list(base)
    day = date(2026, 11, 2)
    fired = 0
    for v, want in jseq:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        series.append((day.isoformat(), v))
        day += timedelta(days=1)
        out, _ = evaluate(cfg, st, list(series), stamp)
        jm = [m for m in out if "급등" in m[0]]
        tm = [m for m in out if "돌파" in m[0]]
        if want == "t150":
            assert len(tm) == 1 and not jm, (v, out)
        else:
            assert len(jm) == (1 if want else 0) and not tm, (v, [m[0] for m in out], st["jump"], jump_of(cfg, series))
        for t_, m_, p_ in jm:
            fired += 1
            check_text(t_, m_)
            assert p_ == 3 and route_topic("cwally-port", t_) == "cwally-port" and "5영업일" in m_, (t_, p_)
    assert fired == 2, fired
    print(f"jump seq ok: +0.25%p 이상 1번, +0.10 미만에서 재무장, 재발동 1번 (예: {jump_title(1.35, ('2026-10-23', 0.25))})")
    # 급등과 단계 돌파가 같은 관측: 단계 메시지 1건에 합침, 급등도 해제
    st = seed_state(cfg, base[-1][0], 1.10, base)
    o, _ = evaluate(cfg, st, base + [("2026-11-02", 1.52)], stamp)
    assert len(o) == 1 and "1.5%p 주의선 돌파" in o[0][0] and "급등도 발생" in o[0][1] and not st["jump"]["armed"], o
    print("tier + jump same observation: 1건(단계), 본문에 급등 한 줄")
    # 예전 state(jump 키 없음): 첫 새 관측에서 조용히 seed (급등이어도 안 보냄), 단계 판정은 정상
    st = seed_state(cfg, base[-1][0], 1.10, base)
    del st["jump"]
    o, _ = evaluate(cfg, st, base + [("2026-11-02", 1.45)], stamp)
    assert o == [] and st["jump"] == {"armed": False, "last_jump": 0.35}, (o, st)
    print("legacy state without jump: silent seed (armed False at +0.35)")
    # 샘플 출력
    st = seed_state(cfg, "2026-10-29", 1.10)
    base = [("2026-09-29", 1.40), ("2026-10-26", 1.70), ("2026-11-02", 1.93)]
    out, _ = evaluate(cfg, st, base, stamp)
    assert out[0][0] == "[CREDIT] LQD 000660 005930 | BBB 회사채 스프레드 1.93%p, 1.9%p 경고선 돌파" and out[0][2] == 4, out
    print(f"-- sample\nTITLE: {out[0][0]}\nPRIORITY: {out[0][2]}\n{out[0][1]}\n")
    for v in (1.55, 2.61):
        st = seed_state(cfg, "2026-10-29", 1.10)
        o, _ = evaluate(cfg, st, [("2026-10-29", 1.10), ("2026-11-02", v)], stamp)
        print(f"-- sample TITLE: {o[0][0]} (p{o[0][2]})")
    st = seed_state(cfg, "2026-10-29", 1.95)
    o, _ = evaluate(cfg, st, [("2026-10-29", 1.95), ("2026-11-02", 1.48)], stamp)
    assert len(o) == 1 and o[0][0] == relief_title(cfg, 1.48) and o[0][2] == 3, o
    print(f"-- sample TITLE: {o[0][0]} (p{o[0][2]})\n")
    # 조회 실패는 cwally-ops, 끝난 뒤 조용
    for tail in ("BBB 스프레드 조회 실패, 판정 못 함",):
        t, m, _ = ops_msg(stamp, tail, "x", oracle)
        check_text(t, m)
        assert route_topic("cwally-port", t) == "cwally-ops", t
    print("ops notice routes to cwally-ops")
    assert ended(cfg, datetime(2027, 2, 27, 0, 0, tzinfo=KST)) and not ended(cfg, datetime(2027, 2, 26, 9, 0, tzinfo=KST))
    print("ends 2027-02-26 23:59 KST: silent after")
    for t in cfg["tiers"]:
        assert "\u00b7" not in t["meaning"] and "\u2014" not in t["meaning"]
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if len(sys.argv) > 2:
        print(f"unexpected args {sys.argv[2:]}")
        return 2
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
            if ended(cfg, now):
                return 0
            st = load_state()
            if st.get("crash_day") == now.strftime("%Y-%m-%d"):
                return 0
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트 오류, 이번 실행 판정 못 함", f"{type(exc).__name__}: {str(exc)[:200]}", cfg["oracle_line"])
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
            st["crash_day"] = now.strftime("%Y-%m-%d")
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
