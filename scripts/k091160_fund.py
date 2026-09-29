#!/usr/bin/env python3
"""091160 fundamentals 자동 갱신기.

매 평일 15:20 KST afternoon 슬롯에서 k091160_monitor.py 직전에 돈다.
- 컨센서스: 네이버 증권 finance API의 삼성전자·SK하이닉스 연간 영업이익 컨센서스를 매일 저장하고,
  7일 이상 쌓이면 가장 오래된(최대 28일 전) 값 대비 변화율로 추세를 정한다.
- 삼성전자 잠정실적: KRX 공시(네이버 전재) '연결재무제표기준영업(잠정)실적(공정공시)'에서 3Q26 영업이익을 읽고
  직전 분기 컨센서스 대비 차이를 계산한다. -5% 이하 미스면 추세를 down5, -10% 이하면 down10으로 강제한다.
- DRAM·NAND: TrendForce 보도자료에서 해당 분기 계약가격 QoQ 범위를 찾는다. 숫자가 없으면 방향(상승/하락)만 저장한다.
- HBM: 공식 자동 소스가 없어 configs 수동값을 유지한다.
결과는 state/k091160_fund.json에만 쓴다. 바뀐 내용이 있을 때만 ntfy 1회.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "k091160.json"
STATE_PATH = ROOT / "state" / "k091160_fund.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124 Safari/537.36 cwally-alerts"}
NAVER = "https://m.stock.naver.com/api/stock/"
P = "[091160]"


def http(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(v):
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def op_consensus(code: str, period: str) -> tuple[str | None, float | None]:
    """(기간키, 영업이익 억원). 컨센서스(isConsensus=Y) 열 중 가장 이른 것."""
    info = json.loads(http(f"{NAVER}{code}/finance/{period}"))["financeInfo"]
    keys = [t["key"] for t in info.get("trTitleList") or [] if t.get("isConsensus") == "Y"]
    if not keys:
        return None, None
    key = keys[0]
    for row in info.get("rowList") or []:
        if row.get("title") == "영업이익":
            return key, num((row.get("columns") or {}).get(key, {}).get("value"))
    return key, None


def samsung_prelim(since: str, period_end: str):
    rows = json.loads(http(f"{NAVER}005930/disclosure?pageSize=50"))
    for r in rows:
        title = r.get("title") or ""
        day = str(r.get("datetime") or "")[:10]
        if "영업(잠정)실적" not in title or day < since:
            continue
        doc = json.loads(http(f"{NAVER}005930/disclosure/{r['disclosureId']}"))
        html = (doc.get("disclosure") or {}).get("contents") or ""
        cells_rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
            cells_rows.append([c for c in cells if c])
        end = next((c[3] for c in cells_rows if len(c) >= 4 and c[0] == "당기실적"), None)
        if end != period_end:
            continue
        op = next((num(c[2]) for c in cells_rows if len(c) >= 3 and c[0] == "영업이익" and c[1] == "당해실적"), None)
        sales = next((num(c[2]) for c in cells_rows if len(c) >= 3 and c[0] == "매출액" and c[1] == "당해실적"), None)
        if op is None:
            continue
        return {"title": title, "date": day, "id": r["disclosureId"], "op_tn": op, "sales_tn": sales}
    return None


def quarter_label(now: datetime) -> str:
    return f"{(now.month - 1) // 3 + 1}Q{now.year % 100:02d}"


def trendforce(qlabel: str) -> dict:
    """TrendForce 보도자료 목록과 최근 메모리 보도자료 본문에서 계약가격 QoQ를 찾는다."""
    out = {"dram": None, "nand": None, "dram_dir": None, "nand_dir": None, "url": None}
    base = "https://www.trendforce.com"
    listing = http(base + "/presscenter/news")
    links = []
    for href, title in re.findall(r'href="(/presscenter/news/\d{8}-\d+\.html)"[^>]*>(.*?)</a>', listing, flags=re.S):
        t = re.sub(r"<[^>]+>", " ", title)
        if re.search(r"DRAM|NAND|Memory|HBM", t, re.I) and href not in links:
            links.append(href)
    texts = []
    for href in links[:6]:
        try:
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", http(base + href)))
            texts.append((base + href, txt))
        except Exception as exc:
            print(f"trendforce detail fail {href}: {exc}", file=sys.stderr)
    try:
        tag = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", http(base + "/research-tags/dram")))
        texts.append((base + "/research-tags/dram", tag))
    except Exception as exc:
        print(f"trendforce tag fail: {exc}", file=sys.stderr)
    rng = r"(\d{1,3})\s*[–\-~to]+\s*(\d{1,3})\s*%"
    for url, txt in texts:
        for sent in re.split(r"(?<=[.!?])\s", txt):
            if qlabel not in sent and not re.search(r"fourth quarter|Q4 ?20\d\d", sent, re.I):
                continue
            is_d = bool(re.search(r"DRAM", sent)) and "HBM" not in sent[:40]
            is_n = bool(re.search(r"NAND", sent))
            m = re.search(rng, sent)
            up = re.search(r"ris|increase|climb|up |gain|surge", sent, re.I)
            down = re.search(r"declin|fall|drop|decreas", sent, re.I)
            if m and re.search(r"contract", sent, re.I) and re.search(r"QoQ|quarter", sent, re.I):
                low = float(m.group(1))
                val = -low if down and not up else low
                if is_d and not is_n and out["dram"] is None:
                    out["dram"], out["url"] = val, url
                if is_n and not is_d and out["nand"] is None:
                    out["nand"], out["url"] = val, url
            if (up or down) and re.search(r"price", sent, re.I):
                d = "up" if up and not down else ("down" if down and not up else None)
                if is_d and out["dram_dir"] is None and d:
                    out["dram_dir"] = d
                if is_n and out["nand_dir"] is None and d:
                    out["nand_dir"] = d
    return out


def classify(chg_sum: float, chg_min: float) -> str:
    if chg_min <= -10:
        return "down10"
    if chg_sum <= -5:
        return "down5"
    if chg_sum >= 1:
        return "up"
    return "flat"


def publish(title: str, msg: str, pri: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import ntfy_util

    ntfy_util.publish("cwally-port", title, msg, priority=pri, tags=["memo", "chart_with_upwards_trend"])


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    auto = cfg.get("fund_auto") or {}
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today > str(cfg.get("valid_until") or "9999-12-31"):
        print("expired; silent")
        return 0
    st = {"history": {}, "effective": {}, "prelim": None, "last_alert_key": None, "fail_sent_date": None}
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    prev_eff = dict(st.get("effective") or {})
    eff = dict(prev_eff)
    errors, notes = [], []

    # 1) 컨센서스 스냅샷
    snap = {}
    try:
        for name, code in (("sam", "005930"), ("hyn", "000660")):
            ky, vy = op_consensus(code, "annual")
            kq, vq = op_consensus(code, "quarter")
            snap[name] = {"y_key": ky, "y": vy, "q_key": kq, "q": vq}
        if any(snap[n]["y"] is None for n in snap):
            raise ValueError("연간 컨센서스 없음")
        st["history"][today] = snap
    except Exception as exc:
        errors.append(f"컨센서스 조회 실패: {exc}")
    hist = st["history"]
    for d in sorted(hist):
        if d < (now - timedelta(days=45)).strftime("%Y-%m-%d"):
            hist.pop(d)
    min_days = int(auto.get("consensus_min_days", 7))
    win_days = int(auto.get("consensus_window_days", 28))
    trend_src = "수동값"
    if today in hist:
        cands = [d for d in sorted(hist) if d <= (now - timedelta(days=min_days)).strftime("%Y-%m-%d")]
        cands = [d for d in cands if d >= (now - timedelta(days=win_days + 3)).strftime("%Y-%m-%d")]
        base_d = cands[0] if cands else None
        if base_d:
            b, c = hist[base_d], hist[today]
            if all(b[n]["y_key"] == c[n]["y_key"] for n in ("sam", "hyn")):
                chg = {n: (c[n]["y"] / b[n]["y"] - 1) * 100 for n in ("sam", "hyn")}
                tot = (c["sam"]["y"] + c["hyn"]["y"]) / (b["sam"]["y"] + b["hyn"]["y"]) * 100 - 100
                eff["consensus_trend"] = classify(tot, min(chg.values()))
                eff["consensus_detail"] = f"{base_d}->{today} 합산 {tot:+.1f}% (삼성 {chg['sam']:+.1f}%, 하이닉스 {chg['hyn']:+.1f}%)"
                trend_src = "자동"
            else:
                notes.append("컨센서스 기준연도 변경, 비교 보류")
        if trend_src != "자동":
            eff.pop("consensus_trend", None)
            eff["consensus_detail"] = f"자동 비교 이력 {len(hist)}일, {min_days}일 이상 쌓이면 자동 판정. 그 전에는 config 수동값"

    # 2) 삼성전자 잠정실적
    pcfg = auto.get("samsung_prelim") or {}
    in_window = pcfg.get("watch_from", "9999") <= today <= pcfg.get("watch_to", "0000")
    if in_window or (st.get("prelim") is None and today >= pcfg.get("watch_from", "9999")):
        try:
            pre = samsung_prelim(pcfg.get("watch_from", today), pcfg.get("period_end", ""))
            if pre and not st.get("prelim"):
                base_q = None
                for d in sorted(hist, reverse=True):
                    h = hist[d]["sam"]
                    if h.get("q_key") == pcfg.get("q_key") and d < pre["date"]:
                        base_q = h["q"] / 10000.0  # 억원 -> 조원
                        break
                if base_q is None:
                    base_q = float(pcfg.get("fallback_consensus_tn"))
                pre["consensus_tn"] = round(base_q, 2)
                pre["surprise_pct"] = round((pre["op_tn"] / base_q - 1) * 100, 2)
                st["prelim"] = pre
        except Exception as exc:
            errors.append(f"잠정실적 조회 실패: {exc}")
    pre = st.get("prelim")
    if pre:
        s = pre["surprise_pct"]
        forced = "down10" if s <= -10 else ("down5" if s <= -5 else None)
        eff["prelim_detail"] = f"삼성 3Q26 잠정 영업이익 {pre['op_tn']:.2f}조 vs 컨센서스 {pre['consensus_tn']:.2f}조 ({s:+.1f}%), 공시 {pre['date']}"
        if forced and today <= (datetime.strptime(pre["date"], "%Y-%m-%d") + timedelta(days=28)).strftime("%Y-%m-%d"):
            order = ["up", "flat", "down5", "down10"]
            cur = eff.get("consensus_trend") or (cfg.get("fundamentals") or {}).get("consensus_trend", "flat")
            eff["consensus_trend"] = max(cur, forced, key=order.index)
            eff["consensus_detail"] = (eff.get("consensus_detail") or "") + f" | 잠정실적 미스로 {forced} 강제"

    # 3) DRAM·NAND
    ql = quarter_label(now)
    try:
        tf = trendforce(ql)
        eff["memory_quarter"] = ql
        eff["dram_qoq_pct"] = tf["dram"]
        eff["nand_qoq_pct"] = tf["nand"]
        eff["dram_dir"] = tf["dram_dir"] or ("up" if (tf["dram"] or 0) > 0 else None)
        eff["nand_dir"] = tf["nand_dir"] or ("up" if (tf["nand"] or 0) > 0 else None)
        eff["memory_src"] = tf["url"] or "https://www.trendforce.com/presscenter/news"
    except Exception as exc:
        errors.append(f"TrendForce 조회 실패: {exc}")
    eff["updated_at"] = stamp
    st["effective"] = eff
    st["updated_at"] = stamp

    def fmt(v):
        return "미확인" if v is None else f"{v:+.0f}%"

    summary = (
        f"컨센서스 {eff.get('consensus_trend', '수동값')} ({eff.get('consensus_detail', '')}) / "
        f"{eff.get('memory_quarter', ql)} DRAM {fmt(eff.get('dram_qoq_pct'))}({eff.get('dram_dir') or '방향 미확인'}), "
        f"NAND {fmt(eff.get('nand_qoq_pct'))}({eff.get('nand_dir') or '방향 미확인'}) / "
        f"HBM 수동값 {(cfg.get('fundamentals') or {}).get('hbm')}"
    )
    if pre:
        summary = eff["prelim_detail"] + " / " + summary
    print(summary)
    for e in errors:
        print(e, file=sys.stderr)

    key_fields = ("consensus_trend", "dram_qoq_pct", "nand_qoq_pct", "dram_dir", "nand_dir", "memory_quarter", "prelim_detail")
    changed = [k for k in key_fields if prev_eff.get(k) != eff.get(k)]
    first_run = not prev_eff
    alert = None
    if pre and "prelim_detail" in changed:
        s = pre["surprise_pct"]
        act = "신규·추가 매수 중단" if s <= -5 else ("변동 없음, 규칙대로" if s > -3 else "경계, 다음 날 판정 재확인")
        alert = (f"{P} 삼성 잠정실적 반영 | {act}", 4)
    elif changed and not first_run:
        alert = (f"{P} 펀더멘털 갱신 | 단계 조건 재계산", 3)
    elif errors and in_window and st.get("fail_sent_date") != today:
        alert = (f"{P} 펀더멘털 조회실패 | 수동 확인", 3)
        st["fail_sent_date"] = today

    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not alert:
        print("no fundamental change; silent")
        return 0
    body = "\n".join(
        [
            f"{stamp} | 091160 펀더멘털",
            "이게 뭐 알림: 091160 분할매수 규칙에 쓰는 실적·메모리 값이 바뀌었는지 알려준다.",
            f"지금 결과/상태: {summary}" + (f" | 오류: {'; '.join(errors)[:200]}" if errors else ""),
            "지금 할 일: 오늘 15:20 단계 판정은 이 값으로 다시 계산됐다. 같은 시각 [091160] 단계 알림이 있으면 그것을 따른다.",
            "나중에: HBM 값은 SK하이닉스 3Q 실적발표(10월 하순 예상) 뒤 configs/k091160.json에서 직접 고친다.",
            "익절/줄일 때: 컨센서스가 down10이면 신규 금지와 비중 축소를 검토한다.",
            "하지 말 것: 잠정실적 헤드라인만 보고 장중 추격 매수나 투매를 하지 않는다.",
        ]
    )
    print(alert[0])
    print(body)
    if not dry:
        publish(alert[0], body, alert[1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # 다른 알림 스크립트 실행을 막지 않는다
        print(f"k091160 unexpected error: {exc!r}", file=sys.stderr)
        raise SystemExit(0)
