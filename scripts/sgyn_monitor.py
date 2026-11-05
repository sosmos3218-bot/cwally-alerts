#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "sgyn.json"
STATE_PATH = ROOT / "state" / "sgyn.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def http_json(url: str, data: dict | None = None, timeout: int = 30):
    body = None
    headers = {**UA}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    else:
        text = raw.decode("utf-8", "replace")
    return json.loads(text)


def http_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def ntfy_publish(server: str, topic: str, title: str, message: str, priority: int) -> None:
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": ["house", "chart_with_upwards_trend"]}
    ).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def latest_xlsx(cfg: dict) -> tuple[int, int, Path]:
    reb = cfg["reb"]
    listing = http_json(reb["list_url"], {"page": 1, "rows": 20, "listSubCd": reb["list_sub_cd"]})
    item = next(x for x in listing["data"] if reb["title_key"] in (x.get("bbsTit") or ""))
    seq = int(item["seq"])
    detail = http_json(reb["detail_url"], {"bbsCd": reb["bbs_cd"], "seq": seq})["data"]
    xfile = next(f for f in detail["files"] if str(f.get("fileExt", "")).lower() == "xlsx")
    file_seq = int(xfile["fileSeq"])
    dest = Path(tempfile.gettempdir()) / f"sgyn_{file_seq}.xlsx"
    if not dest.exists() or dest.stat().st_size < 1_000_000:
        body = f"bbsCd={reb['bbs_cd']}&seq={seq}&fileSeq={file_seq}".encode()
        req = urllib.request.Request(reb["file_url"], data=body, headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            dest.write_bytes(resp.read())
    return seq, file_seq, dest


def col_series(path: Path, sheet: str, col: int, last_n: int = 16) -> list[tuple[str, float]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows: list[tuple[str, float]] = []
    for row in ws.iter_rows(min_row=6, values_only=True):
        dt, val = row[0], row[col] if col < len(row) else None
        if dt is None or val is None:
            continue
        key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
        try:
            rows.append((key, float(val)))
        except (TypeError, ValueError):
            continue
    wb.close()
    return rows[-last_n:]


def pct(v: float | None) -> str:
    return "없음" if v is None else f"{v:+.2f}%"


def cnt(v: int | float | None) -> str:
    return "없음" if v is None else f"{int(v):,}건"


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.in_cell = False
        self.cell = ""
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = True
        if self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.cell = ""

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(re.sub(r"\s+", " ", self.cell).strip())
            self.in_cell = False
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []
        if tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell += data


def parse_permit_table(html: str, row_name: str) -> dict:
    parser = _TableParser()
    parser.feed(html)
    if len(parser.rows) < 2:
        raise RuntimeError("permit table missing")
    header = parser.rows[0]
    target = next(r for r in parser.rows[1:] if r and row_name in r[0])
    months = []
    for i, name in enumerate(header):
        if i == 0 or "합계" in name:
            continue
        raw = target[i] if i < len(target) else ""
        raw = raw.replace(",", "").strip()
        if raw == "":
            continue
        months.append((re.sub(r"[`']", "", name), int(float(raw))))
    if len(months) < 1:
        raise RuntimeError("permit months empty")
    cur_label, cur = months[-1]
    prev = months[-2][1] if len(months) > 1 else None
    chg = None if prev in (None, 0) else (cur - prev) / prev * 100
    return {"month": cur_label, "count": cur, "prev": prev, "mom_pct": chg}


def collect_permit(cfg: dict, prev_state: dict) -> dict:
    out = dict((prev_state.get("permit") or {}))
    for key, spec in (cfg.get("permit") or {}).items():
        try:
            html = http_text(spec["url"])
            out[key] = parse_permit_table(html, spec["row"])
            out[key]["source"] = "수원시 토허 통계"
        except Exception as exc:
            print("permit fail", key, exc, file=sys.stderr)
            out.setdefault(key, {"month": None, "count": None, "prev": None, "mom_pct": None})
            out[key]["error"] = str(exc)
    return out


def collect_iros(cfg: dict, prev_state: dict) -> dict:
    prev = dict((prev_state.get("iros") or {}))
    iros = cfg.get("iros") or {}
    try:
        end = datetime.now(KST).strftime("%Y%m")
        start_dt = datetime.now(KST).replace(day=1) - timedelta(days=400)
        start = start_dt.strftime("%Y%m")
        payload = http_json(
            iros["graph_url"],
            {
                "rdata_seq": iros.get("rdata_seq", "4"),
                "search_start_date": start,
                "search_end_date": end,
                "charttype": "chart1",
            },
        )
        rows = payload.get("dataList") or []
        series = []
        for row in rows:
            month = str(row.get("crmm") or "")
            a = row.get("val1_work1")
            b = row.get("val_work1")
            if month and a is not None and b is not None:
                series.append({"month": month, "series_a": int(a), "series_b": int(b), "total": int(a) + int(b)})
        if not series:
            raise RuntimeError("iros empty")
        cur, prev_row = series[-1], series[-2] if len(series) > 1 else None
        mom = None
        if prev_row and prev_row["total"]:
            mom = (cur["total"] - prev_row["total"]) / prev_row["total"] * 100
        return {
            "month": cur["month"],
            "total": cur["total"],
            "series_a": cur["series_a"],
            "series_b": cur["series_b"],
            "prev_total": None if prev_row is None else prev_row["total"],
            "mom_pct": mom,
            "source": "등기정보광장 매매 소유권이전",
        }
    except Exception as exc:
        print("iros fail", exc, file=sys.stderr)
        prev["error"] = str(exc)
        return prev


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    force = os.environ.get("FORCE_ALERT") == "1"
    thursday = now.weekday() == 3
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}

    if not force and not thursday and mode != "weekly":
        print("sgyn silent; thursday only")
        return 0
    if not force and prev.get("last_sent_date") == today:
        print("sgyn already sent today")
        return 0

    th = cfg["thresholds"]
    labels = cfg["labels"]
    cols = cfg["reb"]["cols"]
    permit = collect_permit(cfg, prev)
    iros = collect_iros(cfg, prev)
    try:
        _seq, file_seq, xlsx = latest_xlsx(cfg)
    except Exception as exc:
        print("reb download fail", exc, file=sys.stderr)
        ntfy_publish(
            "https://ntfy.sh",
            "cwally-port",
            "[SGYN] 수집 실패",
            stamp + "\n이게 뭐 알림: 경기남부 목요일 체크.\n지금 상태: 부동산원 시계열 다운로드 실패.\n지금 할 일: R-ONE을 직접 보세요.\n하지 말 것: 추격.",
            4,
        )
        return 0

    sale, jeonse = {}, {}
    hits: list[str] = []
    reasons: dict[str, list[str]] = {}
    series_out = {}
    week_date = None
    for key, col in cols.items():
        srows = col_series(xlsx, cfg["reb"]["sale_sheet"], col)
        jrows = col_series(xlsx, cfg["reb"]["jeonse_sheet"], col)
        sale[key] = srows[-1][1] if srows else None
        jeonse[key] = jrows[-1][1] if jrows else None
        if srows:
            week_date = srows[-1][0]
        j13 = sum(v for _, v in jrows[-13:]) if jrows else None
        series_out[key] = {"sale_week": sale[key], "jeonse_week": jeonse[key], "jeonse_13w": j13}
        why = []
        if sale[key] is not None and sale[key] <= th["sale_week_break"]:
            why.append("매매주간 " + pct(sale[key]))
        if jeonse[key] is not None and jeonse[key] <= th["jeonse_week_break"]:
            why.append("전세주간 " + pct(jeonse[key]))
        if j13 is not None and j13 <= th["jeonse_13w_break"]:
            why.append("전세 13주 " + pct(j13))
        if why:
            hits.append(key)
            reasons[key] = why

    if hits:
        title = "[SGYN] KOSPI " + "·".join(labels[k] for k in cfg["title_regions"] if k in hits)
        do_now = "제목 권역만 조정으로 보세요. 나머지는 유지."
        pri = 4
    else:
        title = "[SGYN] KOSPI 유지"
        do_now = "추격하지 말고 다음 목요일까지 봅니다."
        pri = 3

    yt_permit = permit.get("yeongtong") or {}
    permit_txt = "영통 " + (yt_permit.get("month") or "없음") + " " + cnt(yt_permit.get("count")) + " (" + pct(yt_permit.get("mom_pct")) + ")"
    if not yt_permit.get("count"):
        permit_txt = "영통 수집실패. 수지·동탄은 지자체 주간 표 없음"
    else:
        permit_txt += ". 수지·동탄은 지자체 주간 표 없음"

    iros_txt = "전국 매매등기 " + (iros.get("month") or "없음") + " " + cnt(iros.get("total")) + " (" + pct(iros.get("mom_pct")) + ")"
    if iros.get("total") is None:
        iros_txt = "등기정보광장 수집실패. 권역별 생애최초는 월간"

    lines = [
        f"{stamp} | SGYN | 기준 {week_date or '없음'} | 부동산원+토허+등기",
        "이게 뭐 알림: 동탄·영통(광교)·수지 주간 매매·전세가 유지 선을 깨는지 보는 목요일 알림.",
        "지금 상태: 동탄 매매 "
        + pct(sale.get("dongtan"))
        + " / 전세 "
        + pct(jeonse.get("dongtan"))
        + " | 영통 매매 "
        + pct(sale.get("yeongtong"))
        + " / 전세 "
        + pct(jeonse.get("yeongtong"))
        + " | 수지 매매 "
        + pct(sale.get("suji"))
        + " / 전세 "
        + pct(jeonse.get("suji")),
        "전세 13주 합: 동탄 "
        + pct((series_out.get("dongtan") or {}).get("jeonse_13w"))
        + " | 영통 "
        + pct((series_out.get("yeongtong") or {}).get("jeonse_13w"))
        + " | 수지 "
        + pct((series_out.get("suji") or {}).get("jeonse_13w")),
        "토허: " + permit_txt,
        "등기: " + iros_txt + ". 권역별 생애최초는 월간 공표.",
        "지금 할 일: " + do_now,
    ]
    for k in hits:
        lines.append("- " + labels[k] + ": " + ", ".join(reasons[k]))
    lines += [
        "나중에: 다음 목 15:20. 깨질 순서는 동탄→웰빙/영통→광교본체→수지.",
        "익절/줄일 때: 제목에 나온 권역만. 영통은 광교 본체+웰빙타운 공통 지표.",
        "하지 말 것: 네 권역 묶기. 유지 주에 추격.",
    ]
    msg = "\n".join(lines)
    print(msg)
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "last_sent_date": today,
                "week_date": week_date,
                "file_seq": file_seq,
                "hits": [labels[k] for k in hits],
                "series": series_out,
                "permit": permit,
                "iros": iros,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    ntfy_publish("https://ntfy.sh", "cwally-port", title, msg, pri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
