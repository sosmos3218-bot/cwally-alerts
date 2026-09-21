#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "configs" / "smh.json"
STATE = ROOT / "state" / "smh.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
PAGES = (
    "https://companiesmarketcap.com/vaneck-semiconductor-etf/holdings/",
    "https://www.tradingview.com/symbols/NASDAQ-SMH/holdings/",
)
SISE = "https://api.finance.naver.com/siseJson.naver?symbol={c}&requestType=1&startTime={a}&endTime={b}&timeframe=day"
YCH = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=1mo"


def ntfy(server, title, msg, pri=3, tags="chart_with_upwards_trend"):
    body = json.dumps({"topic": "cwally-port", "title": title, "message": msg, "priority": pri, "tags": tags.split(",")}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print(r.read().decode())


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


def parse_asof(text):
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    months = "January February March April May June July August September October November December".split()
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})", text, re.I)
    if not m:
        return None
    return f"{m.group(3)}-{months.index(m.group(1).title())+1:02d}-{int(m.group(2)):02d}"


def holdings():
    err = None
    for url in PAGES:
        try:
            html = get(url)
        except Exception as exc:
            err = f"{url}: {exc}"
            continue
        asof = None
        for pat in (r"holdings as of ([A-Za-z]+ \d{1,2}, 20\d{2})", r"as of ([A-Za-z]+ \d{1,2}, 20\d{2})", r"(20\d{2}-\d{2}-\d{2})"):
            m = re.search(pat, html, re.I)
            if m:
                asof = parse_asof(m.group(1))
                if asof:
                    break
        item = {}
        low = html.lower()
        for name in ("sk hynix", "skhyv", "skhy"):
            i = low.find(name)
            if i < 0:
                continue
            chunk = re.sub(r"<[^>]+>", " ", html[max(0, i-180):i+420])
            chunk = re.sub(r"\s+", " ", chunk)
            pct = re.search(r"(\d+(?:\.\d+)?)\s*%", chunk)
            sh = re.search(r"([\d,]{6,})", chunk)
            if pct:
                item["weight_pct"] = float(pct.group(1))
            if sh:
                item["shares"] = int(sh.group(1).replace(",", ""))
            val = re.search(r"\$([\d.]+)\s*(B|M)", chunk, re.I)
            if val:
                n = float(val.group(1))
                item["value_usd"] = n * (1_000_000_000 if val.group(2).upper() == "B" else 1_000_000)
            break
        if asof or item:
            return {"ok": True, "asof": asof, "item": item, "src": url}
    return {"ok": False, "asof": None, "item": {}, "src": None, "err": err}


def usd(v):
    if v is None:
        return "없음"
    s = "+" if v > 0 else ""
    return f"{s}{v/100_000_000:.2f}억달러" if abs(v) >= 100_000_000 else f"{s}{v:,.0f}달러"


def krw(v):
    if v is None:
        return "없음"
    s = "+" if v > 0 else ""
    if abs(v) >= 1_000_000_000_000:
        return f"{s}{v/1_000_000_000_000:.2f}조"
    if abs(v) >= 100_000_000:
        return f"{s}{v/100_000_000:.0f}억"
    return f"{s}{v:,.0f}원"


def skhy_vol():
    data = json.loads(get(YCH.format(t="SKHY")))
    res = data["chart"]["result"][0]
    q = (res.get("indicators") or {}).get("quote") or [{}]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        if not ("2026-09-18" <= day <= "2026-09-22"):
            continue
        close = q[0].get("close", [None])[i] if i < len(q[0].get("close") or []) else None
        vol = q[0].get("volume", [None])[i] if i < len(q[0].get("volume") or []) else None
        if close is None or vol is None:
            continue
        out.append((day, float(close), float(vol)))
    return out


