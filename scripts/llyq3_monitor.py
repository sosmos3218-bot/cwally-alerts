#!/usr/bin/env python3
"""LLY Q3 2026 one-shot: plug release facts into the hold/buy/sell table.

Buy requires guidance raise and price decline not worse than Q2, and a confirmed
Oct 29 regular close. Before the close, the action stays 보유. Not investment advice.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "llyq3.json"
STATE = ROOT / "state" / "llyq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}


def fetch(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore"), resp.geturl()


def textify(html):
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def load_state():
    st = {
        "final_notified": False,
        "release_notified": False,
        "call_notified": False,
        "empty_notified": False,
        "fail_notified": False,
        "expire_notified": False,
        "score": None,
    }
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st, now):
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def first(pattern, text):
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def abs_url(href):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://investor.lilly.com" + href
    return "https://investor.lilly.com/" + href


def collect(cfg):
    blobs, errors, links = [], [], []
    try:
        raw, _ = fetch(cfg["sources"]["news"])
        blobs.append(textify(raw))
        for href in re.findall(r'href="([^"]+)"', raw, re.I):
            if re.search(r"third-quarter|q3-2026|third quarter", href, re.I) and "2026" in href:
                links.append(abs_url(href))
    except Exception as exc:
        errors.append(f"news: {exc}")
    for link in links[:4]:
        try:
            raw, _ = fetch(link)
            blobs.append(textify(raw))
        except Exception as exc:
            errors.append(f"link: {exc}")
    try:
        raw, _ = fetch(cfg["sources"]["sec"])
        data = json.loads(raw)
        recent = data.get("filings", {}).get("recent", {})
        for form, date, acc, doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if date >= "2026-10-29" and form in {"8-K", "EX-99.1"}:
                url = f"https://www.sec.gov/Archives/edgar/data/59478/{acc.replace('-', '')}/{doc}"
                try:
                    body, _ = fetch(url)
                    blobs.append(textify(body))
                except Exception as exc:
                    errors.append(f"secfile: {exc}")
                break
    except Exception as exc:
        errors.append(f"sec: {exc}")
    return "\n".join(blobs), "; ".join(errors)


def close_on(cfg):
    try:
        raw, _ = fetch(cfg["sources"]["quote"])
        data = json.loads(raw)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None, "시세 응답 없음"
        quote = result[0]
        ts = quote.get("timestamp") or []
        closes = ((quote.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        for stamp, price in zip(ts, closes):
            if price is None:
                continue
            day = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(KST)
            # Regular session of Oct 29 ET closes at 05:00 KST Oct 30. Yahoo stamps the session date in ET.
            et = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(timezone(timedelta(hours=-4)))
            if et.strftime("%Y-%m-%d") == "2026-10-29":
                return float(price), f"{et:%Y-%m-%d} ET 세션 {price:.2f}달러"
        return None, "10월 29일 세션 종가 없음"
    except Exception as exc:
        return None, f"시세 조회 실패: {exc}"


def parse_release(text):
    third = bool(re.search(r"third quarter (?:ended|of) (?:September|Sep\.?) 30,? 2026", text, re.I))
    embargo = bool(re.search(r"embargoed until", text, re.I))
    revenue = first(r"revenue[^.]{0,40}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    if revenue is None:
        revenue = first(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion in revenue", text)
    mounjaro = first(r"Mounjaro[^.]{0,80}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    zepbound = first(r"Zepbound[^.]{0,80}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion", text)
    foundayo = first(r"(?:Foundayo|orforglipron)[^.]{0,80}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*million", text)
    price = None
    price_m = re.search(
        r"realized prices (?:decreased|declined|were lower(?: by)?|fell)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
        text,
        re.I,
    )
    if price_m:
        price = -float(price_m.group(1))
    else:
        up = re.search(r"realized prices (?:increased|rose)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)\s*%", text, re.I)
        if up:
            price = float(up.group(1))
    volume = first(r"volume[^.]{0,40}?(?:increased|grew|growth of)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)\s*%", text)
    us_price = None
    us_m = re.search(
        r"U\.S\.[^.]{0,80}?prices (?:decreased|declined|lower)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)\s*%",
        text,
        re.I,
    )
    if us_m:
        us_price = -float(us_m.group(1))
    ous = first(r"(?:outside the U\.S\.|international)[^.]{0,40}?(?:increased|grew)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)\s*%", text)
    guide = re.search(
        r"revenue guidance[^.]{0,60}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion to \$\s*([0-9]+(?:\.[0-9]+)?)\s*billion",
        text,
        re.I,
    )
    if not guide:
        guide = re.search(
            r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*billion to \$\s*([0-9]+(?:\.[0-9]+)?)\s*billion",
            text,
            re.I,
        )
    guide_low = float(guide.group(1)) if guide else None
    guide_high = float(guide.group(2)) if guide else None
    guide_cut = bool(re.search(r"(lowered|reduced|decreased).{0,40}(revenue guidance|full-year revenue)", text, re.I))
    guide_up = bool(re.search(r"(raised|increased).{0,40}(revenue guidance|full-year revenue)", text, re.I))
    published = bool(third and revenue is not None and not embargo)
    return {
        "published": published,
        "revenue": revenue,
        "mounjaro": mounjaro,
        "zepbound": zepbound,
        "foundayo": foundayo,
        "price": price,
        "volume": volume,
        "us_price": us_price,
        "ous": ous,
        "guide_low": guide_low,
        "guide_high": guide_high,
        "guide_cut": guide_cut,
        "guide_up": guide_up,
    }


def mark(status, fact):
    return status, fact


def score(parsed, close, close_note, close_ready):
    b = {
        "guide": ("확인불가", "2026 매출 가이던스 구간이 원문에 없음"),
        "price": ("확인불가", "전 세계 실현가격 변화가 원문에 없음"),
        "volume": ("확인불가", "물량 증가율이 원문에 없음"),
        "combo": ("확인불가", "Mounjaro·Zepbound 매출이 원문에 없음"),
        "foundayo": ("확인불가", "Foundayo 매출이 원문에 없음"),
        "ous": ("확인불가", "해외 물량 수치가 원문에 없음"),
        "close": ("확인불가", close_note),
    }
    lo, hi = parsed.get("guide_low"), parsed.get("guide_high")
    if parsed.get("guide_cut") or (hi is not None and hi < 87 and lo is not None and lo < 85):
        b["guide"] = mark("이탈", f"가이던스 {lo}~{hi}십억달러, 하향")
    elif hi is not None and hi > 87:
        b["guide"] = mark("통과", f"가이던스 {lo}~{hi}십억달러, 상단 870억 초과")
    elif lo is not None:
        b["guide"] = mark("경고", f"가이던스 {lo}~{hi}십억달러, 상단 870억 유지 또는 미상향")
    price = parsed.get("price")
    if price is not None:
        if price >= -13:
            b["price"] = mark("통과", f"실현가격 {price:.1f}%, Q2 -13%보다 악화 아님")
        else:
            b["price"] = mark("이탈", f"실현가격 {price:.1f}%, Q2 -13%보다 악화")
    volume = parsed.get("volume")
    if volume is not None and price is not None:
        if volume + price > 0:
            b["volume"] = mark("통과", f"물량 {volume:.1f}%가 가격 {price:.1f}%를 상쇄")
        else:
            b["volume"] = mark("이탈", f"물량 {volume:.1f}%가 가격 {price:.1f}%를 상쇄하지 못함")
    elif volume is not None:
        b["volume"] = mark("경고", f"물량 {volume:.1f}%, 가격과 짝이 없음")
    mj, zb = parsed.get("mounjaro"), parsed.get("zepbound")
    if mj is not None and zb is not None:
        combo = mj + zb
        b["combo"] = mark("통과" if combo >= 14.871 else "경고", f"합산 {combo:.2f}십억달러, Q2 14.87")
    fy = parsed.get("foundayo")
    if fy is not None:
        if fy >= 250:
            b["foundayo"] = mark("통과", f"Foundayo {fy:.0f}백만달러")
        elif fy >= 150:
            b["foundayo"] = mark("경고", f"Foundayo {fy:.0f}백만달러, Q2 98 대비 증가이나 가속 약함")
        else:
            b["foundayo"] = mark("이탈", f"Foundayo {fy:.0f}백만달러, 150 미만")
    ous = parsed.get("ous")
    if ous is not None:
        b["ous"] = mark("통과" if ous >= 50 else "이탈", f"해외 증가 {ous:.1f}%")
    if close is not None and close_ready:
        b["close"] = mark("통과", f"확정 종가 {close:.2f}달러. {close_note}. 기준 10/7 1188.72")
    elif close is not None:
        b["close"] = mark("경고", f"세션 수치 {close:.2f}달러이나 마감 전. {close_note}")
    sell_flags = []
    if b["guide"][0] == "이탈":
        sell_flags.append("가이던스 하향")
    if parsed.get("us_price") is not None and parsed["us_price"] < -9:
        sell_flags.append("미국 가격 하락 확대")
    if b["ous"][0] == "이탈":
        sell_flags.append("해외 물량 둔화")
    if b["foundayo"][0] == "이탈":
        sell_flags.append("Foundayo 부진")
    buy_ready = b["guide"][0] == "통과" and b["price"][0] == "통과"
    if len(sell_flags) >= 2 and close_ready:
        action = "매도"
        why = "이탈 " + str(len(sell_flags)) + "개: " + ", ".join(sell_flags)
    elif buy_ready and close_ready and b["close"][0] == "통과":
        action = "매수"
        why = "가이던스 상향과 가격 비악화, 확정 종가 확인"
    else:
        action = "보유"
        if buy_ready and not close_ready:
            why = "필수 2개 통과이나 확정 종가 전. 매수 전환 보류"
        elif sell_flags and not close_ready:
            why = "이탈 후보 " + ", ".join(sell_flags) + ". 종가 확정 전 매도 전환 보류"
        else:
            why = "필수 조건 미충족 또는 확인불가. 추격매수 보류"
    return action, why, b, sell_flags


def emit(cfg, title, lines, pri):
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title[:180],
        "\n".join(lines),
        pri,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    close_ready_at = datetime.fromisoformat(cfg["close_ready_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if mode not in {"force", "dry"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode != "force":
        print("already final; skip")
        save_state(st, now)
        return 0
    text, err = collect(cfg)
    parsed = parse_release(text) if text else {}
    close, close_note = close_on(cfg)
    close_ready = now >= close_ready_at or mode == "force"
    st["parsed"] = parsed
    st["close"] = close
    if not parsed.get("published"):
        if mode == "dry":
            print("not published", err)
            save_state(st, now)
            return 0
        late = now >= close_ready_at
        if not text and not st.get("fail_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | LLY | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + (err or "출처 없음") + ". 실패로 채점하지 않음.",
                "지금 할 일: IR 원문을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 23:40 KST와 10/30 05:20 KST에 재확인. 만료 2026-10-30.",
                "익절/줄일 때: 조회 실패로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 실적 미스로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[LLY] 조회 실패 | 채점 보류", lines, 4)
            st["fail_notified"] = True
            save_state(st, now)
            return 0
        if late and not st.get("expire_notified"):
            lines = [
                now.strftime("%Y-%m-%d %H:%M KST") + " | LLY | 3분기 실적",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 확정 종가 시각에도 3분기 원문 없음. Q2 수치로 채점하지 않음.",
                "지금 할 일: 보유 유지. IR을 직접 확인하고 이 알림으로 매매하지 마세요.",
                "나중에: 이 일회 창은 2026-10-30 05:20 KST에 끝남.",
                "익절/줄일 때: 미발표로 비중 조정 금지.",
                "하지 말 것: 미발표를 가이던스 하향으로 읽기. 신용, 미수, 추격매수.",
            ]
            emit(cfg, "[LLY] 원문 없음 | 보유 유지", lines, 3)
            st["expire_notified"] = True
            st["final_notified"] = True
            save_state(st, now)
            return 0
        if st.get("empty_notified") and mode != "force":
            print("empty already sent")
            save_state(st, now)
            return 0
        lines = [
            now.strftime("%Y-%m-%d %H:%M KST") + " | LLY | 3분기 실적",
            "이게 뭐 알림: " + cfg["explain"],
            "지금 상태: 3분기 원문이 아직 아님. Q2로 채점하지 않음. 기준은 조건부 보유.",
            "지금 할 일: 보유 유지. 발표 전 추가매수 금지.",
            "나중에: 23:40 KST 콜 후, 10/30 05:20 KST 확정 종가로 다시 채점.",
            "익절/줄일 때: 미발표 상태로 비중 조정 금지.",
            "하지 말 것: 장중 시세를 확정 종가로 쓰기. 신용, 미수, 추격매수.",
        ]
        emit(cfg, "[LLY] 원문 없음 | 보유 유지", lines, 3)
        st["empty_notified"] = True
        save_state(st, now)
        return 0
    action, why, parts, flags = score(parsed, close, close_note, close_ready)
    rev = parsed.get("revenue")
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | LLY | 3분기 기준표",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: 결론 " + action + ". " + why
        + ". 매출 " + (f"{rev:.2f}십억달러" if rev else "확인불가")
        + ". 가이던스 " + parts["guide"][0] + " / " + parts["guide"][1]
        + ". 가격 " + parts["price"][0] + " / " + parts["price"][1]
        + ". 물량 " + parts["volume"][0] + " / " + parts["volume"][1]
        + ". 두 제품 " + parts["combo"][0] + " / " + parts["combo"][1]
        + ". Foundayo " + parts["foundayo"][0] + " / " + parts["foundayo"][1]
        + ". 종가 " + parts["close"][0] + " / " + parts["close"][1]
        + ". 매출 컨센서스 상회만으로는 매수 아님.",
        "지금 할 일: " + action + ". 확인불가는 합격으로 채우지 말 것.",
        "나중에: 확정 종가는 10/30 05:20 KST. 그 전 매수 전환 없음. 2027 EPS 상향은 보조 조건.",
        "익절/줄일 때: 가이던스 하향, 미국 가격 악화, 해외 물량 둔화, Foundayo 부진 중 2개면 매도.",
        "하지 말 것: 장중 가격을 종가로 단정. 노보 배수만으로 매도. 신용, 미수, 추격매수. 투자 권유 아님.",
        "출처: " + cfg["sources"]["news"],
    ]
    title = f"[LLY] {action} | 3분기 기준표"
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        save_state(st, now)
        return 0
    stage = "final" if close_ready else ("call" if now.hour >= 23 else "release")
    stage_key = {"final": "final_notified", "call": "call_notified", "release": "release_notified"}[stage]
    if st.get(stage_key) and st.get("score") == action and mode != "force":
        print(f"{stage} already sent with {action}")
        save_state(st, now)
        return 0
    emit(cfg, title, lines, 4 if action != "보유" else 3)
    st[stage_key] = True
    st["score"] = action
    if stage == "final":
        st["final_notified"] = True
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
