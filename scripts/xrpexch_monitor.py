#!/usr/bin/env python3
"""[XRP-EXCH] 주요 거래소 XRP 보유량 (공개 라벨 지갑 합계, 대리 지표, 조건 알림만).

스냅숏: 매주 일요일 00:00 UTC(한국 일요일 09:00) 직후 첫 원장의 잔고 합계. 실행이 늦어도 같은 원장이라 값이 같다.
지갑: configs/xrpexch.json addresses (XRPScan 공개 라벨 중 거래소 이름, 2026-10-22 잔고 1백만 XRP 이상, 고정 목록).
판정 (주 1회, 같은 주는 1번):
  - 전주 대비 +2% 이상 증가 -> 1회
  - 주간 순감소 4주 연속(이번 주 포함) -> 연속이 4주가 된 주에 1회. 증가 주가 나오면 다시 셈.
유료 거래소 보유량 지표(CryptoQuant/Glassnode)를 대신하는 대리값이라 본문에 항상 '대리 지표'로 밝힌다.
분기 지갑 목록 갱신 (2026-10-23 09:58 KST 승인):
  - 분기(KST 1/4/7/10월)마다 1번. 그 분기 첫 일요일 21:00 portlight 실행에서, 빠지면 분기 8일째 이후 평일 09:00 실행에서 따라잡기.
  - XRPScan 공개 라벨(well-known names)에서 config refresh.exchanges 이름이 붙은 지갑 중 그 주 스냅숏 원장 잔고
    1백만 XRP 이상인 지갑 = 새 목록. state wallets 에 버전(YYYY-MM-DD)과 함께 저장(봇의 state 커밋).
  - 안전장치: 라벨 조회 실패, 지갑 수 20% 넘게 변동, 같은 원장 기준 합계 20% 넘게 변동, 라벨 거래소 지갑 전체 대비
    커버리지 95% 미만이면 옛 목록 유지 + cwally-ops 1번 안내(분기당). 성공하면 바뀐 내용 요약을 cwally-ops 로 1번.
  - 연속성: 스냅숏마다 list_version 을 기록. 판정에 쓰는 최근 5주 스냅숏 중 현재 목록 버전과 다른 것은 같은 원장
    과거 잔고로 새 목록 기준 다시 계산(rebased_from 에 옛 값 보관). 그래서 전주 대비 %와 4주 연속 감소는 항상 같은 지갑 묶음 비교.
modes: auto | dry(발송 없음, state 안 씀. job=xrpwatch 수동 실행이나 'dry refresh'면 목록 갱신도 모의 실행) |
       seed N(지난 N주 스냅숏을 원장 과거값으로 채움, 발송 없음)
매매 신호가 아니다.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
RIPPLE_EPOCH = 946684800
NAME = "xrpexch"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "cwally-alerts/1.0 (+https://github.com/sosmos3218-bot/cwally-alerts)"
NA = "확인 불가"
MAX_FAIL = 0  # 계정 하나라도 못 읽으면 그 주 합계는 확인 불가


class Xrpl:
    def __init__(self, hosts: list[str]):
        self.hosts = hosts

    def rpc(self, method: str, params: dict) -> dict:
        last: Exception | None = None
        for attempt in range(3):
            for host in self.hosts:
                try:
                    req = urllib.request.Request(host, data=json.dumps({"method": method, "params": [params]}).encode(),
                                                 headers={"Content-Type": "application/json", "User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=40) as resp:
                        res = json.load(resp)["result"]
                    if res.get("error") in {"tooBusy", "noNetwork", "noCurrent", "noClosed", "lgrNotFound", "slowDown"}:
                        raise RuntimeError(f"{host} {res.get('error')}")
                    return res
                except Exception as exc:  # noqa: BLE001
                    last = exc
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"XRPL {method} 실패: {last}")

    def close_time(self, idx: int) -> int:
        return int(self.rpc("ledger", {"ledger_index": idx})["ledger"]["close_time"])

    def validated(self) -> tuple[int, int]:
        lg = self.rpc("ledger", {"ledger_index": "validated"})["ledger"]
        return int(lg["ledger_index"]), int(lg["close_time"])

    def first_ledger_at(self, t: int) -> int:
        vi, vt = self.validated()
        if t > vt:
            raise RuntimeError("스냅숏 시각이 아직 안 옴")
        est = vi - int((vt - t) / 3.9)
        lo, hi = max(est - 40000, 1), min(est + 40000, vi)
        while self.close_time(lo) >= t:
            lo = max(lo - 200000, 1)
        while self.close_time(hi) < t:
            hi = min(hi + 200000, vi)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.close_time(mid) < t:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def balance(self, acct: str, ledger: int) -> float:
        r = self.rpc("account_info", {"account": acct, "ledger_index": ledger})
        if r.get("error") == "actNotFound":
            return 0.0
        if r.get("error"):
            raise RuntimeError(f"{acct} {r.get('error')}")
        return int(r["account_data"]["Balance"]) / 1e6


def last_snapshot(now_utc: datetime) -> datetime:
    d = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def quarter_of(d) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_start(d):
    return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)


def active_list(cfg: dict, state: dict) -> dict:
    w = state.get("wallets")
    if isinstance(w, dict) and w.get("addresses"):
        return w
    return {"version": cfg.get("addresses_version", "2026-10-22"), "quarter": cfg.get("addresses_quarter", "2026-Q4"),
            "source": "configs/xrpexch.json addresses", "addresses": cfg["addresses"]}


def fetch_labels(url: str) -> list[dict]:
    last: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if not isinstance(data, list):
                raise ValueError("라벨 응답이 목록이 아님")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    raise RuntimeError(f"XRPScan 라벨 조회 실패: {last}")


def balances(x: Xrpl, accts: list[str], ledger: int) -> tuple[dict[str, float], list[str]]:
    def one(a: str):
        try:
            return a, x.balance(a, ledger), None
        except Exception as exc:  # noqa: BLE001
            return a, None, str(exc)[:80]

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, accts))
    return {a: b for a, b, e in res if e is None}, [f"{a[:8]}: {e}" for a, _, e in res if e]


def refresh_list(x: Xrpl, cfg: dict, old: dict, at: datetime, now: datetime) -> tuple[dict | None, dict]:
    """새 목록을 만들고 안전장치를 검사한다. (새 목록 또는 None, 요약/사유)."""
    rc = cfg["refresh"]
    labels = fetch_labels(rc["label_url"])
    names = set(rc["exchanges"])
    cand: dict[str, dict] = {}
    for it in labels:
        acct = it.get("account")
        if isinstance(it, dict) and it.get("name") in names and isinstance(acct, str) and acct.startswith("r"):
            cand.setdefault(acct, {"exchange": it["name"], "label": it.get("desc"), "account": acct})
    if len(labels) < int(rc["min_label_rows"]) or len(cand) < int(rc["min_exchange_labels"]):
        return None, {"reason": f"라벨 행 {len(labels)}개, 거래소 라벨 지갑 {len(cand)}개로 너무 적음(깨진 응답 의심)"}
    ledger = x.first_ledger_at(int(at.timestamp()) - RIPPLE_EPOCH)
    old_accts = [a["account"] for a in old["addresses"]]
    bal, fails = balances(x, sorted(set(cand) | set(old_accts)), ledger)
    if fails:
        return None, {"reason": f"원장 잔고 조회 실패 {len(fails)}개: {'; '.join(fails[:3])}"}
    min_xrp = float(rc["min_xrp"])
    new_addrs = sorted((cand[a] for a in cand if bal.get(a, 0.0) >= min_xrp), key=lambda r: (r["exchange"], -bal[r["account"]]))
    all_lab = sum(bal.get(a, 0.0) for a in cand)
    new_tot = sum(bal[r["account"]] for r in new_addrs)
    old_tot = sum(bal.get(a, 0.0) for a in old_accts)
    new_set, old_set = {r["account"] for r in new_addrs}, set(old_accts)
    added, removed = new_set - old_set, old_set - new_set
    info = {
        "ledger": ledger, "at_utc": at.strftime("%Y-%m-%d %H:%M"), "label_rows": len(labels), "exchange_labels": len(cand),
        "old_count": len(old_set), "new_count": len(new_set), "added": len(added), "removed": len(removed),
        "added_xrp": round(sum(bal.get(a, 0.0) for a in added)), "removed_xrp": round(sum(bal.get(a, 0.0) for a in removed)),
        "old_total": round(old_tot), "new_total": round(new_tot), "all_labelled_total": round(all_lab),
        "coverage_pct": round(new_tot / all_lab * 100, 2) if all_lab else None,
        "count_change_pct": round((len(new_set) / len(old_set) - 1) * 100, 2) if old_set else None,
        "total_change_pct": round((new_tot / old_tot - 1) * 100, 2) if old_tot else None,
        "added_by_exchange": dict(sorted({e: sum(1 for a in added if cand[a]["exchange"] == e) for e in {cand[a]["exchange"] for a in added}}.items())),
        "removed_list": sorted(removed)[:20],
    }
    bad = []
    if info["count_change_pct"] is None or abs(info["count_change_pct"]) > float(rc["max_count_change_pct"]):
        bad.append(f"지갑 수 변동 {info['count_change_pct']}%")
    if info["total_change_pct"] is None or abs(info["total_change_pct"]) > float(rc["max_total_change_pct"]):
        bad.append(f"같은 원장 합계 변동 {info['total_change_pct']}%")
    if info["coverage_pct"] is None or info["coverage_pct"] < float(rc["min_coverage_pct"]):
        bad.append(f"커버리지 {info['coverage_pct']}%")
    if bad:
        info["reason"] = ", ".join(bad) + " (기준 넘음)"
        return None, info
    new = {"version": now.strftime("%Y-%m-%d"), "quarter": quarter_of(now.date()), "source": rc["label_url"],
           "refreshed_at": now.isoformat(), "filter": f"exchanges in config refresh.exchanges, balance >= {min_xrp:,.0f} XRP at ledger {ledger}",
           "prev_version": old.get("version"), "addresses": new_addrs}
    return new, info


def snapshot(x: Xrpl, cfg: dict, at: datetime, wl: dict) -> dict:
    ledger = x.first_ledger_at(int(at.timestamp()) - RIPPLE_EPOCH)
    addrs = wl["addresses"]

    def one(a: dict):
        try:
            return a, x.balance(a["account"], ledger), None
        except Exception as exc:  # noqa: BLE001
            return a, None, str(exc)[:80]

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, addrs))
    fails = [f"{a['exchange']} {a['account'][:8]}: {e}" for a, b, e in res if e]
    by_ex: dict[str, float] = defaultdict(float)
    for a, b, _ in res:
        if b is not None:
            by_ex[a["exchange"]] += b
    return {"at_utc": at.strftime("%Y-%m-%d %H:%M"), "ledger": ledger, "total": round(sum(by_ex.values()), 2),
            "by_exchange": {k: round(v, 2) for k, v in sorted(by_ex.items())}, "accounts": len(addrs),
            "list_version": wl["version"], "fails": fails}


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str, tilt: str | None = None) -> str:
    return "\n".join([
        f"{stamp} | 거래소 XRP 보유량(대리 지표)",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        *([f"기울기(기본 확률 대비): {tilt}"] if tilt else []),
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def streak_down(snaps: dict, key: str) -> int:
    n = 0
    d = datetime.strptime(key, "%Y-%m-%d")
    while True:
        cur, prev = snaps.get(d.strftime("%Y-%m-%d")), snaps.get((d - timedelta(days=7)).strftime("%Y-%m-%d"))
        if not cur or not prev or cur["total"] >= prev["total"]:
            return n
        if cur.get("list_version", "2026-10-22") != prev.get("list_version", "2026-10-22"):
            return n  # 다른 지갑 목록끼리는 비교하지 않는다 (판정 4주는 항상 같은 버전으로 다시 계산됨)
        n += 1
        d -= timedelta(days=7)


def movers(cur: dict, prev: dict) -> str:
    ch = [(k, cur["by_exchange"].get(k, 0) - prev["by_exchange"].get(k, 0)) for k in set(cur["by_exchange"]) | set(prev["by_exchange"])]
    ch.sort(key=lambda kv: -abs(kv[1]))
    return ", ".join(f"{k} {v / 1e6:+,.1f}백만" for k, v in ch[:3])


def signal_msg(cfg: dict, snaps: dict, key: str, kind: str, streak: int, stamp: str) -> tuple[str, str, int, list[str]]:
    cur = snaps[key]
    pkey = (datetime.strptime(key, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    prev = snaps[pkey]
    pct = (cur["total"] / prev["total"] - 1) * 100
    chain = []
    d = datetime.strptime(key, "%Y-%m-%d")
    for _ in range(min(max(streak, 1), 4) + 1):
        s = snaps.get(d.strftime("%Y-%m-%d"))
        if s:
            chain.append(f"{d:%m/%d} {s['total'] / 1e9:,.3f}십억")
        d -= timedelta(days=7)
    status = (
        f"기준 {key} 00:00 UTC(원장 {cur['ledger']}) 라벨 거래소 지갑 {cur['accounts']}개 합계 {cur['total']:,.0f} XRP,"
        f" 전주 대비 {cur['total'] - prev['total']:+,.0f} XRP ({pct:+.2f}%)."
        f" {'4주 연속 순감소' if kind == 'down' else '전주 대비 2% 이상 증가'}. 주별 합계: {' <- '.join(chain)}."
        f" 많이 변한 거래소: {movers(cur, prev)}."
        f" 비교한 두 주 모두 같은 지갑 목록(버전 {cur.get('list_version', '2026-10-22')}) 기준."
        f" 대리 지표: XRPScan 공개 라벨 지갑(1백만 XRP 이상, 분기마다 목록 갱신) 원장 잔고 합계라 실제 거래소 보유량과 다를 수 있음"
        f" (새 지갑 이동, 수탁 지갑 섞임). 출처: XRPL 원장(xrplcluster 등 공개 JSON-RPC)."
    )
    if kind == "down":
        tilt = ("상방 가능성 다소 높음 (근거: 거래소 지갑에서 4주 연속 물량이 빠져 매도 대기 물량이 줄어드는 흐름으로 흔히 해석."
                " 대리 지표이고 확률 수치는 계산하지 않음)")
        title = "[XRP-EXCH] 거래소 보유량 4주 연속 감소 | 공급 감소 쪽, 추격 말고 확인"
        action = "보유 중이면 유지. 새로 사려면 ETF 흐름, 에스크로 알림과 가격 위치를 같이 보고 분할로만."
    else:
        tilt = ("하방 가능성 다소 높음 (근거: 거래소 지갑으로 한 주에 2% 이상 들어와 매도 대기 물량이 늘었을 수 있음."
                " 대리 지표이고 확률 수치는 계산하지 않음)")
        title = f"[XRP-EXCH] 거래소 보유량 주간 {pct:+.1f}% | 공급 증가 쪽, 추가매수 보류 검토"
        action = "추가매수는 잠시 보류 검토. 어느 거래소로 들어왔는지(위 변화 큰 거래소)와 가격 반응을 확인."
    msg = body(stamp, "주요 거래소 XRP 보유량(공개 라벨 지갑 합계)이 4주 연속 줄거나 한 주에 2% 이상 늘 때만 오는 알림입니다.", status,
               action, "매주 일요일 21:00에 다시 판정합니다. 조건이 아닌 주는 알림이 없습니다.",
               "이 알림만으로 정하지 않기. 미리 정한 비중과 손절 기준대로.",
               "대리 지표를 실제 거래소 전체 보유량으로 단정, 한 거래소 지갑 이동을 시장 전체 매도로 읽기.", tilt)
    return title, msg, 4, ["bank"]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    snaps = state.setdefault("snapshots", {})
    judged = state.setdefault("judged", {})
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    x = Xrpl(cfg["sources"]["xrpl"])
    at = last_snapshot(now.astimezone(UTC))
    key = at.strftime("%Y-%m-%d")
    out: list[tuple[str, str, int, list[str]]] = []
    ops: list[tuple[str, str, int, list[str]]] = []
    wl = active_list(cfg, state)
    q = quarter_of(now.date())
    pick = os.environ.get("PICK_JOB", "")
    sim = mode == "dry" and (pick == "xrpwatch" or "refresh" in sys.argv[2:])
    due = wl.get("quarter") != q and (pick == "portlight" or (now.date() - quarter_start(now.date())).days >= 7)
    print(f"wallet list version {wl.get('version')} ({wl.get('quarter')}, {len(wl['addresses'])} wallets); quarter {q};"
          f" refresh due={due} simulate={sim}")
    if mode in {"auto", "dry"} and (due or sim):
        try:
            new, info = refresh_list(x, cfg, wl, at, now)
            print("refresh:", json.dumps({k: v for k, v in info.items() if k != "removed_list"}, ensure_ascii=False))
            if new:
                ops.append(("[XRP-EXCH] 거래소 지갑 목록 분기 갱신 | 운영 안내", body(
                    stamp, "거래소 XRP 보유량 알림이 쓰는 라벨 지갑 목록을 분기마다 새로 만든 결과입니다(매매 신호 아님).",
                    f"목록 {wl.get('version')} -> {new['version']}: 지갑 {info['old_count']}개 -> {info['new_count']}개"
                    f" (추가 {info['added']}개 {info['added_xrp']:,} XRP, 제외 {info['removed']}개 {info['removed_xrp']:,} XRP)."
                    f" 같은 원장({info['ledger']}, {info['at_utc']} UTC) 기준 합계 {info['old_total']:,} -> {info['new_total']:,} XRP"
                    f" ({info['total_change_pct']:+.2f}%), 라벨 거래소 지갑 전체 대비 커버리지 {info['coverage_pct']}%."
                    f" 출처: XRPScan 공개 라벨 + XRPL 원장.",
                    "할 일 없음. 다음 주간 판정부터 새 목록을 씁니다.",
                    "비교용 최근 5주 스냅숏은 새 목록으로 과거 잔고를 다시 계산해서 연속성이 유지됩니다.",
                    "해당 없음.", "목록 갱신으로 생긴 합계 차이를 거래소 유입이나 유출로 읽기."), 2, ["card_index"]))
                if mode == "auto":
                    state["wallets"] = new
                    state.setdefault("refresh_log", []).append({k: v for k, v in info.items() if k != "removed_list"} | {"version": new["version"], "ok": True})
                wl = new
            else:
                raise RuntimeError(info.get("reason") or "알 수 없는 사유")
        except Exception as exc:  # noqa: BLE001
            print(f"wallet list refresh rejected: {exc}", file=sys.stderr)
            fn = state.setdefault("fail_notice", {})
            if fn.get("refresh") != q or mode == "dry":
                ops.append(("[XRP-EXCH] 거래소 지갑 목록 갱신 확인 불가 | 옛 목록 유지", body(
                    stamp, "거래소 XRP 보유량 알림의 분기 지갑 목록 갱신이 안전장치에 걸린 안내입니다(매매 신호 아님).",
                    f"새 목록 {NA} ({str(exc)[:200]}). 옛 목록 {wl.get('version')} ({len(wl['addresses'])}개) 그대로 씁니다.",
                    "필요하면 XRPScan 라벨 목록을 직접 확인. 같은 분기 안에서는 다음 실행 때 조용히 다시 시도합니다.",
                    "다음 분기에 다시 갱신합니다.", "해당 없음.", "이 안내를 거래소 보유량 변화로 읽기."), 3, ["warning"]))
                fn["refresh"] = q
    if mode == "auto" and key in judged:
        print("week already judged; nothing to do")
        for title, msg, pri, tags in ops:
            print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: cwally-ops\n{msg}\n")
            publish(resolve_topic("ops"), title, msg, pri, tags, server)
        if ops:
            state["updated_at"] = now.isoformat()
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    print(f"target snapshot {key} 00:00 UTC (judged: {key in judged})")
    need = int(sys.argv[2]) if mode == "seed" and len(sys.argv) > 2 else int(cfg["decrease_streak"]) + 1
    try:
        for k in range(need - 1, -1, -1):
            d = at - timedelta(days=7 * k)
            dk = d.strftime("%Y-%m-%d")
            old = snaps.get(dk)
            if old and old.get("list_version", "2026-10-22") == wl["version"] and mode != "dry":
                continue
            s = snapshot(x, cfg, d, wl)
            if old and old.get("list_version", "2026-10-22") != wl["version"]:
                s["rebased_from"] = {"list_version": old.get("list_version", "2026-10-22"), "total": old.get("total"),
                                     "accounts": old.get("accounts")}
            print(f"snapshot {dk} (list {wl['version']}): total {s['total']:,.0f} ledger {s['ledger']} fails {len(s['fails'])}"
                  + (f" rebased from {s['rebased_from']['list_version']} total {s['rebased_from']['total']:,.0f}" if s.get("rebased_from") else ""))
            if len(s["fails"]) > MAX_FAIL:
                raise RuntimeError(f"{dk} 잔고 조회 실패 {len(s['fails'])}개: {'; '.join(s['fails'][:3])}")
            snaps[dk] = s
        prev_key = (at - timedelta(days=7)).strftime("%Y-%m-%d")
        cur, prev = snaps[key], snaps[prev_key]
        pct = (cur["total"] / prev["total"] - 1) * 100
        streak = streak_down(snaps, key)
        up = pct >= float(cfg["wow_increase_pct"])
        down = streak == int(cfg["decrease_streak"])
        print(f"{key}: total {cur['total']:,.0f} vs {prev_key} {prev['total']:,.0f} -> {pct:+.3f}%, down streak {streak}"
              f" -> increase>=2% {up}, 4-week decrease {down}")
        if mode == "seed":
            for dk in sorted(snaps):
                judged[dk] = {"note": "seed 2026-10-22 (발송 없음)"}
            print(f"seeded; current conditions: increase={up} decrease4={down} (no send)")
        else:
            if up:
                out.append(signal_msg(cfg, snaps, key, "up", streak, stamp))
            if down:
                out.append(signal_msg(cfg, snaps, key, "down", streak, stamp))
            judged[key] = {"pct": round(pct, 3), "streak": streak, "sent_up": up, "sent_down": down, "at": now.isoformat()}
            if not out:
                print("condition not met (no alert)")
    except Exception as exc:  # noqa: BLE001
        print(f"exchange snapshot failed: {exc}", file=sys.stderr)
        fn = state.setdefault("fail_notice", {})
        if fn.get("snapshot") != key and mode != "seed":
            out.append(("[XRP-EXCH] 거래소 지갑 잔고 조회 실패 | 판정 못 함", body(
                stamp, "거래소 XRP 보유량 알림의 데이터 조회 실패 안내.", f"XRPL 원장 잔고 {NA} ({str(exc)[:140]}).",
                "필요하면 XRPScan 에서 거래소 지갑을 직접 확인.", "다음 실행(평일 09:00 또는 일요일 21:00)에서 다시 봅니다.",
                "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 보유량 변화 없음으로 읽기."), 3, ["warning"]))
            fn["snapshot"] = key
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if mode == "auto":
            publish(topic, title, msg, pri, tags, server)
    for title, msg, pri, tags in ops:  # 목록 갱신 안내는 항상 cwally-ops
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: cwally-ops\n{msg}\n")
        if mode == "auto":
            publish(resolve_topic("ops"), title, msg, pri, tags, server)
    if mode == "dry":
        print("DRY RUN: no publish, state not written")
        return 0
    keep = sorted(snaps)[-12:]
    state["snapshots"] = {k: snaps[k] for k in keep}
    state["judged"] = {k: v for k, v in judged.items() if k >= keep[0]} if keep else judged
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
