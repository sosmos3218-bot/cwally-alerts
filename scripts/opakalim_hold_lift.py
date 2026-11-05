#!/usr/bin/env python3
"""Opakalim (오파칼림, BHV-7000) partial clinical hold-lift checklist for [326030].

Used only by scripts/326030_monitor.py. Detects an official lift disclosure and builds a
review-only ntfy body: whether 물타기 금지 can lift, price reaction, scope, add-buy zones.
"""
from __future__ import annotations

import re
from datetime import datetime


def is_hold_lift(cfg: dict, title: str, text: str | None = None) -> bool:
    hl = cfg.get("hold_lift") or {}
    if not hl.get("enabled", True):
        return False
    blob = f"{title}\n{text or ''}"
    if not any(d in blob for d in hl.get("drug_names") or []):
        return False
    for ex in hl.get("exclude_title_any") or []:
        if ex in title and not any(k in blob for k in ("해제", "lifted", "lift", "재개")):
            return False
    return any(k.casefold() in blob.casefold() for k in hl.get("title_or_body_any") or [])


def lift_scope(title: str, text: str | None) -> str:
    blob = f"{title}\n{text or ''}"
    low = blob.casefold()
    full_hints = ("전면 해제", "전체 해제", "clinical hold has been lifted", "hold has been fully lifted", "완전 해제")
    part_hints = ("부분 해제", "partial lift", "일부 해제", "RISE2만", "제한적 해제")
    resume = (
        "신규 환자 등록 재개", "신규 환자 등록을 재개", "환자 등록 재개", "환자 등록을 재개",
        "enrollment resum", "screening resum", "등록 재개",
    )
    lift_clear = (
        "clinical hold 해제", "partial clinical hold 해제", "hold 해제", "hold를 해제",
        "보류 해제", "보류를 해제", "clinical hold lifted", "hold lifted",
    )
    if any(h in blob for h in full_hints) or (("hold lifted" in low or "clinical hold lifted" in low) and "partial" not in low):
        return "전면에 가까운 해제 문구"
    if "partial clinical hold" in low and any(x in blob for x in ("해제", "lift")):
        return "부분 해제(또는 Partial Hold 해제) 문구"
    if any(h in blob for h in part_hints):
        return "부분 해제(또는 제한적) 문구"
    if any(h.casefold() in low for h in lift_clear) or any(h in blob for h in resume):
        return "해제/등록 재개 문구(범위는 원문 확인)"
    return "해제 범위 문구 불명확(원문 확인 필요)"


