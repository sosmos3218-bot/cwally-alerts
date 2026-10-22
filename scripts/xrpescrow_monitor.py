#!/usr/bin/env python3
"""[XRP-ESCROW] 리플 에스크로 월간 재잠금률 (조건 알림만).

재잠금률 = 그 달(UTC) 새 에스크로로 다시 잠근 XRP / 그 달 에스크로에서 풀린 XRP.
  >= 80%  -> 상방 쪽 신호로 1회 (월중 누적으로 먼저 넘으면 그때 1회, 재잠금은 달 안에서 늘기만 하므로 뒤집히지 않음)
  <  70%  -> 하방 쪽 신호로 1회 (달이 끝나야 확정이라 다음 달 첫 평일 09:00 확정 판정에서만)
  그 사이 -> 알림 없음
온체인(XRPL 공개 JSON-RPC)만 사용:
  해제   = configs ripple_accounts(+state learned_accounts) 계정이 소유한 Escrow 가 EscrowFinish(tesSUCCESS)로 지워진 양
  재잠금 = 같은 계정, 또는 그 달 그 계정들에서 trace_min_xrp 이상 받은 계정의 자기 앞 EscrowCreate(tesSUCCESS) 양
           (받은 계정이 에스크로를 만들면 learned_accounts 에 추가)
modes: auto | dry(발송 없음, state 안 씀) | seed(지난 달들을 계산해 기록만, 발송 없음)
매매 신호가 아니다.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
RIPPLE_EPOCH = 946684800
NAME = "xrpescrow"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "cwally-alerts/1.0 (+https://github.com/sosmos3218-bot/cwally-alerts)"
NA = "확인 불가"
DROPS = 1_000_000


class Xrpl:
    def __init__(self, hosts: list[str]):
        self.hosts = hosts
        self.calls = 0

    def rpc(self, method: str, params: dict) -> dict:
        last: Exception | None = None
        for attempt in range(2):
            for host in self.hosts:
                try:
                    self.calls += 1
                    req = urllib.request.Request(host, data=json.dumps({"method": method, "params": [params]}).encode(),
                                                 headers={"Content-Type": "application/json", "User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        res = json.load(resp)["result"]
                    if res.get("error") in {"tooBusy", "noNetwork", "noCurrent", "noClosed", "lgrNotFound", "slowDown"}:
                        raise RuntimeError(f"{host} {res.get('error')}")
                    return res
                except Exception as exc:  # noqa: BLE001
                    last = exc
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"XRPL {method} 실패: {last}")

    def close_time(self, idx: int) -> int:
        return int(self.rpc("ledger", {"ledger_index": idx})["ledger"]["close_time"])

    def validated(self) -> tuple[int, int]:
        lg = self.rpc("ledger", {"ledger_index": "validated"})["ledger"]
        return int(lg["ledger_index"]), int(lg["close_time"])

    def first_ledger_at(self, t: int) -> int:
        """close_time >= t 인 첫 원장 (t 는 Ripple epoch 초)."""
        vi, vt = self.validated()
        if t > vt:
            return vi + 1
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

    def account_tx(self, acct: str, lmin: int, lmax: int) -> list[dict]:
        out: list[dict] = []
        marker = None
        while True:
            p = {"account": acct, "ledger_index_min": lmin, "ledger_index_max": lmax, "limit": 200, "forward": True}
            if marker:
                p["marker"] = marker
            r = self.rpc("account_tx", p)
            if r.get("error"):
                raise RuntimeError(f"account_tx {acct}: {r.get('error')}")
            out += r.get("transactions") or []
            marker = r.get("marker")
            if not marker:
                return out


def tx_of(t: dict) -> tuple[dict, dict, str]:
    tx = t.get("tx_json") or t.get("tx") or {}
    return tx, t.get("meta") or {}, t.get("hash") or tx.get("hash") or ""


def month_bounds(ym: str) -> tuple[datetime, datetime]:
    y, m = map(int, ym.split("-"))
    s = datetime(y, m, 1, tzinfo=UTC)
    e = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return s, e


def compute(x: Xrpl, accounts: set[str], start: datetime, end: datetime, trace_min: int) -> dict:
    a = x.first_ledger_at(int(start.timestamp()) - RIPPLE_EPOCH)
    b = x.first_ledger_at(int(end.timestamp()) - RIPPLE_EPOCH) - 1
    vi, _ = x.validated()
    b = min(b, vi)
    released = relocked = 0
    rel_rows: list[str] = []
    lock_rows: list[str] = []
    seen: set[str] = set()
    recipients: set[str] = set()
    learned: set[str] = set()

    def scan(acct: str, trace: bool) -> None:
        nonlocal released, relocked
        for t in x.account_tx(acct, a, b):
            tx, meta, h = tx_of(t)
            if meta.get("TransactionResult") != "tesSUCCESS" or h in seen:
                continue
            tt = tx.get("TransactionType")
            if tt == "EscrowFinish":
                for n in meta.get("AffectedNodes") or []:
                    d = n.get("DeletedNode")
                    if d and d.get("LedgerEntryType") == "Escrow" and d["FinalFields"].get("Account") in accounts:
                        amt = int(d["FinalFields"]["Amount"])
                        released += amt
                        seen.add(h)
                        rel_rows.append(f"{d['FinalFields']['Account'][:6]} {amt / DROPS / 1e6:,.0f}백만")
            elif tt == "EscrowCreate" and tx.get("Account") == acct and tx.get("Destination") == acct and isinstance(tx.get("Amount"), str):
                amt = int(tx["Amount"])
                relocked += amt
                seen.add(h)
                fa = tx.get("FinishAfter")
                until = datetime.fromtimestamp(fa + RIPPLE_EPOCH, UTC).strftime("%Y-%m") if fa else "?"
                lock_rows.append(f"{acct[:6]} {amt / DROPS / 1e6:,.0f}백만(~{until})")
                if acct not in accounts:
                    learned.add(acct)
            elif trace and tt == "Payment" and tx.get("Account") == acct:
                da = meta.get("delivered_amount")
                if isinstance(da, str) and int(da) >= trace_min * DROPS:
                    recipients.add(tx.get("Destination"))

    for acct in sorted(accounts):
        scan(acct, True)
    for acct in sorted(recipients - accounts):
        scan(acct, False)
    rate = (relocked / released * 100) if released else None
    return {"start": start.strftime("%Y-%m-%d %H:%M UTC"), "end": end.strftime("%Y-%m-%d %H:%M UTC"),
            "ledgers": [a, b], "released_xrp": released // DROPS, "relocked_xrp": relocked // DROPS,
            "rate_pct": round(rate, 2) if rate is not None else None, "released_rows": rel_rows,
            "relocked_rows": lock_rows, "learned": sorted(learned)}


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str, tilt: str | None = None) -> str:
    return "\n".join([
        f"{stamp} | 리플 에스크로 재잠금률",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        *([f"기울기(기본 확률 대비): {tilt}"] if tilt else []),
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def history_line(months: dict, exclude: str) -> str:
    vals = [(k, v["rate_pct"]) for k, v in sorted(months.items()) if k != exclude and v.get("final") and v.get("rate_pct") is not None]
    vals = vals[-12:]
    if not vals:
        return "과거 월 기록 없음"
    rates = [r for _, r in vals]
    return (f"직전 {len(vals)}개월({vals[0][0]}~{vals[-1][0]}, 같은 방식 온체인 계산) 재잠금률 최저 {min(rates):.0f}%"
            f" 최고 {max(rates):.0f}%, 80% 이상 {sum(r >= 80 for r in rates)}번, 70% 미만 {sum(r < 70 for r in rates)}번")


def signal_msg(cfg: dict, ym: str, r: dict, kind: str, interim: bool, months: dict, stamp: str) -> tuple[str, str, int, list[str]]:
    hi, lo = cfg["relock_high_pct"], cfg["relock_low_pct"]
    basis = f"{ym} 월중 누적({r['start']} ~ {r['end']})" if interim else f"{ym} 한 달 전체({r['start']} ~ {r['end']})"
    status = (
        f"{basis} 에스크로 해제 {r['released_xrp']:,} XRP, 새 에스크로 재잠금 {r['relocked_xrp']:,} XRP,"
        f" 재잠금률 {r['rate_pct']:.1f}% ({'기준 ' + str(int(hi)) + '% 이상' if kind == 'hi' else '기준 ' + str(int(lo)) + '% 미만'})."
        f" 순유출 {r['released_xrp'] - r['relocked_xrp']:,} XRP. 해제: {', '.join(r['released_rows']) or '없음'}."
        f" 재잠금: {', '.join(r['relocked_rows']) or '없음'}. 참고: {history_line(months, ym)}."
        f" 출처: XRPL 원장(xrplcluster 등 공개 JSON-RPC), 원장 {r['ledgers'][0]}~{r['ledgers'][1]}."
    )
    if kind == "hi":
        tilt = ("상방 가능성 다소 높음 (근거: 풀린 물량 중 다시 잠근 몫이 평소보다 커서 이번 달 시장에 풀릴 수 있는 공급이 줄어듦."
                " 확률 수치는 계산하지 않음)")
        title = f"[XRP-ESCROW] 재잠금률 {r['rate_pct']:.0f}% | 공급 감소 쪽, 추격 말고 확인"
        action = "보유 중이면 유지. 새로 사려면 ETF 흐름, 거래소 보유량 알림과 가격 위치를 같이 보고 분할로만."
    else:
        tilt = ("하방 가능성 다소 높음 (근거: 풀린 물량 중 다시 잠기지 않은 몫이 평소보다 커서 매도에 쓰일 수 있는 공급이 늘어남."
                " 확률 수치는 계산하지 않음)")
        title = f"[XRP-ESCROW] 재잠금률 {r['rate_pct']:.0f}% | 공급 증가 쪽, 추가매수 보류 검토"
        action = "추가매수는 잠시 보류 검토. 리플 공시나 대량 이동(거래소 입금) 소식이 있는지 확인."
    msg = body(stamp, "리플이 매달 에스크로에서 푼 XRP 중 다시 잠근 비율이 80% 이상이거나 70% 미만일 때만 오는 알림입니다.", status,
               action, "다음 달 해제(1일 09:00 KST) 뒤 다시 판정합니다. 70~80% 사이는 알림이 없습니다.",
               "이 알림만으로 정하지 않기. 미리 정한 비중과 손절 기준대로.",
               "재잠금률 하나로 사고팔기, 순유출 전부를 매도로 단정, 월중 수치를 확정으로 읽기(70% 미만은 확정 판정만 옴).", tilt)
    return title, msg, 4 if kind == "hi" else 4, ["lock" if kind == "hi" else "unlock"]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    now_utc = now.astimezone(UTC)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    months = state.setdefault("months", {})
    accounts = set(cfg["ripple_accounts"]) | set(state.get("learned_accounts") or [])
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    x = Xrpl(cfg["sources"]["xrpl"])
    hi, lo = float(cfg["relock_high_pct"]), float(cfg["relock_low_pct"])
    trace_min = int(cfg["trace_min_xrp"])
    cur = now_utc.strftime("%Y-%m")
    prev = (now_utc.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    out: list[tuple[str, str, int, list[str]]] = []

    def learn(r: dict) -> None:
        if r["learned"]:
            state["learned_accounts"] = sorted(set(state.get("learned_accounts") or []) | set(r["learned"]))
            accounts.update(r["learned"])

    if mode == "seed":
        y, m = map(int, cur.split("-"))
        for k in range(int(sys.argv[2]) if len(sys.argv) > 2 else 12, 0, -1):
            mm = (y * 12 + m - 1) - k
            ym = f"{mm // 12}-{mm % 12 + 1:02d}"
            s, e = month_bounds(ym)
            r = compute(x, accounts, s, e, trace_min)
            learn(r)
            months[ym] = {**r, "final": True, "sent_hi": False, "sent_lo": False, "note": "seed 2026-10-22 (발송 없음)"}
            print(ym, r["released_xrp"], r["relocked_xrp"], r["rate_pct"], r["relocked_rows"], "calls", x.calls)
    try:
        # 1) 지난달 확정 판정
        pm = months.get(prev) or {}
        if mode != "seed" and (not pm.get("final") or mode == "dry"):
            s, e = month_bounds(prev)
            r = compute(x, accounts, s, e, trace_min)
            print(f"final {prev}:", json.dumps(r, ensure_ascii=False))
            if not r["released_xrp"]:
                raise RuntimeError(f"{prev} 해제 내역 0 (계정 목록 또는 원장 조회 확인 필요)")
            if mode != "dry":
                learn(r)
            rate = r["rate_pct"]
            sent_hi, sent_lo = pm.get("sent_hi", False), pm.get("sent_lo", False)
            if rate >= hi and not sent_hi:
                out.append(signal_msg(cfg, prev, r, "hi", False, months, stamp))
                sent_hi = True
            elif rate < lo and not sent_lo:
                out.append(signal_msg(cfg, prev, r, "lo", False, months, stamp))
                sent_lo = True
            else:
                print(f"final {prev}: rate {rate}% -> no alert (hi sent={sent_hi})")
            months[prev] = {**r, "final": True, "sent_hi": sent_hi, "sent_lo": sent_lo, "at": now.isoformat()}
        # 2) 이번 달 중간 판정 (80% 이상만)
        cm = months.get(cur) or {}
        if mode != "seed" and now_utc.day >= int(cfg["interim_from_day"]) and not cm.get("sent_hi"):
            s, _ = month_bounds(cur)
            r = compute(x, accounts, s, now_utc.replace(second=0, microsecond=0), trace_min)
            print(f"interim {cur}:", json.dumps(r, ensure_ascii=False))
            if mode != "dry":
                learn(r)
            if r["released_xrp"] and r["rate_pct"] >= hi:
                out.append(signal_msg(cfg, cur, r, "hi", True, months, stamp))
                months[cur] = {**r, "final": False, "sent_hi": True, "sent_lo": False, "at": now.isoformat()}
            else:
                print(f"interim {cur}: rate {r['rate_pct']}% -> no alert (low side waits for month end)")
                months[cur] = {**r, "final": False, "sent_hi": False, "sent_lo": False, "at": now.isoformat()}
        else:
            print(f"interim {cur}: skipped (day {now_utc.day} < {cfg['interim_from_day']} or already sent)")
    except Exception as exc:  # noqa: BLE001
        print(f"escrow compute failed: {exc}", file=sys.stderr)
        fn = state.setdefault("fail_notice", {})
        key = now.strftime("%Y-%m-%d")
        if fn.get("compute") != key and mode != "seed":
            out.append(("[XRP-ESCROW] 에스크로 원장 조회 실패 | 판정 못 함", body(
                stamp, "리플 에스크로 재잠금률 알림의 데이터 조회 실패 안내.", f"XRPL 원장 {NA} ({str(exc)[:140]}).",
                "필요하면 XRPScan 에서 리플 에스크로 계정을 직접 확인.", "다음 평일 09:00에 다시 봅니다.",
                "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 재잠금 0으로 읽기."), 3, ["warning"]))
            fn["compute"] = key
    print(f"xrpl calls: {x.calls}")
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if mode == "auto":
            publish(topic, title, msg, pri, tags, server)
    if mode == "dry":
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
