"""단순 순현금과 조정 순현금.

단순 순현금은 흔히 쓰는 값이다.
  현금및현금성자산 + 단기금융상품 + 유동 당기손익-공정가치 금융자산 + 기타유동금융자산
  + 본계정으로 잡힌 단기미수금
  - 차입금 - 일반사채 - 전환사채 - 신주인수권부사채

조정에서 빼는 것
  예수금(VA, PG 같은 고객 정산 자금)
  기타금융자산 안에 식별되는 정산형 미수금
  단순 값에 안 들어간 교환사채

리스부채는 빼지 않고 금액만 적는다.
비지배지분은 여기서 비율만 계산하고, 통과 여부는 evaluate 가 정한다.
별도 재무제표가 있으면 판정은 별도 조정 순현금을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from screeners.money import fmt_eok
from screeners.rows import has_statement, sum_tag


@dataclass
class Adjustment:
    id: str
    effect_krw: int
    noted_krw: int
    reason: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "effect_krw": self.effect_krw,
            "noted_krw": self.noted_krw,
            "reason": self.reason,
        }


@dataclass
class ScopeCash:
    present: bool
    naive_krw: int = 0
    adjusted_krw: int = 0
    lease_liability_krw: int = 0
    nci_krw: int = 0
    equity_total_krw: int | None = None
    equity_parent_krw: int | None = None
    nci_ratio: float | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    components: dict = field(default_factory=dict)
    bridge: str = ""

    def as_dict(self) -> dict:
        return {
            "present": self.present,
            "naive_krw": self.naive_krw,
            "adjusted_krw": self.adjusted_krw,
            "lease_liability_krw": self.lease_liability_krw,
            "nci_krw": self.nci_krw,
            "equity_total_krw": self.equity_total_krw,
            "equity_parent_krw": self.equity_parent_krw,
            "nci_ratio": self.nci_ratio,
            "adjustments": [item.as_dict() for item in self.adjustments],
            "components": self.components,
            "bridge": self.bridge,
        }


def _nz(value: int | None) -> int:
    return 0 if value is None else value


def compute_scope(rows: list[dict], fs: str) -> ScopeCash:
    if not has_statement(rows, fs):
        return ScopeCash(present=False)
    cash = _nz(sum_tag(rows, "cash", "end", fs))
    st_fin = _nz(sum_tag(rows, "st_fin", "end", fs))
    fvpl = _nz(sum_tag(rows, "fvpl", "end", fs))
    other_fin = _nz(sum_tag(rows, "other_fin", "end", fs))
    settlement = _nz(sum_tag(rows, "settlement", "end", fs))
    # 본계정 미수금만 단순 순현금 자산에 넣는다. 내역은 기타금융자산 안에 이미 들어 있다.
    settlement_main = _settlement_main(rows, fs)
    deposits = _nz(sum_tag(rows, "deposit", "end", fs))
    st_borrow = _nz(sum_tag(rows, "st_borrow", "end", fs))
    lt_borrow = _nz(sum_tag(rows, "lt_borrow", "end", fs))
    current_lt = _nz(sum_tag(rows, "current_lt", "end", fs))
    bond = _nz(sum_tag(rows, "bond", "end", fs))
    convertible = _nz(sum_tag(rows, "convertible", "end", fs))
    exchangeable = _nz(sum_tag(rows, "exchangeable", "end", fs))
    lease = _nz(sum_tag(rows, "lease", "end", fs))
    nci = _nz(sum_tag(rows, "nci", "end", fs))
    equity_total = sum_tag(rows, "equity_total", "end", fs)
    equity_parent = sum_tag(rows, "equity_parent", "end", fs)
    if equity_parent is None and equity_total is not None:
        equity_parent = equity_total - nci

    exchangeable_inside = _exchangeable_inside_bond(rows, fs)
    exchangeable_outside = max(0, exchangeable - exchangeable_inside)

    debt_naive = st_borrow + lt_borrow + current_lt + bond + convertible
    cash_like = cash + st_fin + fvpl + other_fin + settlement_main
    naive = cash_like - debt_naive

    adjustments: list[Adjustment] = []
    effect = 0
    if deposits:
        adjustments.append(
            Adjustment(
                id="customer_deposits",
                effect_krw=-deposits,
                noted_krw=deposits,
                reason=(
                    f"예수금 {fmt_eok(deposits)}은 고객 정산 자금(VA, PG 사업 예수금 포함)이라 "
                    "회사 현금에서 뺌"
                ),
            )
        )
        effect -= deposits
    if settlement:
        adjustments.append(
            Adjustment(
                id="settlement_receivable",
                effect_krw=-settlement,
                noted_krw=settlement,
                reason=(
                    f"단기미수금 등 정산채권 {fmt_eok(settlement)}은 기타금융자산에 섞여 단순 순현금에 "
                    "들어갔으면 제외"
                ),
            )
        )
        effect -= settlement
    if exchangeable_outside:
        adjustments.append(
            Adjustment(
                id="exchangeable_bond",
                effect_krw=-exchangeable_outside,
                noted_krw=exchangeable_outside,
                reason=(
                    f"교환사채 {fmt_eok(exchangeable_outside)}은 단순 순현금의 차입금에 없어서 "
                    "조정에서 뺌"
                ),
            )
        )
        effect -= exchangeable_outside
    elif exchangeable_inside:
        adjustments.append(
            Adjustment(
                id="exchangeable_bond",
                effect_krw=0,
                noted_krw=exchangeable_inside,
                reason=(
                    f"교환사채 {fmt_eok(exchangeable_inside)}은 사채 계정에 이미 포함되어 단순 순현금에서 "
                    "빠졌고, 한 번 더 빼지 않음"
                ),
            )
        )
    if lease:
        adjustments.append(
            Adjustment(
                id="lease_liability",
                effect_krw=0,
                noted_krw=lease,
                reason=f"리스부채 {fmt_eok(lease)}은 차입금과 달라 순현금에서 빼지 않고 적기만 함",
            )
        )

    nci_ratio = None
    if equity_total and equity_total > 0 and nci:
        nci_ratio = nci / equity_total
    elif equity_parent is not None and nci and (equity_parent + nci) > 0:
        nci_ratio = nci / (equity_parent + nci)

    components = {
        "cash": cash,
        "st_fin": st_fin,
        "fvpl": fvpl,
        "other_fin": other_fin,
        "settlement_main": settlement_main,
        "settlement": settlement,
        "deposits": deposits,
        "debt_naive": debt_naive,
        "st_borrow": st_borrow,
        "lt_borrow": lt_borrow,
        "current_lt": current_lt,
        "bond": bond,
        "convertible": convertible,
        "exchangeable": exchangeable,
        "exchangeable_outside": exchangeable_outside,
        "lease": lease,
    }
    bridge = (
        f"현금성 {fmt_eok(cash_like)} - 차입/사채 {fmt_eok(debt_naive)} "
        f"= 단순 순현금 {fmt_eok(naive)}"
    )
    return ScopeCash(
        present=True,
        naive_krw=naive,
        adjusted_krw=naive + effect,
        lease_liability_krw=lease,
        nci_krw=nci,
        equity_total_krw=equity_total,
        equity_parent_krw=equity_parent,
        nci_ratio=nci_ratio,
        adjustments=adjustments,
        components=components,
        bridge=bridge,
    )


def _settlement_main(rows: list[dict], fs: str) -> int:
    """내역이 아닌 본계정 미수금. 단순 순현금에만 더하고, 조정에서는 전체 미수금을 뺀다."""
    from screeners.rows import _sj, detail_blank, fs_of, tag_of

    total = 0
    found = False
    for row in rows:
        if fs_of(row) != fs.upper() or _sj(row) != "BS":
            continue
        if tag_of(row) != "settlement":
            continue
        if not detail_blank(row.get("account_detail")):
            continue
        value = _end(row)
        if value is None:
            continue
        total += value
        found = True
    return total if found else 0


def _end(row: dict) -> int | None:
    from screeners.rows import amount_of

    return amount_of(row, "end")


def _exchangeable_inside_bond(rows: list[dict], fs: str) -> int:
    """사채 내역으로만 나온 교환사채. 사채 합계에 이미 들어 있다."""
    from screeners.rows import _sj, amount_of, detail_blank, fs_of, norm_name, tag_of

    total = 0
    for row in rows:
        if fs_of(row) != fs.upper() or _sj(row) != "BS":
            continue
        if tag_of(row) != "exchangeable":
            continue
        if detail_blank(row.get("account_detail")):
            continue
        parent = norm_name(row.get("account_nm"))
        if "사채" not in parent or "교환사채" in parent:
            continue
        value = amount_of(row, "end")
        if value:
            total += value
    return total
