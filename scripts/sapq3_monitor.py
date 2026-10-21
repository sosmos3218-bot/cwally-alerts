#!/usr/bin/env python3
"""SAP Q3 2026 oneshot: fetch real CCB/margin/guidance, re-evaluate conditional buy and zones.
Year-guarded. Dry mode never publishes or writes state.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Year guard
if datetime.now(ZoneInfo("Asia/Seoul")).year != 2026:
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ntfy_util import publish, resolve_server, resolve_topic
except ImportError:
    publish = None  # type: ignore

CONFIG_PATH = ROOT / "configs" / "sapq3.json"
STATE_PATH = ROOT / "state" / "sapq3.json"

IR_URL = "https://www.sap.com/investors/en/financial-documents-and-events/recent-results.html"
Q3_PDF_HINTS = [
    "https://www.sap.com/docs/download/investors/2026/sap-2026-q3-statement.pdf",
    "https://www.sap.com/investors/en.html",
]

THRESHOLDS = {
    "ccb_cc_min": 24.0,          # % positive
    "ccb_cc_warn": 22.0,         # % warning
    "op_margin_min": 29.0,       # %
    "fcf_min_bn": 9.5,           # € billion
}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def is_dry() -> bool:
    return os.environ.get("SAPQ3_MODE", "auto").lower() == "dry"


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; cwally-alerts)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_metrics(html: str) -> dict:
    """Best-effort regex extraction from IR HTML or PDF text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    metrics = {}

    # Current cloud backlog
    m = re.search(r"current cloud backlog[^0-9]*([0-9.,]+)\s*(billion|bn|€)?[^0-9]*up\s+([0-9.]+)\s*%[^0-9]*([0-9.]+)\s*%\s*at constant", text, re.I)
    if m:
        metrics["ccb_bn"] = float(m.group(1).replace(",", ""))
        metrics["ccb_growth"] = float(m.group(3))
        metrics["ccb_cc"] = float(m.group(4))
    else:
        m = re.search(r"current cloud backlog.*?([0-9.]+)\s*%\s*at constant currencies", text, re.I)
        if m:
            metrics["ccb_cc"] = float(m.group(1))

    # Cloud revenue
    m = re.search(r"cloud revenue[^0-9]*([0-9.,]+).*?up\s+([0-9.]+)\s*%[^0-9]*([0-9.]+)\s*%\s*at constant", text, re.I)
    if m:
        metrics["cloud_bn"] = float(m.group(1).replace(",", ""))
        metrics["cloud_growth"] = float(m.group(2))
        metrics["cloud_cc"] = float(m.group(3))

    # Non-IFRS operating profit / margin
    m = re.search(r"non-IFRS operating profit[^0-9]*([0-9.,]+).*?up\s+([0-9.]+)\s*%", text, re.I)
    if m:
        metrics["op_bn"] = float(m.group(1).replace(",", ""))
        metrics["op_growth"] = float(m.group(2))
    m = re.search(r"operating margin.*?([0-9.]+)\s*%", text, re.I)
    if m:
        metrics["op_margin"] = float(m.group(1))

    # FCF outlook
    m = re.search(r"free cash flow.*?([0-9.]+)\s*billion", text, re.I)
    if m:
        metrics["fcf_bn"] = float(m.group(1))

    # Guidance snippets
    if re.search(r"continues to expect|outlook unchanged|maintains", text, re.I):
        metrics["guidance_maintained"] = True
    if re.search(r"lowers|cuts|reduces|updated to reflect", text, re.I):
        metrics["guidance_updated"] = True

    return metrics


