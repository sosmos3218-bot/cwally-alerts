"""Regenerated note for one screen run. Screening records only."""

from __future__ import annotations


def _fmt(value) -> str:
    if value is None or isinstance(value, bool):
        return "" if value is None else str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value) >= 1 and abs(value - round(value)) < 1e-4:
            return str(int(round(value)))
        if abs(value) >= 1:
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _reason_counts(excluded: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in excluded:
        reason = str(row.get("reason") or "")
        counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _lookup(screened: dict, ticker: str) -> dict | None:
    for row in screened.get("magic") or []:
        if row.get("ticker") == ticker:
            return {"where": "magic_formula", **row}
    for row in screened.get("excluded") or []:
        if row.get("ticker") == ticker:
            return {"where": "excluded", **row}
    return None


def render_verification(meta: dict, screened: dict, generated_at: str) -> str:
    """Markdown note stamped with this run. Replaces the frozen sample."""
    lines = [
        "# US quant screen verification",
        "",
        f"Trade date `{meta.get('trade_date')}`. Run date `{meta.get('run_date')}`. Generated at `{generated_at}`.",
        f"Code `{meta.get('code_version')}`. Previous trade date for diff `{meta.get('previous_trade_date')}`.",
        "",
        "This file is rewritten by every screen run. The rows are rule-based screening records.",
        "",
        "The weekday schedule is GitHub cron `30 22 * * 0-4` (07:30 Asia/Seoul). GitHub may start that job 4 to 6 hours later. This screen does not add an external scheduler.",
        "",
        "## Magic Formula top rows",
        "",
        "| combined_rank | ticker | name | shares | shares_end | shares_source | shares_kind | market_cap | enterprise_value | earnings_yield |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in screened.get("magic") or []:
        lines.append(
            "| {combined_rank} | {ticker} | {name} | {shares} | {shares_end} | {shares_source} | {shares_kind} | {market_cap} | {enterprise_value} | {earnings_yield} |".format(
                combined_rank=_fmt(row.get("combined_rank")),
                ticker=row.get("ticker") or "",
                name=(row.get("name") or "").replace("|", "/"),
                shares=_fmt(row.get("shares")),
                shares_end=_fmt(row.get("shares_end")),
                shares_source=row.get("shares_source") or "",
                shares_kind=row.get("shares_kind") or "",
                market_cap=_fmt(row.get("market_cap")),
                enterprise_value=_fmt(row.get("enterprise_value")),
                earnings_yield=_fmt(row.get("earnings_yield")),
            )
        )
    lines.extend(["", "## Exclusion counts", "", "| reason | count |", "| --- | --- |"])
    for reason, count in _reason_counts(screened.get("excluded") or []):
        lines.append(f"| {reason} | {count} |")
    lines.extend(["", "## Checked names", ""])
    lines.append("MA, TBLA, and CRMD are listed when they are in this run, with the share fields or the exclusion reason.")
    lines.append("")
    for ticker in ("MA", "TBLA", "CRMD"):
        row = _lookup(screened, ticker)
        if row is None:
            lines.append(f"- `{ticker}`: not in the magic list or the excluded list.")
            continue
        if row["where"] == "magic_formula":
            lines.append(
                f"- `{ticker}`: on the magic list. shares `{_fmt(row.get('shares'))}` "
                f"source `{row.get('shares_source')}` end `{row.get('shares_end')}` "
                f"kind `{row.get('shares_kind')}` market_cap `{_fmt(row.get('market_cap'))}` "
                f"enterprise_value `{_fmt(row.get('enterprise_value'))}` "
                f"earnings_yield `{_fmt(row.get('earnings_yield'))}`."
            )
        else:
            lines.append(
                f"- `{ticker}`: excluded. reason `{row.get('reason')}` detail `{row.get('detail')}`."
            )
    lines.extend(
        [
            "",
            "## Form 4",
            "",
            f"Acceptance cutoff `{meta.get('form4_acceptance_cutoff')}`.",
            f"Latest Form 4 filing date `{meta.get('form4_latest_filing_date')}`.",
            f"Latest acceptance `{meta.get('form4_latest_acceptance')}`.",
            "A value of `unknown (master.idx)` means the index rows in this run had no acceptance time.",
            "",
            "## Counts",
            "",
            f"Magic input rows `{((meta.get('counts') or {}).get('magic_input_rows'))}`.",
            f"Magic top `{((meta.get('counts') or {}).get('magic_top'))}`.",
            f"Excluded `{((meta.get('counts') or {}).get('excluded'))}`.",
            f"Universe `{((meta.get('counts') or {}).get('universe'))}`.",
            "",
        ]
    )
    return "\n".join(lines)
