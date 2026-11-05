"""OpenDART fnlttSinglAcntAll 행을 계정 묶음으로 모은다.

본계정과 내역(account_detail)을 더하면 두 번 세지 않는다.
연결(CFS)과 별도(OFS)는 호출하는 쪽에서 나눈다.
"""

from __future__ import annotations

from screeners.money import parse_krw

# 한 줄만 고르는 계정. 비슷한 이름(영업이익, 영업이익(손실))이 같이 있으면 대표 이름.
PICK_ONE = {
    "cash": ("현금및현금성자산",),
    "st_fin": ("단기금융상품",),
    "fvpl": (
        "유동당기손익-공정가치측정금융자산",
        "유동당기손익인식금융자산",
        "당기손익-공정가치측정금융자산",
    ),
    "other_fin": ("기타유동금융자산", "기타금융자산"),
    "revenue": ("매출액", "영업수익", "수익(매출액)"),
    "op_income": ("영업이익", "영업이익(손실)"),
    "fin_income": ("금융수익",),
    "fin_cost": ("금융비용", "금융원가"),
    "ni_owner": (
        "지배기업의소유주에게귀속되는당기순이익",
        "지배기업의소유주에게귀속되는당기순손익",
        "지배주주순이익",
        "지배기업소유주지분순이익",
    ),
    "ni_total": ("당기순이익", "당기순손익"),
    "equity_parent": (
        "지배기업의소유주에게귀속되는자본",
        "지배기업소유주지분",
        "지배주주지분",
    ),
    "equity_total": ("자본총계",),
    "nci": ("비지배지분",),
    "cfo": ("영업활동현금흐름",),
}

# 유동, 비유동처럼 부분의 합이 필요한 계정.
SUM_PARTS = {
    "lease",
    "deposit",
    "settlement",
    "bond",
    "exchangeable",
    "convertible",
    "st_borrow",
    "lt_borrow",
    "current_lt",
    "fx_gain",
    "dividend_cf",
    "capex",
}

GENERIC_NAMES = {
    "st_borrow": {"차입금", "차입금합계"},
}


def norm_name(text: str | None) -> str:
    return "".join(str(text or "").split())


def detail_blank(text: str | None) -> bool:
    return norm_name(text) in {"", "-", "해당없음", "없음"}


def _sj(row: dict) -> str:
    return str(row.get("sj_div") or "").upper()


def tag_from_name(name: str, sj: str) -> str | None:
    n = norm_name(name)
    if not n:
        return None
    if sj == "BS":
        if "리스부채" in n or "사용권부채" in n:
            return "lease"
        if "교환사채" in n:
            return "exchangeable"
        if "전환사채" in n or "신주인수권부사채" in n:
            return "convertible"
        if "사채" in n:
            return "bond"
        if "유동성장기" in n or "유동성차입" in n:
            return "current_lt"
        if "단기차입" in n or n in {"차입금", "차입금합계"}:
            return "st_borrow"
        if "장기차입" in n:
            return "lt_borrow"
        if "예수금" in n or "결제예수" in n:
            return "deposit"
        if any(word in n for word in ("미수금", "정산채권", "미정산", "정산미수")):
            return "settlement"
        if "현금및현금성자산" in n:
            return "cash"
        if "단기금융상품" in n:
            return "st_fin"
        if "당기손익" in n and "공정가치" in n and "비유동" not in n:
            return "fvpl"
        if "기타유동금융자산" in n or (n == "기타금융자산" and "비유동" not in n):
            return "other_fin"
        if "비지배지분" in n and "손익" not in n and "이익" not in n:
            return "nci"
        if "지배" in n and ("소유주" in n or "주주" in n) and "손익" not in n and "이익" not in n:
            return "equity_parent"
        if n in {"자본총계", "자본합계"}:
            return "equity_total"
        return None
    if sj in {"IS", "CIS"}:
        if "매출총이익" in n or "매출원가" in n:
            return None
        if n.startswith("매출액") or n.startswith("영업수익") or n.startswith("수익(매출액)"):
            return "revenue"
        if "영업이익" in n and "률" not in n and "금융" not in n and "법인세" not in n:
            return "op_income"
        if "금융수익" in n:
            return "fin_income"
        if "금융비용" in n or "금융원가" in n:
            return "fin_cost"
        if "외환차익" in n or "외화환산이익" in n:
            return "fx_gain"
        if "비지배" in n and ("순이익" in n or "순손익" in n):
            return "ni_nci"
        if "지배" in n and ("순이익" in n or "순손익" in n):
            return "ni_owner"
        if (
            n.startswith("당기순이익")
            or n.startswith("당기순손익")
            or n.startswith("분기순이익")
            or n.startswith("반기순이익")
        ) and "지배" not in n and "비지배" not in n:
            return "ni_total"
        return None
    if sj == "CF":
        if "배당금" in n and "지급" in n:
            return "dividend_cf"
        if "영업활동현금흐름" in n:
            return "cfo"
        if "유형자산의취득" in n:
            return "capex"
        return None
    return None


