"""Share counts and class titles from a 10-Q or 10-K instance.

Companyfacts drops axis members, so a dual-class cover page is visible only on
the filing. This parser reads the extracted XBRL instance (``*_htm.xml``) or an
inline XBRL document and returns rows ``select_shares`` can consume.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

_SHARE_TAGS = {
    "EntityCommonStockSharesOutstanding": ("dei", "instant"),
    "CommonStockSharesOutstanding": ("us-gaap", "instant"),
    "WeightedAverageNumberOfSharesOutstandingBasic": ("us-gaap", "duration"),
}
_TITLE_TAG = "Security12bTitle"


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    if ":" in tag:
        return tag.split(":", 1)[-1]
    return tag


def _num(text: str, scale: str | None) -> float | None:
    raw = (text or "").strip().replace(",", "").replace(" ", "")
    if not raw or raw == "-":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if scale not in (None, "", "INF"):
        try:
            value *= 10 ** int(scale)
        except ValueError:
            return None
    return value


def _contexts_from_tree(root: ElementTree.Element) -> dict[str, dict]:
    found = {}
    for node in root.iter():
        if _local(node.tag) != "context":
            continue
        cid = node.attrib.get("id") or ""
        if not cid:
            continue
        instant = None
        start = None
        end = None
        members = []
        for child in node.iter():
            name = _local(child.tag)
            if name == "instant" and child.text:
                instant = child.text.strip()[:10]
            elif name == "startDate" and child.text:
                start = child.text.strip()[:10]
            elif name == "endDate" and child.text:
                end = child.text.strip()[:10]
            elif name == "explicitMember":
                member = (child.text or "").strip()
                if member:
                    members.append((child.attrib.get("dimension") or "", member))
        found[cid] = {"instant": instant, "start": start, "end": end or instant, "members": members}
    return found


def _pick_member(members: list) -> tuple[str, str]:
    """Prefer the class-of-stock axis when a context carries more than one member."""
    pairs = []
    for item in members or []:
        if isinstance(item, str):
            dim, mem = "", item
        else:
            dim, mem = item
        mem = str(mem or "").strip()
        if mem:
            pairs.append((str(dim or ""), mem))
    for dim, mem in pairs:
        if dim.split(":")[-1].endswith("ClassOfStockAxis"):
            return dim, mem
    if pairs:
        return pairs[0]
    return "", ""


def _rows_from_tree(root: ElementTree.Element, contexts: dict[str, dict]) -> list[dict]:
    rows = []
    for node in root.iter():
        tag = _local(node.tag)
        if tag not in _SHARE_TAGS and tag != _TITLE_TAG:
            continue
        cref = node.attrib.get("contextRef") or ""
        ctx = contexts.get(cref) or {}
        dimension, member = _pick_member(ctx.get("members") or [])
        if tag == _TITLE_TAG:
            text = "".join(node.itertext()).strip()
            if not text:
                continue
            rows.append(
                {
                    "tag": tag,
                    "text": re.sub(r"\s+", " ", text),
                    "end": ctx.get("end"),
                    "start": ctx.get("start"),
                    "member": member,
                }
            )
            continue
        taxonomy, kind = _SHARE_TAGS[tag]
        value = _num("".join(node.itertext()), node.attrib.get("scale"))
        if value is None or value <= 0:
            continue
        unit = (node.attrib.get("unitRef") or "").lower()
        if unit and "share" not in unit and unit not in {"shares", "pure"}:
            continue
        end = ctx.get("instant") if kind == "instant" else ctx.get("end")
        if not end:
            continue
        rows.append(
            {
                "tag": tag,
                "taxonomy": taxonomy,
                "source": f"{taxonomy}:{tag}",
                "kind": kind,
                "start": None if kind == "instant" else ctx.get("start"),
                "end": end,
                "val": value,
                "unit": "shares",
                "member": member,
                "dimension": dimension,
                "fp": "",
                "fy": None,
            }
        )
    return rows


_CTX_RE = re.compile(r"""<(?:\w+:)?context\b([^>]*)>(.*?)</(?:\w+:)?context>""", re.IGNORECASE | re.DOTALL)
_FACT_RE = re.compile(
    r"""<(?:ix:)?(?:nonFraction|nonNumeric)\b([^>]*)>(.*?)</(?:ix:)?(?:nonFraction|nonNumeric)>""",
    re.IGNORECASE | re.DOTALL,
)
_MEMBER_RE = re.compile(
    r"""<(?:\w+:)?explicitMember\b([^>]*)>(.*?)</(?:\w+:)?explicitMember>""",
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(r"""([:\w-]+)\s*=\s*["']([^"']*)["']""")


def _attrs(text: str) -> dict[str, str]:
    return {key.split(":")[-1]: value for key, value in _ATTR_RE.findall(text)}


def _contexts_from_text(text: str) -> dict[str, dict]:
    found = {}
    for attrs, body in _CTX_RE.findall(text):
        meta = _attrs(attrs)
        cid = meta.get("id") or ""
        if not cid:
            continue
        instant = _first_tag_text(body, "instant")
        start = _first_tag_text(body, "startDate")
        end = _first_tag_text(body, "endDate") or instant
        members = []
        for member_attrs, member_text in _MEMBER_RE.findall(body):
            member = re.sub(r"\s+", " ", member_text).strip()
            if not member:
                continue
            members.append((_attrs(member_attrs).get("dimension") or "", member))
        found[cid] = {"instant": instant, "start": start, "end": end, "members": members}
    return found


def _first_tag_text(body: str, name: str) -> str | None:
    match = re.search(rf"<(?:\w+:)?{name}\b[^>]*>([^<]+)</(?:\w+:)?{name}>", body, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()[:10]


def _rows_from_text(text: str, contexts: dict[str, dict]) -> list[dict]:
    """Inline XBRL fallback when the document is not a bare XML instance."""
    fake_root_facts = []
    for attrs, body in _FACT_RE.findall(text):
        meta = _attrs(attrs)
        name = meta.get("name") or ""
        tag = _local(name)
        if tag not in _SHARE_TAGS and tag != _TITLE_TAG:
            continue
        ctx = contexts.get(meta.get("contextRef") or "") or {}
        dimension, member = _pick_member(ctx.get("members") or [])
        if tag == _TITLE_TAG:
            title = re.sub(r"<[^>]+>", " ", body)
            title = re.sub(r"\s+", " ", title).strip()
            if title:
                fake_root_facts.append(
                    {"tag": tag, "text": title, "end": ctx.get("end"), "start": ctx.get("start"), "member": member}
                )
            continue
        taxonomy, kind = _SHARE_TAGS[tag]
        value = _num(re.sub(r"<[^>]+>", "", body), meta.get("scale"))
        if value is None or value <= 0:
            continue
        end = ctx.get("instant") if kind == "instant" else ctx.get("end")
        if not end:
            continue
        fake_root_facts.append(
            {
                "tag": tag,
                "taxonomy": taxonomy,
                "source": f"{taxonomy}:{tag}",
                "kind": kind,
                "start": None if kind == "instant" else ctx.get("start"),
                "end": end,
                "val": value,
                "unit": "shares",
                "member": member,
                "dimension": dimension,
                "fp": "",
                "fy": None,
            }
        )
    return fake_root_facts


_OBLIGATION_TAG = re.compile(
    r"^(ObligationToRepurchaseShares(?:Current|Noncurrent)?"
    r"|ShareRepurchaseObligation(?:Current|Noncurrent)?"
    r"|LiabilityForShareRepurchase(?:Current|Noncurrent)?)$"
)


def _obligation_row(tag: str, value: float, ctx: dict, unit: str) -> dict | None:
    if value <= 0:
        return None
    members = ctx.get("members") or []
    dimension, member = _pick_member(members)
    blob = " ".join(str(item) for item in (dimension, member))
    if "SubsequentEvent" in blob:
        return None
    end = ctx.get("instant") or (ctx.get("end") if not ctx.get("start") else None)
    if ctx.get("start") and ctx.get("start") != end:
        return None
    if not end:
        return None
    taxonomy = "us-gaap"
    return {
        "tag": tag,
        "taxonomy": taxonomy,
        "source": f"{taxonomy}:{tag}",
        "kind": "instant",
        "start": None,
        "end": end,
        "val": value,
        "value": value,
        "unit": "USD",
        "member": member,
        "dimension": dimension,
        "fp": "",
        "fy": None,
    }


def _obligation_rows_from_tree(root: ElementTree.Element, contexts: dict[str, dict]) -> list[dict]:
    rows = []
    for node in root.iter():
        tag = _local(node.tag)
        if not _OBLIGATION_TAG.match(tag):
            continue
        unit = (node.attrib.get("unitRef") or "").lower()
        if unit and "usd" not in unit and unit not in {"usd", "pure"}:
            continue
        value = _num("".join(node.itertext()), node.attrib.get("scale"))
        if value is None:
            continue
        ctx = contexts.get(node.attrib.get("contextRef") or "") or {}
        row = _obligation_row(tag, value, ctx, unit)
        if row is not None:
            rows.append(row)
    return rows


def _obligation_rows_from_text(text: str, contexts: dict[str, dict]) -> list[dict]:
    rows = []
    for attrs, body in _FACT_RE.findall(text):
        meta = _attrs(attrs)
        tag = _local(meta.get("name") or "")
        if not _OBLIGATION_TAG.match(tag):
            continue
        value = _num(re.sub(r"<[^>]+>", "", body), meta.get("scale"))
        if value is None:
            continue
        ctx = contexts.get(meta.get("contextRef") or "") or {}
        row = _obligation_row(tag, value, ctx, meta.get("unitRef") or "")
        if row is not None:
            rows.append(row)
    return rows


def parse_repurchase_obligations(text: str | bytes) -> list[dict]:
    """Instant repurchase-obligation facts from an instance or an inline document."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not text or not text.strip():
        return []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        root = None
    if root is not None:
        return _obligation_rows_from_tree(root, _contexts_from_tree(root))
    return _obligation_rows_from_text(text, _contexts_from_text(text))