def ma_n(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def buy_zones(cfg: dict, rows: list[dict], close: float | None) -> list[str]:
    hl = cfg["hold_lift"]
    plan_z = int(hl.get("add_zone_plan") or 83400)
    ps = cfg["position"]
    out = []
    out.append(
        f"계획 추가매수 전제선: 종가 {plan_z:,}원 이상(보류 전 9/10 종가선) + 보류 해제 공시 확인, "
        f"합계 비중 {ps['max_pct']}%({ps['max_krw']:,}원) 안"
    )
    room = max(0, int(ps["max_krw"]) - int(ps["cost"]))
    if close:
        sh = int(room // close) if close > 0 else 0
        out.append(f"남은 한도 대략 {room:,}원(평단 기준 원금 {ps['cost']:,}원 차감), 지금 종가 기준 약 {sh}주까지(참고)")
    closes = [r["close"] for r in rows or []]
    m20 = ma_n(closes, 20)
    m60 = ma_n(closes, 60)
    out.append(f"20일 평균 {m20:,.0f}원 근처 눌림(참고, 자동 지정가 아님)" if m20 is not None else f"20일 평균: 종가 이력 {len(closes)}일로 부족")
    out.append(f"60일 평균 {m60:,.0f}원 근처 눌림(참고)" if m60 is not None else f"60일 평균: 종가 이력 {len(closes)}일로 부족(필요 시 수동 확인)")
    if closes:
        support = min(closes[-20:]) if len(closes) >= 5 else min(closes)
        out.append(f"최근 지지(최근 {min(20, len(closes))}거래일 최저 종가) {support:,.0f}원")
    if close is not None and len(closes) >= 2:
        prev = closes[-2]
        out.append(f"직전 종가 대비 {(close / prev - 1) * 100:+.1f}% (공시 직후 갭/반응 참고)")
    out.append(f"평단 {ps['avg']:,}원은 추가매수 목표가 아니라 보유 원가 기준(지금 종가가 평단 아래면 물타기 성격이 커짐)")
    return out


def hold_lift_verdict(scope: str, close: float | None, plan_z: int) -> tuple[str, str]:
    unclear = "불명확" in scope
    below = close is not None and close < plan_z
    if unclear:
        return (
            "보류 유지(원문 확인)",
            "공시 원문에서 오파칼림(Opakalim) Partial Clinical Hold가 실제로 해제됐는지, RISE2/RISE3 적용 범위를 확인하세요. 확인 전에는 물타기 금지 유지.",
        )
    if below:
        return (
            "조건부(해제는 보이나 가격 전제 미달)",
            f"해제 공시로 보이면 물타기 금지의 '공시 조건'은 충족 후보입니다. 다만 기존 계획 추가매수 전제선 {plan_z:,}원 종가 이상이 아직이라 추격/물타기는 보류하고, 전제선 회복과 비중 한도만 점검하세요.",
        )
    return (
        "해제 검토 가능(사람 최종 확인)",
        f"공식 공시상 해제 후보이고 종가가 계획 전제선 {plan_z:,}원 이상입니다. 원문으로 전면/부분 범위와 거래 종결(딜 클로징) 상태를 확인한 뒤에만, 합계 비중 3% 안에서 추가 매수를 검토하세요.",
    )


def find_hold_lift(cfg: dict, st: dict, io: dict, fails: list[str], engine) -> dict | None:
    hl = cfg.get("hold_lift") or {}
    if not hl.get("enabled", True):
        return None
    seen = st.setdefault("hold_lift_seen", [])
    notice_seen = st.setdefault("notice_seen", [])
    since = hl.get("since") or "2026-11-05"
    try:
        rows = io["notices"](cfg)
    except Exception as exc:  # noqa: BLE001
        fails.append(f"{cfg['code']} 보류해제 공시 피드 (네이버 {str(exc)[:60]}; 다음 실행에서 다시 확인)")
        rows = []
    hit = None
    for r in sorted(rows, key=lambda x: str(x.get("datetime") or "")):
        title = engine.scrub(str(r.get("title") or ""))
        text = str(r.get("text") or r.get("contents") or "")
        no = str(r.get("id") or r.get("no") or "")
        day = str(r.get("datetime") or "")[:10]
        if not no or no in seen or day < since:
            continue
        if is_hold_lift(cfg, title, text):
            hit = {
                "no": no,
                "title": title,
                "text": text,
                "datetime": str(r.get("datetime") or "")[:16].replace("T", " "),
                "src": "naver_notice",
                "link": cfg["sources"]["notice_page"].format(code=cfg["code"]),
            }
            break
    if hit is None:
        try:
            page = io["http"](cfg["sources"]["skbp_list"])
        except Exception as exc:  # noqa: BLE001
            print(f"skbp press optional skip: {exc}")
            page = ""
        for m in re.finditer(
            r'data-seq="(\d+)"[\s\S]{0,200}?class="tit">([^<]+)<[\s\S]{0,120}?class="date">(\d{4}\.\d{2}\.\d{2})',
            page,
        ):
            seq, title, day = m.group(1), engine.scrub(m.group(2).strip()), m.group(3).replace(".", "-")
            key = f"skbp:{seq}"
            if key in seen or day < since:
                continue
            if is_hold_lift(cfg, title, None):
                hit = {
                    "no": key,
                    "title": title,
                    "text": "",
                    "datetime": day,
                    "src": "skbp_press",
                    "link": cfg["sources"]["skbp_view"].format(seq=seq),
                }
                break
    if not hit:
        return None
    seen.append(hit["no"])
    if hit["src"] == "naver_notice" and hit["no"] not in notice_seen:
        notice_seen.append(hit["no"])
    return hit


def build_hold_lift(cfg: dict, hit: dict, rows: list[dict], stamp: str, engine, pos_line) -> tuple[str, str, int]:
    hl = cfg["hold_lift"]
    close = rows[-1]["close"] if rows else None
    plan_z = int(hl.get("add_zone_plan") or 83400)
    scope = lift_scope(hit["title"], hit.get("text"))
    label, action = hold_lift_verdict(scope, close, plan_z)
    zones = buy_zones(cfg, rows, close)
    px = f"최근 종가 {close:,.0f}원({rows[-1]['day']})" if close is not None else "종가 없음"
    checklist = (
        f"1) 공식 공시 여부: {hit['src']} / {hit['datetime']} / {hit['title'][:80]}. "
        f"2) 해제 범위: {scope}. "
        f"3) 가격 반응: {px}. "
        f"4) 물타기 금지 해제 점검 결과: {label}."
    )
    title = f"{cfg['title_prefix']} {hl['title']}"
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: {hl['what']}",
        f"지금 결과/상태: {checklist} 공시/자료: {hit['link']}. {pos_line(cfg, close)} "
        f"약물: {hl.get('drug_note', '오파칼림(Opakalim)')}",
        f"지금 할 일: {action} 추가 매수 구간(검토용): " + " / ".join(zones),
        "나중에: 같은 공시 번호는 다시 보내지 않습니다. 종가 선(68,000 / 83,400 / 91,600 / 평단) 감시와 일반 공시 감시는 계속합니다. "
        "딜 클로징 전 단계면 계약 종결 공시도 따로 확인하세요.",
        "익절/줄일 때: 손절선 68,000원 종가 이탈 때 15주(절반) 매도 검토. 최대 비중 3%(약 600만원). 해제 공시만으로 비중 한도를 넘기지 마세요.",
        "하지 말 것: 이 알림을 자동 매수 신호로 보기, 제목만 보고 시장가 추격, 보류 해제 확인 전 물타기, 신용 및 미수. 투자 권유 아님.",
    ])
    msg = engine.with_footer(cfg, msg)
    engine.clean(title, msg)
    return title, msg, int(hl.get("priority", 5))


def hold_lift_step(cfg: dict, st: dict, now: datetime, io: dict, fails: list[str], rows: list[dict], engine, pos_line) -> list[tuple]:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    hit = find_hold_lift(cfg, st, io, fails, engine)
    if not hit:
        return []
    t, m, p = build_hold_lift(cfg, hit, rows, stamp, engine, pos_line)
    return [(t, m, p, ["syringe"])]
