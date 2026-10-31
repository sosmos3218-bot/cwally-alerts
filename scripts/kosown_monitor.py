#!/usr/bin/env python3
"""KOSPI foreign ownership threshold monitor. Not a trade signal."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "kosown.json"
STATE_PATH = ROOT / "state" / "kosown.json"
KST = timezone(timedelta(hours=9))


def helper(name: str):
    try:
        return __import__(name)
    except ImportError:
        flat = ROOT.parent / "ntfy-source" / f"scripts__{name}.py"
        if not flat.exists():
            raise
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, flat)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(fallback)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = copy.deepcopy(fallback)
    if isinstance(data, dict):
        out.update(data)
    return out


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_state() -> dict:
    return {
        "schema_version": 1,
        "last_baseline_week": None,
        "last_samsung_own": None,
        "last_skhynix_own": None,
        "last_kospi": None,
        "updated_at": None,
    }


def body(stamp: str, what: str, status: str, action: str, later: str, reduce: str, avoid: str) -> str:
    lines = [
        f"{stamp} | KOSOWN",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {reduce}",
        f"하지 말 것: {avoid}",
    ]
    return "\n".join(lines)


def publish(ntfy, server: str, title: str, message: str, priority: int = 3) -> None:
    print(f"channel=cwally-port title={title}")
    print(message)
    ntfy.publish("cwally-port", title, message, priority, ["chart_with_upwards_trend"], server)


def install_confirm(now: datetime) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    th = cfg["thresholds"]
    message = body(
        stamp,
        "외국인 지분율·코스피 임계값 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        f"관찰 임계: 삼성전자 {th['samsung_own']}%, SK하이닉스 {th['skhynix_own']}%, 코스피 {th['kospi_reanchor']}. {th['note']}",
        "지분율 회복과 순매수 전환을 함께 보세요. 지분율만으로 매수하지 마세요.",
        "가이던스와 함께 확인할 때 재안착 가능성을 보세요.",
        "이 알림으로 즐이지 마세요. 임계값 회복 전까지는 관망.",
        "지분율 숫자만으로 수급 전환 확정하기. 매크로 리스크 무시하기.",
    )
    publish(ntfy, server, "[KOSOWN] 005930/000660 설치 확인 | 매매 신호 아님", message, 3)
    print("install-confirm sent; state not written")
    return 0


def run(now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, default_state())
    work = copy.deepcopy(state)
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    if now.year != int(cfg["year_guard"]) or not (cfg["active_from"] <= today <= cfg["expire_date"]):
        print(f"{stamp} outside active window; silent")
        return 0
    ntfy = helper("ntfy_util")
    server = ntfy.resolve_server(cfg.get("ntfy_server"))
    th = cfg["thresholds"]
    week = now.strftime("%G-W%V")
    if now.weekday() == 0 and state.get("last_baseline_week") != week:
        message = body(
            stamp,
            "외국인 지분율·코스피 임계값 주간 점검입니다. 매수 신호가 아닙니다.",
            f"관찰 기준: 삼성전자 {th['samsung_own']}%, SK하이닉스 {th['skhynix_own']}%, 코스피 {th['kospi_reanchor']}. {th['note']}",
            "지분율 회복 여부와 일별 순매수 규모를 함께 확인하세요.",
            "가이던스와 매크로가 맞물릴 때 재안착 확률을 보세요.",
            "이 알림으로 즐이지 마세요.",
            "지분율만으로 즉각 수급 전환 판단하기.",
        )
        try:
            publish(ntfy, server, "[KOSOWN] 005930/000660 기준 유지 | 임계값 회복 전까지 관망", message, 3)
            work["last_baseline_week"] = week
            work["updated_at"] = stamp
            atomic_save(STATE_PATH, work)
            print("baseline sent")
        except Exception as exc:
            print(f"publish failed: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"{stamp} no weekly baseline due")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-confirm", action="store_true")
    args = parser.parse_args()
    now = datetime.now(KST)
    if args.install_confirm:
        return install_confirm(now)
    return run(now)


if __name__ == "__main__":
    raise SystemExit(main())