def parse_share_document(text: str | bytes) -> list[dict]:
    """Share and title rows from an instance or an inline document. Empty on a bad file."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not text or not text.strip():
        return []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        root = None
    if root is not None:
        contexts = _contexts_from_tree(root)
        rows = _rows_from_tree(root, contexts)
        if rows:
            return rows
    contexts = _contexts_from_text(text)
    return _rows_from_text(text, contexts)


def stamp_filing(rows: list[dict], accn: str, form: str, filed: str) -> list[dict]:
    """Attach the filing identity companyfacts already knows."""
    stamped = []
    for row in rows:
        item = dict(row)
        item["accn"] = accn
        item["form"] = form
        item["filed"] = filed
        stamped.append(item)
    return stamped


def instance_name(items: list) -> str | None:
    """Prefer the extracted XBRL instance. Otherwise the primary HTML document."""
    records = []
    for item in items or []:
        if isinstance(item, dict):
            raw = str(item.get("name") or "")
            try:
                size = int(str(item.get("size") or "0").strip() or "0")
            except (TypeError, ValueError):
                size = 0
        else:
            raw = str(item)
            size = 0
        name = raw.rsplit("/", 1)[-1]
        if name:
            records.append((name, size))
    xmls = [name for name, _size in records if name.endswith("_htm.xml")]
    if xmls:
        return sorted(xmls)[0]
    html = []
    for name, size in records:
        lower = name.lower()
        if not lower.endswith(".htm") and not lower.endswith(".html"):
            continue
        if re.match(r"R\d+\.html?$", name, re.IGNORECASE):
            continue
        if lower.startswith("ex") or "exhibit" in lower:
            continue
        html.append((name, size))
    if not html:
        return None
    html.sort(key=lambda item: (-item[1], item[0]))
    return html[0][0]
