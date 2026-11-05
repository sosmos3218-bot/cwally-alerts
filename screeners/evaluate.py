"""한 종목 판정. 네트워크를 쓰지 않는다.

트루엔과 기요하라는 같은 조정 순현금을 쓰고 기준 숫자만 다르다.
가치함정 표시(매출, 영업이익 감소, 주주환원, 최대주주, 금융수익)는 통과를 막지 않는다.
비지배지분이 큰데 별도 순현금이 없으면 연결 숫자를 지배주주 현금으로 보지 않아 통과하지 못한다.
"""

from __future__ import annotations

import re

from screeners.fundamentals import (
    RANK,
    build_income,
    fin_distorts,
    pbr_of,
    per_of,
    profitable,
    yoy_down,
)
from screeners.money import fmt_eok, parse_ratio
from screeners.net_cash import ScopeCash, compute_scope

BURN_RE = re.compile(r"(주식|자기주식).{0,8}소각|소각.{0,8}(결정|주식)")


def evaluate_company(payload: dict, config: dict) -> dict:
    code = str(payload.get("code") or "").zfill(6)
    name = str(payload.get("name") or "")
    market = str(payload.get("market") or "")
    mcap = payload.get("market_cap_krw")
    mcap = int(mcap) if mcap is not None else None
    statements = list(payload.get("statements") or [])
    # 재무상태는 최신 보고서만. 직전 연간을 섞으면 현금과 자본이 두 번 나오거나 예전 숫자가 남는다.
    latest_rows = _latest_rows(statements)
    cfs = compute_scope(latest_rows, "CFS")
    ofs = compute_scope(latest_rows, "OFS")
    income = build_income(statements)
    headline = cfs if cfs.present else ofs
    screen_scope, screen_cash, blocked = _screen_cash(cfs, ofs, config)
    equity = _equity_for_pbr(cfs, ofs, screen_scope)
    per = per_of(mcap, income)
    pbr = pbr_of(mcap, equity)
    # 실조회는 시세 PER, PBR 로 이미 걸렀다. 반기 한 장만 받아도 그 값을 판정에 쓴다.
    if payload.get("listing_per") is not None:
        per = float(payload["listing_per"])
        income = {**income, "per_basis": "listing"}
    if payload.get("listing_pbr") is not None:
        pbr = float(payload["listing_pbr"])
    profit = profitable(income)
    shareholder = _shareholder(payload, income)
    flags = _flags(headline, cfs, income, shareholder, config)
    fails = _fails(
        name=name,
        mcap=mcap,
        config=config,
        per=per,
        pbr=pbr,
        profit=profit,
        income=income,
        screen_cash=screen_cash,
        blocked=blocked,
        cfs=cfs,
        ofs=ofs,
    )
    ratio = None
    if screen_cash is not None and mcap:
        ratio = screen_cash / mcap
    cfs_ratio = None
    if cfs.present and mcap:
        cfs_ratio = cfs.adjusted_krw / mcap
    scaled = None
    if cfs.present and cfs.nci_ratio is not None:
        scaled = int(round(cfs.adjusted_krw * (1 - cfs.nci_ratio)))
    return {
        "code": code,
        "name": name,
        "market": market,
        "passes": not fails,
        "fail_reasons": fails,
        "flags": flags,
        "market_cap_krw": mcap,
        "per": None if per is None else round(per, 4),
        "pbr": None if pbr is None else round(pbr, 4),
        "per_basis": income.get("per_basis"),
        "profit_basis": income.get("basis"),
        "net_cash": {
            "naive_krw": headline.naive_krw if headline.present else None,
            "adjusted_krw": headline.adjusted_krw if headline.present else None,
            "screen_krw": screen_cash,
            "screen_scope": screen_scope,
            "screen_blocked": blocked,
            "ratio": None if ratio is None else round(ratio, 4),
            "cfs_ratio": None if cfs_ratio is None else round(cfs_ratio, 4),
            "lease_liability_krw": headline.lease_liability_krw if headline.present else 0,
            "nci_krw": cfs.nci_krw if cfs.present else (ofs.nci_krw if ofs.present else 0),
            "nci_ratio": _nci_ratio(cfs, ofs),
            "scaled_cfs_krw": scaled,
            "adjustments": [item.as_dict() for item in (headline.adjustments if headline.present else [])],
            "bridge": headline.bridge if headline.present else "",
            "cfs": cfs.as_dict(),
            "ofs": ofs.as_dict(),
        },
        "income": {
            "label": income.get("label"),
            "yoy_label": income.get("yoy_label"),
            "revenue_krw": income.get("revenue"),
            "revenue_prior_krw": income.get("revenue_prior"),
            "op_income_krw": income.get("op"),
            "op_prior_krw": income.get("op_prior"),
            "net_income_krw": income.get("ni"),
            "fin_income_krw": income.get("yoy_fin"),
            "basis": income.get("basis"),
        },
        "shareholder": shareholder,
    }


