"""손익, PER, PBR, 전년비, 금융수익 비중.

TTM 은 이번 누적 + 직전 연간 - 전년 같은 기간 누적이다.
TTM 재료가 없으면 최근 연간을 쓰고, 연간도 없으면 누적만으로 흑자 여부만 본다.
PER 은 TTM 또는 연간 지배순이익으로만 계산한다. 분기 누적을 연환산하지 않는다.
"""

from __future__ import annotations

from screeners.rows import has_statement, sum_tag

RANK = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
LABEL = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업보고서"}


def _fs_for_income(rows: list[dict]) -> str | None:
    if has_statement(rows, "CFS") or _has_is(rows, "CFS"):
        return "CFS"
    if has_statement(rows, "OFS") or _has_is(rows, "OFS"):
        return "OFS"
    return None


def _has_is(rows: list[dict], fs: str) -> bool:
    from screeners.rows import _sj, fs_of

    return any(fs_of(row) == fs and _sj(row) in {"IS", "CIS"} for row in rows)


def _pack(rows: list[dict], fs: str, field: str) -> dict:
    owner = sum_tag(rows, "ni_owner", field, fs)
    total = sum_tag(rows, "ni_total", field, fs)
    fin = sum_tag(rows, "fin_income", field, fs)
    if fin is None:
        fin = sum_tag(rows, "fx_gain", field, fs)
    return {
        "revenue": sum_tag(rows, "revenue", field, fs),
        "op": sum_tag(rows, "op_income", field, fs),
        "ni": owner if owner is not None else total,
        "fin": fin,
    }


def _statement_income(rows: list[dict]) -> dict | None:
    fs = _fs_for_income(rows)
    if fs is None:
        return None
    ytd = _pack(rows, fs, "ytd")
    prior = _pack(rows, fs, "ytd_prior")
    return {"fs": fs, "ytd": ytd, "prior": prior}


def _sub(a: int | None, b: int | None, c: int | None) -> int | None:
    if a is None or b is None or c is None:
        return None
    return a + b - c


def build_income(statements: list[dict]) -> dict:
    """statements: [{year, reprt_code, rows}]."""
    usable = []
    for item in statements:
        code = str(item.get("reprt_code") or "")
        if code not in RANK:
            continue
        parsed = _statement_income(item.get("rows") or [])
        if parsed is None:
            continue
        usable.append({**parsed, "year": int(item["year"]), "reprt_code": code, "rows": item.get("rows") or []})
    if not usable:
        return {"basis": "none", "label": "손익 없음"}
    latest = max(usable, key=lambda item: (item["year"], RANK[item["reprt_code"]]))
    annual = None
    if latest["reprt_code"] == "11011":
        annual = latest
    else:
        for item in usable:
            if item["reprt_code"] == "11011" and item["year"] == latest["year"] - 1:
                annual = item
                break
    ttm = None
    if latest["reprt_code"] == "11011":
        ttm = dict(latest["ytd"])
        ttm_label = f"{latest['year']} 연간"
        basis = "annual"
    elif annual is not None:
        ttm = {
            "revenue": _sub(latest["ytd"]["revenue"], annual["ytd"]["revenue"], latest["prior"]["revenue"]),
            "op": _sub(latest["ytd"]["op"], annual["ytd"]["op"], latest["prior"]["op"]),
            "ni": _sub(latest["ytd"]["ni"], annual["ytd"]["ni"], latest["prior"]["ni"]),
            "fin": _sub(latest["ytd"]["fin"], annual["ytd"]["fin"], latest["prior"]["fin"]),
        }
        if ttm["op"] is None or ttm["ni"] is None:
            ttm = None
        ttm_label = f"{latest['year']} {LABEL[latest['reprt_code']]} 기준 TTM"
        basis = "ttm" if ttm else "ytd"
    else:
        ttm_label = ""
        basis = "ytd"

    if basis == "annual":
        current = latest["ytd"]
        prior = latest["prior"]
        label = f"{latest['year']} 연간"
        per_basis = "annual"
    elif basis == "ttm" and ttm is not None:
        current = ttm
        prior = None
        label = ttm_label
        per_basis = "ttm"
    else:
        current = latest["ytd"]
        prior = latest["prior"]
        label = f"{latest['year']} {LABEL[latest['reprt_code']]} 누적"
        per_basis = "ytd"
        basis = "ytd"

    # 전년비는 최신 보고서의 누적(또는 연간) 대 전기 칸. TTM 대 TTM 은 재료가 더 필요하다.
    yoy_current = latest["ytd"]
    yoy_prior = latest["prior"]
    yoy_label = f"{latest['year']} {LABEL[latest['reprt_code']]}"

    return {
        "basis": basis,
        "per_basis": per_basis,
        "label": label,
        "yoy_label": yoy_label,
        "fs": latest["fs"],
        "revenue": current.get("revenue"),
        "op": current.get("op"),
        "ni": current.get("ni"),
        "fin": current.get("fin"),
        "revenue_prior": None if prior is None else prior.get("revenue"),
        "op_prior": None if prior is None else prior.get("op"),
        "yoy_revenue": yoy_current.get("revenue"),
        "yoy_revenue_prior": yoy_prior.get("revenue"),
        "yoy_op": yoy_current.get("op"),
        "yoy_op_prior": yoy_prior.get("op"),
        "yoy_fin": yoy_current.get("fin"),
        "yoy_fin_op": yoy_current.get("op"),
        "dividend_cf": _dividend_cf(latest["rows"], latest["fs"]),
    }


def _dividend_cf(rows: list[dict], fs: str) -> int | None:
    return sum_tag(rows, "dividend_cf", "ytd", fs)


def profitable(income: dict) -> bool | None:
    op = income.get("op")
    ni = income.get("ni")
    if op is None or ni is None:
        return None
    return op > 0 and ni > 0


def per_of(market_cap: int | None, income: dict) -> float | None:
    if not market_cap or income.get("per_basis") == "ytd":
        return None
    ni = income.get("ni")
    if ni is None or ni <= 0:
        return None
    return market_cap / ni


def pbr_of(market_cap: int | None, equity: int | None) -> float | None:
    if not market_cap or equity is None or equity <= 0:
        return None
    return market_cap / equity


def yoy_down(current: int | None, prior: int | None) -> bool | None:
    if current is None or prior is None:
        return None
    return current < prior


def fin_distorts(fin: int | None, op: int | None, threshold: float) -> bool | None:
    """금융수익이 영업이익 대비 threshold 배 이상이면 True. 재료가 없으면 None."""
    if fin is None or op is None:
        return None
    if op <= 0:
        return fin > 0
    return (fin / op) >= threshold
