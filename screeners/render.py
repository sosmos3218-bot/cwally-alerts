"""결과 JSON, 마크다운. 가운뎃점과 줄표(em dash)는 쓰지 않는다."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from screeners.money import fmt_eok

KST = ZoneInfo("Asia/Seoul")


def dump_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_markdown(kind: str, rows: list[dict], generated_at: datetime | None = None) -> str:
    now = generated_at or datetime.now(KST)
    passed = [row for row in rows if row.get("passes")]
    lines = [
        f"# {kind} 스크리너",
        "",
        f"만든 시각: {now.strftime('%Y-%m-%d %H:%M')} KST",
        f"검사 {len(rows)}종목, 통과 {len(passed)}종목",
        "",
        "통과는 조정 순현금 비율, 흑자, PER, PBR 기준이다.",
        "매출 감소, 영업이익 감소, 주주환원, 최대주주, 금융수익 표시는 탈락 사유가 아니다.",
        "단순 순현금과 조정 순현금을 같이 적는다.",
        "",
    ]
    if not rows:
        lines.append("결과 없음.")
        lines.append("")
        return "\n".join(lines)
    for row in rows:
        mark = "통과" if row.get("passes") else "탈락"
        net = row.get("net_cash") or {}
        lines.append(f"## {row.get('code')} {row.get('name')} ({row.get('market')}) {mark}")
        lines.append("")
        lines.append(
            f"- 시총 {fmt_eok(row.get('market_cap_krw'))}, "
            f"PER {_num(row.get('per'))}, PBR {_num(row.get('pbr'))}"
        )
        lines.append(
            f"- 단순 순현금 {fmt_eok(net.get('naive_krw'))}, "
            f"조정 순현금 {fmt_eok(net.get('adjusted_krw'))}, "
            f"판정에 쓴 값 {fmt_eok(net.get('screen_krw'))} ({_scope(net.get('screen_scope'))})"
        )
        if net.get("ratio") is not None:
            lines.append(f"- 판정 비율 {net.get('ratio')}")
        if net.get("scaled_cfs_krw") is not None:
            lines.append(f"- 연결 조정 순현금에 지배 지분율을 곱한 참고값 {fmt_eok(net.get('scaled_cfs_krw'))}")
        if net.get("bridge"):
            lines.append(f"- {net.get('bridge')}")
        for item in net.get("adjustments") or []:
            lines.append(f"- 조정 {item.get('id')}: {item.get('reason')}")
        income = row.get("income") or {}
        lines.append(
            f"- 손익({income.get('label')}): 순이익 {fmt_eok(income.get('net_income_krw'))}, "
            f"매출 {fmt_eok(income.get('revenue_krw'))}, 영업이익 {fmt_eok(income.get('op_income_krw'))}"
        )
        holder = row.get("shareholder") or {}
        if holder.get("largest_pct") is not None:
            lines.append(f"- 최대주주등 {holder.get('largest_name')} {holder.get('largest_pct')}%")
        if holder.get("return_executed") is True:
            lines.append("- 주주환원 실행 확인")
        elif holder.get("return_executed") is False:
            lines.append("- 주주환원 실행 확인 안 됨")
        for flag in row.get("flags") or []:
            lines.append(f"- 표시: {flag.get('text')}")
        for reason in row.get("fail_reasons") or []:
            lines.append(f"- 탈락: {reason}")
        lines.append("")
    return "\n".join(lines)


def render_brief(kind: str, payload: dict, limit: int = 15) -> str:
    """GitHub 단계 요약용. 통과 종목만, 길이를 제한한다."""
    rows = list(payload.get("rows") or [])
    passed = [row for row in rows if row.get("passes")]
    count = payload.get("count", len(rows))
    n_pass = payload.get("passes", len(passed))
    lines = [
        f"## {kind}",
        "",
        f"검사 {payload.get('checked', count)}종목, 통과 {n_pass}종목, 건너뜀 {payload.get('skipped', 0)}종목.",
        "",
    ]
    if payload.get("dart_calls") is not None:
        lines.append(
            f"OpenDART 호출 {payload.get('dart_calls')}회, 캐시 {payload.get('dart_cache_hits') or 0}회."
        )
        lines.append("")
    if payload.get("budget_note"):
        lines.append(str(payload.get("budget_note")))
        lines.append("")
    reasons: dict[str, int] = {}
    for item in payload.get("skipped_rows") or []:
        reason = str(item.get("reason") or "이유 없음")
        reasons[reason] = reasons.get(reason, 0) + 1
    if reasons:
        lines.append("건너뜀 이유:")
        for reason, n in sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))[:8]:
            lines.append(f"- {reason}: {n}")
        lines.append("")
    if not passed:
        lines.append("통과 종목 없음.")
        lines.append("")
        return "\n".join(lines)
    show = passed[:limit]
    lines.append("통과 종목. 비율은 판정에 쓴 조정 순현금 / 시총.")
    lines.append("")
    lines.append("| 코드 | 이름 | 비율 | 단순 순현금 | 조정 순현금 | PER | PBR |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in show:
        net = row.get("net_cash") or {}
        lines.append(
            f"| {row.get('code')} | {row.get('name')} | {_num(net.get('ratio'))} | "
            f"{fmt_eok(net.get('naive_krw'))} | {fmt_eok(net.get('adjusted_krw'))} | "
            f"{_num(row.get('per'))} | {_num(row.get('pbr'))} |"
        )
    lines.append("")
    if len(passed) > limit:
        lines.append(f"나머지 {len(passed) - limit}개는 JSON 아티팩트에 있다.")
        lines.append("")
    return "\n".join(lines)


def brief_from_dir(out_dir, kinds: list[str], limit: int = 15) -> str:
    """출력 폴더의 JSON 을 읽어 짧은 마크다운을 만든다. 없는 파일은 결과 없음으로 적는다."""
    from pathlib import Path

    folder = Path(out_dir)
    lines = ["# 스크리너 결과", ""]
    for kind in kinds:
        path = folder / f"{kind}.json"
        if not path.is_file():
            lines.append(f"## {kind}")
            lines.append("")
            lines.append("결과 파일 없음.")
            lines.append("")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        lines.append(render_brief(kind, payload, limit=limit).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _num(value) -> str:
    if value is None:
        return "없음"
    return f"{value:.2f}"


def _scope(scope: str | None) -> str:
    if scope == "OFS":
        return "별도"
    if scope == "CFS":
        return "연결"
    return "없음"
