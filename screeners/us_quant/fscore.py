"""Piotroski F-Score on annual figures.

Each signal is 1 or 0. The total is the sum. Definitions follow Piotroski (2000):

1. ROA = net income_t / assets at the prior year end. Point if ROA > 0.
2. CFO = operating cash flow_t / assets at the prior year end. Point if CFO > 0.
3. Point if ROA_t > ROA of the prior year.
4. Point if CFO > ROA (same scaling, so cash flow exceeds net income).
5. Leverage = long-term debt / assets at that year end. Point if leverage fell.
6. Current ratio = current assets / current liabilities. Point if it rose.
7. Point if common-equity issuance proceeds are not positive.
   When the issuance tag is absent, point if common shares did not rise.
8. Gross margin = gross profit / revenue. Point if it rose.
9. Asset turnover = revenue_t / assets at the prior year end. Point if it rose.

Equal values do not score. A missing required input returns an error string
instead of a partial total.
"""

from __future__ import annotations


def _need(inputs: dict, key: str) -> float:
    if key not in inputs or inputs[key] is None:
        raise KeyError(key)
    return float(inputs[key])


def score_f(inputs: dict) -> dict:
    """Score one issuer. `inputs` holds the raw annual numbers, not the ratios."""
    try:
        ni_t = _need(inputs, "ni_t")
        ni_t1 = _need(inputs, "ni_t1")
        assets_t = _need(inputs, "assets_t")
        assets_t1 = _need(inputs, "assets_t1")
        assets_t2 = _need(inputs, "assets_t2")
        cfo_t = _need(inputs, "cfo_t")
        ltd_t = _need(inputs, "ltd_t")
        ltd_t1 = _need(inputs, "ltd_t1")
        ca_t = _need(inputs, "ca_t")
        cl_t = _need(inputs, "cl_t")
        ca_t1 = _need(inputs, "ca_t1")
        cl_t1 = _need(inputs, "cl_t1")
        gp_t = _need(inputs, "gp_t")
        rev_t = _need(inputs, "rev_t")
        gp_t1 = _need(inputs, "gp_t1")
        rev_t1 = _need(inputs, "rev_t1")
    except KeyError as exc:
        return {"ok": False, "reason": f"missing_{exc.args[0]}"}

    if assets_t1 <= 0 or assets_t2 <= 0 or assets_t <= 0:
        return {"ok": False, "reason": "assets_non_positive"}
    if cl_t <= 0 or cl_t1 <= 0:
        return {"ok": False, "reason": "current_liabilities_non_positive"}
    if rev_t <= 0 or rev_t1 <= 0:
        return {"ok": False, "reason": "revenue_non_positive"}

    issuance = inputs.get("issuance_t")
    shares_t = inputs.get("shares_t")
    shares_t1 = inputs.get("shares_t1")
    if issuance is None and (shares_t is None or shares_t1 is None):
        return {"ok": False, "reason": "equity_issuance_unknown"}

    roa_t = ni_t / assets_t1
    roa_t1 = ni_t1 / assets_t2
    cfo_ratio = cfo_t / assets_t1
    lever_t = ltd_t / assets_t
    lever_t1 = ltd_t1 / assets_t1
    current_t = ca_t / cl_t
    current_t1 = ca_t1 / cl_t1
    margin_t = gp_t / rev_t
    margin_t1 = gp_t1 / rev_t1
    turn_t = rev_t / assets_t1
    turn_t1 = rev_t1 / assets_t2
    if issuance is not None:
        eq = 1 if float(issuance) <= 0 else 0
        eq_basis = "issuance_proceeds"
    else:
        eq = 1 if float(shares_t) <= float(shares_t1) else 0
        eq_basis = "shares_not_increased"

    flags = {
        "f_roa": 1 if roa_t > 0 else 0,
        "f_cfo": 1 if cfo_ratio > 0 else 0,
        "f_droa": 1 if roa_t > roa_t1 else 0,
        "f_accrual": 1 if cfo_ratio > roa_t else 0,
        "f_dlever": 1 if lever_t < lever_t1 else 0,
        "f_dliquid": 1 if current_t > current_t1 else 0,
        "f_eqoffer": eq,
        "f_dmargin": 1 if margin_t > margin_t1 else 0,
        "f_dturn": 1 if turn_t > turn_t1 else 0,
    }
    return {
        "ok": True,
        "f_score": int(sum(flags.values())),
        "flags": flags,
        "eq_basis": eq_basis,
        "ratios": {
            "roa_t": roa_t,
            "roa_t1": roa_t1,
            "cfo_t": cfo_ratio,
            "lever_t": lever_t,
            "lever_t1": lever_t1,
            "current_t": current_t,
            "current_t1": current_t1,
            "margin_t": margin_t,
            "margin_t1": margin_t1,
            "turn_t": turn_t,
            "turn_t1": turn_t1,
        },
    }
