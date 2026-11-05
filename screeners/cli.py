"""수동 실행.

  python -m screeners selftest
  python -m screeners truen --demo
  python -m screeners kiyohara --codes 005710,214180,036530
  python -m screeners truen --limit 30
  python -m screeners kiyohara --all

--demo 와 selftest 는 네트워크와 키를 쓰지 않는다.
실데이터는 OPENDART_API_KEY 가 있을 때만 받는다. 키 값은 출력하지 않는다.
스케줄과 ntfy 알림은 없다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from screeners.money import fmt_eok
from screeners.run import load_fixture, run_live, screen_fixtures, write_output, _asof

EXPECTED = {
    "truen": {
        "005710": True,
        "214180": False,
        "036530": False,
        "079960": False,
        "417790": True,
        "900530": True,
    },
    "kiyohara": {
        "005710": True,
        "214180": False,
        "036530": False,
        "079960": False,
        "417790": False,
        "900530": True,
    },
}


def selftest() -> int:
    problems = []
    cached = {kind: {row["code"]: row for row in screen_fixtures(kind)} for kind in EXPECTED}
    for kind, table in EXPECTED.items():
        rows = cached[kind]
        for code, want in table.items():
            got = rows[code]["passes"]
            if got != want:
                reasons = "; ".join(rows[code].get("fail_reasons") or [])
                problems.append(f"{kind} {code} 통과 {got}, 기대 {want}. {reasons}")
    hecto = cached["truen"]["214180"]
    net = hecto["net_cash"]
    mcap = hecto["market_cap_krw"]
    if not (net["naive_krw"] >= mcap > net["adjusted_krw"]):
        problems.append(
            f"헥토 단순 {fmt_eok(net['naive_krw'])}, 조정 {fmt_eok(net['adjusted_krw'])}, 시총 {fmt_eok(mcap)}"
        )
    ids = {item["id"] for item in net["adjustments"]}
    for needed in ("customer_deposits", "settlement_receivable"):
        if needed not in ids:
            problems.append(f"헥토 조정에 {needed} 없음")
    snt = cached["truen"]["036530"]
    snt_ids = {item["id"] for item in snt["flags"]}
    if "exchangeable_bond" not in snt_ids or "nci" not in snt_ids:
        problems.append(f"SNT 표시 부족: {sorted(snt_ids)}")
    daewon = cached["kiyohara"]["005710"]
    if not daewon["passes"]:
        problems.append("대원산업이 기요하라를 통과하지 못함")
    if problems:
        print("SELFTEST FAIL")
        for line in problems:
            print(line)
        return 1
    print(f"SELFTEST OK 픽스처 {len(EXPECTED['truen'])}종목, 트루엔과 기요하라")
    print(f"헥토 단순 {fmt_eok(net['naive_krw'])}, 조정 {fmt_eok(net['adjusted_krw'])} (리스는 빼지 않음)")
    return 0


def _codes(text: str | None) -> list[str] | None:
    if not text:
        return None
    return [part.strip() for part in text.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screeners")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    for name in ("truen", "kiyohara"):
        command = sub.add_parser(name)
        command.add_argument("--demo", action="store_true", help="픽스처만 돌린다. 네트워크 없음")
        command.add_argument("--codes", help="종목코드 쉼표. 예: 417790,005710")
        command.add_argument("--limit", type=int, help="DART 로 볼 최대 종목 수. 비우면 설정의 dart_name_cap")
        command.add_argument("--all", action="store_true", help="시세 조건을 통과한 종목. 기본 상한 300")
        command.add_argument("--out", help="JSON, 마크다운 폴더")
        command.add_argument("--as-of", help="YYYY-MM-DD")
        command.add_argument("--config", help="기준 JSON 경로")
    args = parser.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "output"
    if args.demo:
        rows = screen_fixtures(args.cmd)
        write_output(args.cmd, rows, out)
        passed = [row["code"] for row in rows if row["passes"]]
        print("DEMO", args.cmd, "통과", ",".join(passed) or "없음")
        return 0
    config_path = Path(args.config) if args.config else None
    run_live(
        args.cmd,
        _codes(args.codes),
        args.limit,
        args.all,
        out,
        _asof(args.as_of),
        config_path,
    )
    return 0


__all__ = ["main"]