def tag_of(row: dict) -> str | None:
    """내역 이름이 더 구체적이면 내역을 쓰고, 아니면 계정명을 쓴다."""
    sj = _sj(row)
    detail = row.get("account_detail")
    if not detail_blank(detail):
        tagged = tag_from_name(str(detail), sj)
        if tagged:
            return tagged
    tagged = tag_from_name(str(row.get("account_nm") or ""), sj)
    if tagged:
        return tagged
    return None


def fs_of(row: dict) -> str:
    text = str(row.get("fs_div") or "CFS").upper()
    return text if text in {"CFS", "OFS"} else "CFS"


def _ord(row: dict) -> int:
    try:
        return int(str(row.get("ord") or "9999"))
    except ValueError:
        return 9999


def _krw_ok(row: dict) -> bool:
    currency = norm_name(row.get("currency") or "KRW").upper()
    return currency in {"", "KRW", "원", "KRW원"}


def amount_of(row: dict, field: str) -> int | None:
    """field: end(당기말/당기), prior(전기), ytd(누적), ytd_prior(전년 누적)."""
    if field == "end":
        keys = ("thstrm_amount",)
    elif field == "prior":
        keys = ("frmtrm_amount",)
    elif field == "ytd":
        keys = ("thstrm_add_amount", "thstrm_amount")
    elif field == "ytd_prior":
        keys = ("frmtrm_add_amount", "frmtrm_amount")
    else:
        raise ValueError(f"unknown amount field: {field}")
    for key in keys:
        if key not in row:
            continue
        raw = row.get(key)
        if raw is None or str(raw).strip() in {"", "-"}:
            continue
        return parse_krw(raw)
    return None


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in sorted(rows, key=_ord):
        key = (norm_name(row.get("account_nm")), norm_name(row.get("account_detail")))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _collapse_mains(rows: list[dict], tag: str) -> list[dict]:
    rows = _dedupe(rows)
    if tag in PICK_ONE:
        for canon in PICK_ONE[tag]:
            for row in rows:
                if norm_name(row.get("account_nm")) == norm_name(canon):
                    return [row]
        if not rows:
            return []
        return [sorted(rows, key=_ord)[0]]
    generic = GENERIC_NAMES.get(tag, set())
    names = [norm_name(row.get("account_nm")) for row in rows]
    if any(name not in generic for name in names):
        rows = [row for row in rows if norm_name(row.get("account_nm")) not in generic]
    return rows


def sum_tag(rows: list[dict], tag: str, field: str, fs: str) -> int | None:
    """태그의 금액. 계정이 하나도 없으면 None, 있으면 합(0 포함)."""
    chosen = [
        row
        for row in rows
        if fs_of(row) == fs.upper() and _sj(row) in _sj_ok(tag) and tag_of(row) == tag and _krw_ok(row)
    ]
    if not chosen:
        return None
    mains = [row for row in chosen if detail_blank(row.get("account_detail"))]
    details = [row for row in chosen if not detail_blank(row.get("account_detail"))]
    if details:
        parent_names = {norm_name(row.get("account_nm")) for row in details}
        mains = [row for row in mains if norm_name(row.get("account_nm")) not in parent_names]
        use = _dedupe(details) + _collapse_mains(mains, tag)
    else:
        use = _collapse_mains(mains, tag)
    total = 0
    found = False
    for row in use:
        value = amount_of(row, field)
        if value is None:
            continue
        total += value
        found = True
    if not found:
        return None
    return total


def _sj_ok(tag: str) -> set[str]:
    if tag in {"revenue", "op_income", "fin_income", "fin_cost", "fx_gain", "ni_owner", "ni_total", "ni_nci"}:
        return {"IS", "CIS"}
    if tag in {"dividend_cf", "cfo", "capex"}:
        return {"CF"}
    return {"BS"}


def has_statement(rows: list[dict], fs: str) -> bool:
    return any(fs_of(row) == fs.upper() and _sj(row) == "BS" and _krw_ok(row) for row in rows)


def skipped_fx(rows: list[dict]) -> int:
    return sum(1 for row in rows if not _krw_ok(row))