def kr_flow(listed):
    text = get(SISE.format(c="000660", a="20260917", b="20260922"))
    rows = []
    for m in re.finditer(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\]', text):
        day = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        rows.append({"d": day, "c": float(m.group(5)), "o": float(m.group(7))})
    rows.sort(key=lambda x: x["d"])
    prev = None
    out = []
    for r in rows:
        net = None if prev is None else (r["o"] - prev) / 100.0 * listed * r["c"]
        if "2026-09-18" <= r["d"] <= "2026-09-22":
            out.append((r["d"], r["o"], net))
        prev = r["o"]
    return out


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    st = {"final_notified": False, "fail_notified": False, "expire_notified": False}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg["year_guard"]) or not (cfg["window_start"] <= today <= cfg["window_end"]):
        print("smh window skip")
        return 0
    if st.get("final_notified"):
        print("smh already sent")
        return 0
    server = cfg.get("ntfy_server") or "https://ntfy.sh"
    h = holdings()
    asof = h.get("asof")
    item = h.get("item") or {}
    st.update({"last_check": stamp, "holdings_asof": asof, "weight_pct": item.get("weight_pct"), "source": h.get("src")})
    official = asof in set(cfg["target_asof"]) or (asof is not None and asof >= "2026-09-21")
    if not h.get("ok") and not official:
        if not st.get("fail_notified"):
            ntfy(server, "[SMH] 보유내역 조회 실패 | 판정 보류", "\n".join([
                f"{stamp} | SMH | SK하이닉스 ADR",
                f"이게 뭐 알림: {cfg['explain']}",
                f"지금 결과: 조회 실패. {h.get('err') or '출처 없음'}",
                "지금 할 일: VanEck 보유를 직접 보세요. 4조 유입으로 단정하지 마세요.",
                "나중에: 9/24 아침까지 재시도.",
                "익절/줄일 때: 이 알림으로 매매하지 않습니다.",
                "하지 말 것: 조회 실패를 호재로 해석.",
            ]), 4, "warning")
            st["fail_notified"] = True
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if not official:
        print(f"smh asof={asof} wait")
        if today == cfg["window_end"] and not st.get("expire_notified"):
            ntfy(server, "[SMH] 공식 보유 미반영 | 추정 보류", "\n".join([
                f"{stamp} | SMH | SK하이닉스 ADR",
                f"이게 뭐 알림: {cfg['explain']}",
                f"지금 결과: 공개 기준일 {asof or '없음'}. 9/21·22 공식값이 창 종료까지 없습니다.",
                "지금 할 일: 영상 5%·34.3억 달러를 확정값으로 쓰지 마세요.",
                "나중에: VanEck 보유 갱신 후 수동 확인.",
                "익절/줄일 때: 이 알림으로 매매하지 않습니다.",
                "하지 말 것: 미공개를 편입 완료로 단정.",
            ]), 3, "hourglass")
            st["expire_notified"] = True
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    w = item.get("weight_pct")
    sh = item.get("shares")
    val = item.get("value_usd")
    try:
        vols = skhy_vol()
        vol_txt = ", ".join(f"{d[5:]} {int(v):,}주/{c:.2f}달러" for d, c, v in vols) or "없음"
        vol_sum = sum(c * v for _, c, v in vols)
    except Exception as exc:
        vol_txt, vol_sum = f"조회 실패({exc})", None
    try:
        flows = kr_flow(int(cfg["listed_shares"]))
        flow_txt = ", ".join(f"{d[5:]} 소진율 {o:.2f}% / {krw(n)}" for d, o, n in flows) or "없음"
        flow_sum = sum(n or 0 for _, _, n in flows)
    except Exception as exc:
        flow_txt, flow_sum = f"조회 실패({exc})", None
    claim_w, claim_u = float(cfg["claim_weight_pct"]), float(cfg["claim_usd"])
    if w is None:
        gap = "비중 숫자 없음."
        title = "[SMH] 공식비중 없음 | 추정 대조"
    elif abs(w - claim_w) <= 0.5:
        gap = f"비중 오차 {w-claim_w:+.2f}%p. 영상 5%와 가깝습니다."
        title = f"[SMH] 공식비중 {w:.2f}% | 추정 근접"
    elif w < 1:
        gap = f"비중 {w:.2f}%. 영상 5%보다 훐씬 작습니다."
        title = f"[SMH] 공식비중 {w:.2f}% | 5% 미달"
    else:
        gap = f"비중 오차 {w-claim_w:+.2f}%p."
        title = f"[SMH] 공식비중 {w:.2f}% | 추정 대조"
    msg = "\n".join([
        f"{stamp} | SMH | SK하이닉스 ADR as-of {asof}",
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과: 비중 {w if w is not None else '없음'}%, 주식수 {sh or '없음'}, 평가액 {usd(val)}.",
        f"영상 추정: {claim_w:.1f}% / {usd(claim_u)} / {krw(cfg['claim_krw'])}. {gap}",
        f"평가액 오차: {usd(None if val is None else val-claim_u)}.",
        f"SKHY 거래 9/18-9/22: {vol_txt}. 대금합 {usd(vol_sum)}.",
        f"한국 외국인 9/18-9/22(소진율 추정): {flow_txt}. 누적추정 {krw(flow_sum)}.",
        "지금 할 일: 공식 비중만 재료로 보고, 한국 외국인과 ADR 매수를 한 숫자로 합치지 마세요.",
        "나중에: 이 점검은 1회입니다.",
        "익절/줄일 때: 이 알림으로 사거나 팔지 않습니다.",
        "하지 말 것: 거래대금 전체를 SMH 매수로 단정. 소진율 추정을 거래소 확정 순매수로 단정.",
        f"출처: {h.get('src') or '없음'}",
    ])
    print(msg)
    ntfy(server, title, msg, 4)
    st["final_notified"] = True
    st["updated_at"] = stamp
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