def evaluate(metrics: dict) -> tuple[str, str, list[str]]:
    """Return (opinion, zone_note, reasons)."""
    reasons = []
    ccb = metrics.get("ccb_cc")
    margin = metrics.get("op_margin")
    fcf = metrics.get("fcf_bn")

    positive = 0
    warnings = 0

    if ccb is not None:
        if ccb >= THRESHOLDS["ccb_cc_min"]:
            reasons.append(f"CCB 고정환율 +{ccb:.1f}% (기준 {THRESHOLDS['ccb_cc_min']}% 이상) 유지")
            positive += 1
        elif ccb >= THRESHOLDS["ccb_cc_warn"]:
            reasons.append(f"CCB 고정환율 +{ccb:.1f}% (경고선 {THRESHOLDS['ccb_cc_warn']}% 부근)")
            warnings += 1
        else:
            reasons.append(f"CCB 고정환율 +{ccb:.1f}% (기준 미달)")
            warnings += 2
    else:
        reasons.append("CCB 수치 추출 실패")

    if margin is not None:
        if margin >= THRESHOLDS["op_margin_min"]:
            reasons.append(f"Non-IFRS 영업이익률 {margin:.1f}% (기준 이상)")
            positive += 1
        else:
            reasons.append(f"Non-IFRS 영업이익률 {margin:.1f}% (기준 미달)")
            warnings += 1
    else:
        reasons.append("마진 수치 추출 실패")

    if fcf is not None:
        if fcf >= THRESHOLDS["fcf_min_bn"]:
            reasons.append(f"FCF 전망 약 €{fcf:.1f}B 유지")
            positive += 1
        else:
            reasons.append(f"FCF 전망 €{fcf:.1f}B (하향)")
            warnings += 1
    else:
        reasons.append("FCF 가이던스 추출 실패")

    if positive >= 2 and warnings == 0:
        opinion = "조건부 매수 유지"
        zone = "기존 매수 구간 유지 (210-235 분할, 190 이하 적극)"
    elif warnings >= 2:
        opinion = "조건부 매수 재검토 (비중 축소 또는 하향)"
        zone = "매수 구간을 190-200달러대로 하향 검토"
    else:
        opinion = "조건부 매수 유지 (신중)"
        zone = "기존 구간 유지, 추가 확인 후 소폭 조정 가능"

    return opinion, zone, reasons


def main() -> None:
    cfg = load_config()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    prefix = cfg.get("title_prefix", "[SAPQ3]")

    metrics = {}
    source = "없음"
    for url in [IR_URL] + Q3_PDF_HINTS:
        try:
            raw = fetch_text(url)
            metrics = extract_metrics(raw)
            if metrics:
                source = url
                break
        except Exception as e:
            continue

    opinion, zone_note, reasons = evaluate(metrics)

    title = f"{prefix} Q3 실적 후 {opinion} | 매매 신호 아님"
    # Relative comparison (static baselines; update if needed post-results)
    oracle_note = "Oracle Fusion Cloud ERP: 최근 클라우드 ERP 성장 ~15-18% (NetSuite 포함, AI/에이전트 가속). 전체 클라우드 성장은 인프라 비중 높음."
    ms_note = "Microsoft Dynamics 365: 클라우드 ERP/CRM 성장 중상위, Azure·Copilot·MCP 통합 강점. 미드마켓·워크로드 확장 우위, 엔터프라이즈 대형 딜은 SAP·Oracle과 경쟁."
    relative = (
        "상대 매력: SAP는 CCB·마진 안정성 + 엔터프라이즈 전환(RISE) 강점. "
        "Oracle은 AI/에이전트·클라우드 성장 속도, Microsoft는 에코시스템·미드마켓 침투에서 우위. "
        "SAP CCB 고정환율 24%+ 유지 시 상대 매력 유지, 22% 미만 시 Oracle/MS 상대 우위 확대 가능."
    )

    body_lines = [
        "이게 뭐 알림: SAP Q3 2026 실적 발표 후 실제 CCB·마진·가이던스 수치로 조건부 매수 의견과 매수 구간을 재평가하고, Oracle·Microsoft Dynamics와의 성장 비교로 상대 투자 매력을 업데이트하는 일회성 알림입니다.",
        f"지금 결과/상태: 출처 {source}. " + " / ".join(reasons) + f". 추출 수치: {json.dumps(metrics, ensure_ascii=False)}",
        f"성장 비교: {oracle_note} {ms_note}",
        f"상대 재평가: {relative}",
        f"지금 할 일: {opinion}. {zone_note}.",
        "나중에: 2027년 성장 가속 코멘트와 AI 수익화 지표, Oracle/MS 실적과의 직접 비교를 추가 확인합니다.",
        "익절/줄일 때: 기준 가치 260달러 근접 시 신규 매수 비중 축소. 270달러 이상이면 확대 중단.",
        "하지 말 것: 실적 직후 추격매수, 단일 지표만 보고 의견 전면 변경, 레버리지 사용.",
    ]
    body = "\n".join(body_lines)

    if is_dry():
        print("DRY RUN")
        print(f"Title: {title}")
        print(f"Body:\n{body}")
        print("No ntfy sent, no state written.")
        return

    if publish is None:
        print(f"Would publish (ntfy_util missing): {title}")
        print(body)
        return

    publish(
        title=title,
        message=body,
        topic=resolve_topic("port"),
        server=resolve_server(),
    )
    # Optional state mark
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {"updated_at": now.isoformat(timespec="seconds"), "metrics": metrics, "opinion": opinion}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