def _latest_rows(statements: list[dict]) -> list[dict]:
    ranked = []
    for item in statements:
        code = str(item.get("reprt_code") or "")
        if code not in RANK:
            continue
        try:
            year = int(item["year"])
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((year, RANK[code], item.get("rows") or []))
    if not ranked:
        return []
    ranked.sort(key=lambda item: (item[0], item[1]))
    return list(ranked[-1][2])


def _nci_ratio(cfs: ScopeCash, ofs: ScopeCash) -> float | None:
    if cfs.present and cfs.nci_ratio is not None:
        return round(cfs.nci_ratio, 4)
    if ofs.present and ofs.nci_ratio is not None:
        return round(ofs.nci_ratio, 4)
    return None


def _equity_for_pbr(cfs: ScopeCash, ofs: ScopeCash, screen_scope: str | None) -> int | None:
    if screen_scope == "OFS" and ofs.present and ofs.equity_parent_krw:
        return ofs.equity_parent_krw
    if cfs.present and cfs.equity_parent_krw:
        return cfs.equity_parent_krw
    if ofs.present:
        return ofs.equity_parent_krw
    return None


def _screen_cash(cfs: ScopeCash, ofs: ScopeCash, config: dict) -> tuple[str | None, int | None, bool]:
    """별도가 있으면 별도 조정 순현금. 없으면 연결. 비지배 비중이 크면 연결은 막는다."""
    if ofs.present:
        return "OFS", ofs.adjusted_krw, False
    if not cfs.present:
        return None, None, False
    ratio = cfs.nci_ratio
    threshold = float(config.get("nci_ratio_flag") or 0.2)
    if config.get("block_consolidated_if_nci", True) and ratio is not None and ratio >= threshold:
        return "CFS", cfs.adjusted_krw, True
    return "CFS", cfs.adjusted_krw, False


def _fails(
    name: str,
    mcap: int | None,
    config: dict,
    per: float | None,
    pbr: float | None,
    profit: bool | None,
    income: dict,
    screen_cash: int | None,
    blocked: bool,
    cfs: ScopeCash,
    ofs: ScopeCash,
) -> list[str]:
    fails: list[str] = []
    compact = "".join(name.split())
    for word in config.get("exclude_financial_keywords") or []:
        if word and word in compact:
            fails.append(f"금융업({word})은 순현금 기준에서 제외")
            return fails
    if mcap is None or mcap <= 0:
        fails.append("시가총액이 없음")
        return fails
    low = config.get("mcap_min_krw") or 0
    high = config.get("mcap_max_krw")
    if mcap < low or (high is not None and mcap > high):
        fails.append(
            f"시가총액 {fmt_eok(mcap)}이 소형주 범위 {fmt_eok(low)}~"
            f"{'없음' if high is None else fmt_eok(high)} 밖"
        )
    if profit is None:
        fails.append("영업이익 또는 순이익을 확인할 수 없어 흑자 판정을 못 함")
    elif not profit:
        fails.append("영업이익과 순이익이 모두 흑자가 아님")
    per_max = float(config["per_max"])
    pbr_max = float(config["pbr_max"])
    if per is None:
        basis = income.get("per_basis")
        if basis == "ytd":
            fails.append("PER을 계산할 수 없음. 누적 순이익만 있어 연환산하지 않음")
        else:
            fails.append("PER을 계산할 수 없음")
    elif per > per_max:
        fails.append(f"PER {per:.2f}, 기준 {per_max:g} 초과")
    if pbr is None:
        fails.append("PBR을 계산할 수 없음")
    elif pbr > pbr_max:
        fails.append(f"PBR {pbr:.2f}, 기준 {pbr_max:g} 초과")
    minimum = float(config["net_cash_to_mcap_min"])
    if screen_cash is None:
        fails.append("순현금 재료가 없음")
    else:
        ratio = screen_cash / mcap
        scope = "별도" if ofs.present else "연결"
        if ratio < minimum:
            fails.append(
                f"조정 순현금/시총 {ratio:.2f} ({scope} {fmt_eok(screen_cash)} / {fmt_eok(mcap)}), "
                f"기준 {minimum:.2f} 미만"
            )
    if blocked:
        ratio = cfs.nci_ratio or 0
        fails.append(
            f"비지배지분 비중 {ratio:.0%}라 연결 조정 순현금을 지배주주 현금으로 쓰지 않음"
        )
    return fails


