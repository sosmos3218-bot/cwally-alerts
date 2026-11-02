#!/usr/bin/env python3
"""[NIX-EPS] SK하이닉스(000660) 2027E EPS 컨센서스가 최근 28일 최고치보다 5% 이상 낮아지면 p4 1번. 기존 평일 19:00 k203780 슬롯.
출처: WiseReport(FnGuide) c1050001 컨센서스 표(2027.12(E) EPS, 2026.12(E) EPS), c1010001 추정기관수, 기준일(sDT).
state/nixeps.json history: 기준일별 스냅샷(date, eps27, eps26, n_brokers, base). 기준일이 바뀐 날만 추가하고 판정.
에피소드: 하향 -5% 이상 첫 판정에 p4. 같은 에피소드에서 발송 때 값보다 5% 더 내리면 p3 1번(최대 1번).
재무장: EPS 가 에피소드 기준 최고치의 2% 안으로 회복하거나, 28일 창에서 에피소드 기준보다 높은 새 최고치가 나오면.
첫 실행: cF5001 월말 EPS 추이(최근 3개월 점)로 백필 뒤 오늘 값 추가. 평일엔 메시지 없음.
연도 표시(2027.12(E))가 없거나 값이 이상하면 판정하지 않고 cwally-ops 하루 1번. ends_kst 뒤 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀; --as-of "YYYY-MM-DD HH:MM") | install | selftest
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[NIX-EPS]"
NAME = "nixeps"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts)"}


def get(url: str, referer: str | None = None, timeout: int = 30) -> str:
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------- fetch ----------

def base_date(cfg: dict) -> str:
    html = get(cfg["sources"]["consensus_page"].format(code=cfg["code"]))
    m = re.search(r"sDT:\s*'(\d{8})'", html)
    if not m:
        raise ValueError("컨센서스 기준일(sDT) 없음")
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def parse_table(raw: str, cfg: dict) -> dict:
    rows = json.loads(raw)["JsonData"]
    ty, cy = cfg["target_year"], cfg["context_year"]
    lab = {r.get("YYMM", ""): r for r in rows}
    r27 = lab.get(f"{ty}.12(E)")
    r26 = lab.get(f"{cy}.12(E)") or lab.get(f"{cy}.12(A)")
    if r27 is None:
        raise ValueError(f"{ty}.12(E) 열 없음 (있는 열: {', '.join(lab)[:120]})")
    e27, e26 = num(r27.get("EPS")), num(r26.get("EPS")) if r26 else None
    lo, hi = cfg["plausible_eps"]["min"], cfg["plausible_eps"]["max"]
    if e27 is None or not (lo <= e27 <= hi):
        raise ValueError(f"{ty}E EPS 값 이상: {r27.get('EPS')!r}")
    return {"eps27": e27, "eps26": e26, "eps26_label": (r26 or {}).get("YYMM")}


def brokers(cfg: dict) -> int | None:
    t = get(cfg["sources"]["overview_page"].format(code=cfg["code"]))
    t = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S))))
    m = re.search(r"추정기관수\s+[\d.]+\s+[\d,]+\s+[\d,]+\s+[\d.]+\s+(\d+)", t)
    return int(m.group(1)) if m else None


def snapshot(cfg: dict) -> dict:
    b = base_date(cfg)
    page = cfg["sources"]["consensus_page"].format(code=cfg["code"])
    tab = parse_table(get(cfg["sources"]["consensus_table"].format(code=cfg["code"], dt=b.replace("-", "")), page), cfg)
    try:
        n = brokers(cfg)
    except Exception:  # noqa: BLE001
        n = None
    return {"date": b, **tab, "n_brokers": n, "src": "WiseReport 컨센서스 표"}


def backfill(cfg: dict, base: str) -> list[dict]:
    page = cfg["sources"]["consensus_page"].format(code=cfg["code"])
    raw = get(cfg["sources"]["consensus_trend"].format(code=cfg["code"], dt=base.replace("-", ""), yymm=f"{cfg['target_year']}12"), page)
    c = json.loads(json.loads(raw)["chart1"])
    if c.get("select_item_name") != "EPS":
        raise ValueError("추이 차트가 EPS 아님")
    out = []
    cut = (date.fromisoformat(base) - timedelta(days=95)).isoformat()
    for d, v in zip(c["categories"], c["select_item"]):
        d = d.replace("/", "-")
        if v and d >= cut and d < base:
            out.append({"date": d, "eps27": round(float(v)), "eps26": None, "n_brokers": None, "src": "WiseReport 월말 EPS 추이 백필"})
    return out


def yahoo_close(cfg: dict) -> tuple[str, float] | None:
    try:
        d = json.loads(get(cfg["sources"]["yahoo_chart"].format(sym=cfg["yahoo_symbol"]), timeout=20))["chart"]["result"][0]
        pts = [(datetime.fromtimestamp(t, KST).date().isoformat(), c) for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]) if c]
        return pts[-1] if pts else None
    except Exception:  # noqa: BLE001
        return None


# ---------- evaluate ----------

def window_max(hist: list[dict], base: str, days: int) -> dict:
    lo = (date.fromisoformat(base) - timedelta(days=days)).isoformat()
    w = [h for h in hist if lo <= h["date"] <= base]
    return max(w, key=lambda h: (h["eps27"], h["date"]))


def evaluate(cfg: dict, st: dict) -> list[dict]:
    """최신 스냅샷으로 판정. 발송할 이벤트 목록. st['episode'] 를 고침."""
    hist = st["history"]
    cur = hist[-1]
    mx = window_max(hist, cur["date"], cfg["window_days"])
    chg = (cur["eps27"] / mx["eps27"] - 1) * 100
    ep = st.get("episode") or {"active": False}
    out = []
    if ep.get("active"):
        if cur["eps27"] >= ep["peak"] * (1 - cfg["rearm_within_pct"] / 100):
            print(f"재무장: {cur['eps27']:,.0f} 가 기준 {ep['peak']:,.0f} 의 {cfg['rearm_within_pct']}% 안으로 회복")
            ep = {"active": False}
        elif mx["eps27"] > ep["peak"] and mx["date"] > ep["fired_on"]:
            print(f"재무장: 새 28일 최고 {mx['eps27']:,.0f} ({mx['date']}) 가 에피소드 기준보다 높음")
            ep = {"active": False}
    if not ep.get("active"):
        if chg <= -cfg["cut_pct"]:
            ep = {"active": True, "peak": mx["eps27"], "peak_date": mx["date"], "fired_on": cur["date"], "fire_eps": cur["eps27"], "step_sent": False}
            out.append({"kind": "cut", "cur": cur, "mx": mx, "chg": chg})
    elif not ep.get("step_sent") and cur["eps27"] <= ep["fire_eps"] * (1 - cfg["step_pct"] / 100):
        ep["step_sent"] = True
        out.append({"kind": "step", "cur": cur, "mx": {"eps27": ep["peak"], "date": ep["peak_date"]},
                    "chg": (cur["eps27"] / ep["peak"] - 1) * 100, "fire_eps": ep["fire_eps"]})
    st["episode"] = ep
    print(f"판정: {cur['date']} 27E {cur['eps27']:,.0f}, 28일 최고 {mx['eps27']:,.0f} ({mx['date']}), {chg:+.1f}%, episode={ep}")
    return out


# ---------- messages ----------

def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 000660 | SK하이닉스 {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "SK하이닉스 2027E EPS 컨센서스(WiseReport, 최근 3개월 증권사 평균)가 최근 28일 최고치보다 5% 이상 낮아졌을 때 보내는 알림. 매매 신호 아님."


def message(cfg: dict, e: dict, px: tuple[str, float] | None, stamp: str) -> tuple[str, str, int]:
    cur, mx = e["cur"], e["mx"]
    per = f"{px[1] / cur['eps27']:.2f}배 ({px[0]} KRX 종가 {px[1]:,.0f}원, Yahoo)" if px else "확인 불가(Yahoo KRX 종가 조회 실패)"
    e26 = f"{cur['eps26']:,.0f}원" if cur.get("eps26") else "확인 불가"
    nb = f"{cur['n_brokers']}곳" if cur.get("n_brokers") else "확인 불가"
    st = (f"컨센서스 기준일 {cur['date']}. 2027E EPS {cur['eps27']:,.0f}원, 28일 최고 {mx['eps27']:,.0f}원({mx['date']}) 대비 {e['chg']:+.1f}%. "
          f"2026E EPS {e26}, 추정기관 {nb}. 2027E PER {per}.")
    if e["kind"] == "step":
        st += f" 첫 알림 때 {e['fire_eps']:,.0f}원에서 {(cur['eps27'] / e['fire_eps'] - 1) * 100:+.1f}% 추가 하향."
        title = f"{TAG} 000660 SK하이닉스 2027E EPS 추가 하향, 4주 고점 대비 {e['chg']:+.1f}%"
    else:
        title = f"{TAG} 000660 SK하이닉스 2027E EPS 4주 {e['chg']:+.1f}% 하향"
    return (title, body(stamp, "2027E EPS 하향", WHAT, st,
                        "의미: 이익 정점 신호 가능, 신규 진입 보류 검토. 하향 이유(D램 가격, HBM 물량, 환율)를 리포트로 확인.",
                        f"EPS 가 기준 최고치 {mx['eps27']:,.0f}원의 2% 안으로 회복하거나 새 28일 최고가 나오면 다시 무장. 같은 에피소드 추가 하향 알림은 최대 1번. 2027-03-31 19:00 까지.",
                        "해당 없음(미보유). 보유하게 되면 비중 계획부터 점검.",
                        "하향 한 번만 보고 바닥이나 고점을 단정하기, 낮은 PER 만 보고 바로 사기."),
            cfg["priority"]["step" if e["kind"] == "step" else "cut"])


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 000660 SK하이닉스 EPS 컨센서스 조회 실패 | 판정 보류",
            body(stamp, "조회 실패", "SK하이닉스 2027E EPS 컨센서스 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "WiseReport 컨센서스 화면을 직접 보세요.", "다음 19:00 슬롯에서 다시 읽습니다. state 는 그대로.",
                 "해당 없음.", "추정치를 짐작해서 판정하기."))


def install_message(stamp: str, snap: dict | None) -> tuple[str, str]:
    now = (f"지금 값: 기준일 {snap['date']} 2027E EPS {snap['eps27']:,.0f}원, 2026E {snap['eps26']:,.0f}원, 추정기관 {snap['n_brokers']}곳. "
           if snap and snap.get("eps26") and snap.get("n_brokers") else "")
    return (f"{TAG} 000660 SK하이닉스 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "SK하이닉스 2027E EPS 컨센서스 하향 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 now + "평일 19:00 KST 에 WiseReport 컨센서스를 읽어 최근 28일 최고치 대비 -5% 이상이면 p4 1번. 평소에는 메시지 없음.",
                 "알림이 도착했는지 확인만 하세요.",
                 "같은 에피소드 추가 -5% 는 p3 1번까지. 2% 안 회복이나 새 고점 뒤 다시 무장. 2027-03-31 19:00 에 끝납니다.",
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


def run(mode: str, now: datetime, cfg: dict, pub=publish, snap_fn=None, back_fn=None, px_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        try:
            snap = (snap_fn or snapshot)(cfg)
        except Exception:  # noqa: BLE001
            snap = None
        t, m = install_message(stamp, snap)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    today = now.date().isoformat()
    if now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"]:
        print(f"감시 끝({cfg['ends_kst']}): 조용히 종료")
        return 0
    st = load_state()
    st.setdefault("history", [])
    out, ops = [], None
    try:
        snap = (snap_fn or snapshot)(cfg)
        print(f"snapshot: {snap}")
        if not st["history"]:
            try:
                bf = (back_fn or backfill)(cfg, snap["date"])
            except Exception as exc:  # noqa: BLE001
                bf = []
                print(f"백필 실패({type(exc).__name__}): 오늘 값부터 기록")
            st["history"] = bf
            st["seeded_at"] = stamp
            print(f"백필 {len(bf)}점: {[(h['date'], h['eps27']) for h in bf]}")
        last = st["history"][-1]["date"] if st["history"] else ""
        if snap["date"] > last:
            st["history"].append({k: snap[k] for k in ("date", "eps27", "eps26", "n_brokers", "src")} | {"fetched": stamp})
            evs = evaluate(cfg, st)
            px = (px_fn or yahoo_close)(cfg) if evs else None
            out = [message(cfg, e, px, stamp) for e in evs]
        elif snap["date"] == last and snap["eps27"] != st["history"][-1]["eps27"]:
            st["history"][-1].update({k: snap[k] for k in ("eps27", "eps26", "n_brokers")} | {"fetched": stamp})
            evs = evaluate(cfg, st)
            px = (px_fn or yahoo_close)(cfg) if evs else None
            out = [message(cfg, e, px, stamp) for e in evs]
        else:
            print(f"기준일 {snap['date']} 그대로, 판정 생략")
            if (date.fromisoformat(today) - date.fromisoformat(snap["date"])).days > cfg["stale_days"] and st.get("stale_day") != snap["date"]:
                ops = ops_msg(stamp, f"컨센서스 기준일이 {snap['date']} 에서 {cfg['stale_days']}일 넘게 안 바뀜. 판정은 그대로 둠.")
                st["stale_day"] = snap["date"]
        cut = (date.fromisoformat(today) - timedelta(days=120)).isoformat()
        st["history"] = [h for h in st["history"] if h["date"] >= cut]
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"WiseReport 컨센서스 읽기 또는 검증 실패({err}). 판정 건너뜀, state 는 그대로.")
            st["ops_day"] = today
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> ops\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["chart_with_downwards_trend"], server, config_name=NAME)
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
    raw = json.dumps({"JsonData": [{"YYMM": "2026.12(E)", "EPS": "355,563"}, {"YYMM": "2027.12(E)", "EPS": "529,311"}]})
    assert parse_table(raw, cfg)["eps27"] == 529311
    for bad in ({"JsonData": [{"YYMM": "2026.12(E)", "EPS": "1"}]}, {"JsonData": [{"YYMM": "2027.12(E)", "EPS": "-"}]},
                {"JsonData": [{"YYMM": "2027.12(E)", "EPS": "0"}]}):
        try:
            parse_table(json.dumps(bad), cfg)
            raise AssertionError(bad)
        except ValueError:
            pass
    sent = []
    pub = lambda *a, **k: sent.append(a)  # noqa: E731
    n = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
    seq = {"v": None}
    snap = lambda c: dict(seq["v"])  # noqa: E731
    back = lambda c, b: [{"date": "2026-09-30", "eps27": 463713, "eps26": None, "n_brokers": None, "src": "bf"}]  # noqa: E731
    px = lambda c: ("2026-11-02", 1472000.0)  # noqa: E731
    mk = lambda d, v: {"date": d, "eps27": v, "eps26": 355563, "n_brokers": 20, "src": "t"}  # noqa: E731
    with tempfile.TemporaryDirectory() as td:
        STATE_PATH = Path(td) / "s.json"
        seq["v"] = mk("2026-10-30", 529311)
        run("auto", n("2026-11-02 19:00"), cfg, pub, snap, back, px)
        s = load_state()
        assert [h["date"] for h in s["history"]] == ["2026-09-30", "2026-10-30"] and not sent, s
        run("auto", n("2026-11-03 19:00"), cfg, pub, snap, back, px)
        assert not sent
        seq["v"] = mk("2026-11-05", 510000)   # -3.6%: 없음
        run("auto", n("2026-11-05 19:00"), cfg, pub, snap, back, px)
        assert not sent
        seq["v"] = mk("2026-11-10", 500000)   # -5.5%: p4
        run("auto", n("2026-11-10 19:00"), cfg, pub, snap, back, px)
        assert len(sent) == 1 and sent[0][1] == "[NIX-EPS] 000660 SK하이닉스 2027E EPS 4주 -5.5% 하향" and sent[0][3] == 4, sent
        assert "2027E PER 2.94배" in sent[0][2] and "이익 정점 신호 가능, 신규 진입 보류 검토" in sent[0][2], sent[0][2]
        sent.clear()
        seq["v"] = mk("2026-11-12", 490000)   # 같은 에피소드, 추가 -2%: 없음
        run("auto", n("2026-11-12 19:00"), cfg, pub, snap, back, px)
        assert not sent
        seq["v"] = mk("2026-11-13", 474000)   # 발송값 대비 -5.2%: p3
        run("auto", n("2026-11-13 19:00"), cfg, pub, snap, back, px)
        assert len(sent) == 1 and sent[0][3] == 3 and "추가 하향" in sent[0][1], sent
        sent.clear()
        seq["v"] = mk("2026-11-16", 440000)   # 더 내려도 추가 없음
        run("auto", n("2026-11-16 19:00"), cfg, pub, snap, back, px)
        assert not sent
        seq["v"] = mk("2026-11-20", 520000)   # 기준 529,311 의 2% 안(518,725 이상): 재무장
        run("auto", n("2026-11-20 19:00"), cfg, pub, snap, back, px)
        assert not sent and not load_state()["episode"]["active"]
        seq["v"] = mk("2026-11-24", 490000)   # 28일 최고 529,311(10/30) 대비 -7.4%: 새 p4
        run("auto", n("2026-11-24 19:00"), cfg, pub, snap, back, px)
        assert len(sent) == 1 and sent[0][3] == 4, sent
        sent.clear()
        def boom(c):
            raise ValueError("2027.12(E) 열 없음")
        run("auto", n("2026-11-25 19:00"), cfg, pub, boom, back, px)
        assert len(sent) == 1 and "조회 실패" in sent[0][1], sent
    for tt, mm in (ops_msg("T", "x"), install_message("T", mk("2026-10-30", 529311))):
        check_text(tt, mm)
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