def _flags(headline: ScopeCash, cfs: ScopeCash, income: dict, shareholder: dict, config: dict) -> list[dict]:
    flags: list[dict] = []
    rev = yoy_down(income.get("yoy_revenue"), income.get("yoy_revenue_prior"))
    op = yoy_down(income.get("yoy_op"), income.get("yoy_op_prior"))
    label = income.get("yoy_label") or ""
    if rev is True:
        flags.append({"id": "revenue_yoy_down", "text": f"{label} 매출이 전년보다 줄었음"})
    if op is True:
        flags.append({"id": "op_income_yoy_down", "text": f"{label} 영업이익이 전년보다 줄었음"})
    executed = shareholder.get("return_executed")
    if executed is False:
        flags.append({"id": "no_shareholder_return", "text": "배당이나 자사주 소각 실행이 확인되지 않음"})
    elif executed is None and not shareholder.get("extras_skipped"):
        flags.append({"id": "shareholder_return_unknown", "text": "주주환원 확인 불가"})
    distort = fin_distorts(
        income.get("yoy_fin"),
        income.get("yoy_fin_op"),
        float(config.get("fin_income_to_op_flag") or 1),
    )
    if distort is True:
        flags.append(
            {
                "id": "financial_income",
                "text": (
                    f"금융수익 {fmt_eok(income.get('yoy_fin'))}이 영업이익 "
                    f"{fmt_eok(income.get('yoy_fin_op'))} 대비 커서 PER가 낮아 보일 수 있음"
                ),
            }
        )
    stake = shareholder.get("largest_pct")
    if stake is None and shareholder.get("stake_known") is False:
        flags.append({"id": "stake_unknown", "text": "최대주주 지분 확인 불가"})
    elif stake is not None and stake < float(config.get("low_stake_pct") or 10):
        flags.append({"id": "low_largest_shareholder", "text": f"최대주주등 지분 {stake:.1f}%로 낮음"})
    nci_ratio = cfs.nci_ratio
    threshold = float(config.get("nci_ratio_flag") or 0.2)
    if nci_ratio is not None and nci_ratio >= threshold:
        flags.append(
            {
                "id": "nci",
                "text": f"비지배지분 {fmt_eok(cfs.nci_krw)}, 자본 대비 {nci_ratio:.0%}. 연결 현금을 지배주주 현금으로 보면 안 됨",
            }
        )
    for item in headline.adjustments:
        if item.id == "customer_deposits":
            flags.append({"id": "customer_deposits", "text": item.reason})
        elif item.id == "settlement_receivable":
            flags.append({"id": "settlement_receivable", "text": item.reason})
        elif item.id == "exchangeable_bond":
            flags.append({"id": "exchangeable_bond", "text": item.reason})
        elif item.id == "lease_liability":
            flags.append({"id": "lease_liability", "text": item.reason})
    return flags


def _shareholder(payload: dict, income: dict) -> dict:
    if payload.get("extras") == "skipped":
        return {
            "dividend_per_share": None,
            "burn_titles": [],
            "return_executed": None,
            "largest_name": None,
            "largest_pct": None,
            "stake_note": None,
            "stake_known": True,
            "extras_skipped": True,
        }
    dividend_rows = payload.get("dividend_rows")
    disclosures = payload.get("disclosures")
    holders = payload.get("shareholders")
    dps = _dps(dividend_rows) if dividend_rows is not None else None
    burns = _burns(disclosures) if disclosures is not None else None
    cf = income.get("dividend_cf")
    if dividend_rows is None and disclosures is None and cf is None:
        executed = None
    else:
        executed = bool((dps or 0) > 0 or burns or (cf not in (None, 0)))
    stake_known = holders is not None
    stake = _stake(holders or []) if stake_known else None
    return {
        "dividend_per_share": dps,
        "burn_titles": burns or [],
        "return_executed": executed,
        "largest_name": None if stake is None else stake.get("name"),
        "largest_pct": None if stake is None else stake.get("pct"),
        "stake_note": None if stake is None else stake.get("note"),
        "stake_known": stake_known,
        "extras_skipped": False,
    }


def _dps(rows: list[dict]) -> float | None:
    found = None
    for row in rows:
        se = "".join(str(row.get("se") or "").split())
        kind = str(row.get("stock_knd") or "")
        if "우선" in kind:
            continue
        if "주당" in se and "현금" in se:
            value = parse_ratio(row.get("thstrm"))
            if value is not None:
                found = value
    return found


def _burns(rows: list[dict] | None) -> list[str]:
    if not rows:
        return []
    titles = []
    for row in rows:
        name = str(row.get("report_nm") or row.get("title") or "")
        if BURN_RE.search(name):
            titles.append(name)
    return titles


def _stake(rows: list[dict]) -> dict | None:
    parsed = []
    for row in rows:
        kind = str(row.get("stock_knd") or "")
        if "우선" in kind:
            continue
        rate = parse_ratio(row.get("trmend_posesn_stock_qota_rt"))
        if rate is None:
            continue
        parsed.append((str(row.get("nm") or ""), rate))
    if not parsed:
        return None
    total = sum(rate for _, rate in parsed)
    top_name, top_rate = max(parsed, key=lambda item: item[1])
    if total > 100.0001:
        return {"name": top_name, "pct": round(top_rate, 4), "note": "지분율 합이 100을 넘어 가장 큰 한 줄만 씀"}
    return {"name": top_name, "pct": round(total, 4), "note": "최대주주와 특수관계인 지분 합"}
