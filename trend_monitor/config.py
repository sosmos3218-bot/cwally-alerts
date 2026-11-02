"""설정. YAML 기본값과 CLI 덮어쓰기."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from trend_monitor.errors import UserInputError

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class SectorGroup:
    name: str
    tickers: list[str]


@dataclass
class Config:
    account_krw: int = 100_000_000
    risk_pct: float = 1.0
    max_position_weight_pct: float = 20.0
    max_total_open_risk_pct: float = 6.0
    cash_krw: int | None = None
    stop_mode: str = "pct"
    stop_pct: float = 7.5
    atr_k: float = 2.0
    max_stop_distance_pct: float = 10.0
    panic_enabled: bool = True
    panic_pct: float = 12.0
    climax_min_gain_pct: float = 20.0
    climax_body_pct: float = -4.5
    climax_range_atr: float = 2.0
    climax_sell_fraction: float = 0.5
    ma_window: int = 20
    ma_arm_gain_pct: float = 8.0
    chase_max_distance_pct: float = 5.0
    liquidity_avg_days: int = 20
    liquidity_min_avg_value_krw: float = 5_000_000_000
    base_min_days: int = 35
    base_max_days: int = 60
    base_min_depth_pct: float = 10.0
    base_max_depth_pct: float = 35.0
    high52_days: int = 252
    trend_mas: tuple[int, int, int] = (50, 150, 200)
    history_calendar_days: int = 1200
    reference_tickers: tuple[str, ...] = ("005930", "000660")
    download_min_marcap_krw: float = 0
    download_min_amount_krw: float = 0
    workers: int = 8
    max_fetch_fail_ratio: float = 0.05
    eps_min_yoy_pct: float = 15.0
    eps_require: bool = False
    dart_min_interval_sec: float = 0.25
    breadth_top_n: int = 200
    breadth_ma_window: int = 50
    breadth_min_pct: float = 30.0
    sector_breadth_enabled: bool = False
    sector_breadth_min_ratio: float = 0.5
    sector_groups: list[SectorGroup] = field(default_factory=list)
    ntfy_enabled: bool = False
    ntfy_topic: str = "cwally-port"
    ntfy_priority: int = 3
    ends_kst: str | None = None
    ends_note: str = "스케줄 없음. 수동 실행만."

    def exit_params(self):
        from trend_monitor.rules import ExitParams

        return ExitParams(
            stop_pct=self.stop_pct,
            atr_k=self.atr_k,
            panic_enabled=self.panic_enabled,
            panic_pct=self.panic_pct,
            climax_min_gain_pct=self.climax_min_gain_pct,
            climax_body_pct=self.climax_body_pct,
            climax_range_atr=self.climax_range_atr,
            climax_sell_fraction=self.climax_sell_fraction,
            ma_arm_gain_pct=self.ma_arm_gain_pct,
            require_breadth=self.sector_breadth_enabled,
        )

    def public_params(self) -> dict:
        return {
            "account_krw": self.account_krw,
            "risk_pct": self.risk_pct,
            "max_position_weight_pct": self.max_position_weight_pct,
            "max_total_open_risk_pct": self.max_total_open_risk_pct,
            "cash_krw": self.cash_krw,
            "stop_mode": self.stop_mode,
            "stop_pct": self.stop_pct,
            "atr_k": self.atr_k,
            "max_stop_distance_pct": self.max_stop_distance_pct,
            "panic_enabled": self.panic_enabled,
            "panic_pct": self.panic_pct,
            "ma_window": self.ma_window,
            "ma_arm_gain_pct": self.ma_arm_gain_pct,
            "chase_max_distance_pct": self.chase_max_distance_pct,
            "liquidity_avg_days": self.liquidity_avg_days,
            "liquidity_min_avg_value_krw": self.liquidity_min_avg_value_krw,
            "base_min_days": self.base_min_days,
            "base_max_days": self.base_max_days,
            "base_min_depth_pct": self.base_min_depth_pct,
            "base_max_depth_pct": self.base_max_depth_pct,
            "high52_days": self.high52_days,
            "trend_mas": list(self.trend_mas),
            "eps_min_yoy_pct": self.eps_min_yoy_pct,
            "eps_require": self.eps_require,
            "breadth_top_n": self.breadth_top_n,
            "breadth_ma_window": self.breadth_ma_window,
            "breadth_min_pct": self.breadth_min_pct,
            "download_min_marcap_krw": self.download_min_marcap_krw,
            "download_min_amount_krw": self.download_min_amount_krw,
            "ntfy_enabled": self.ntfy_enabled,
        }


def load_config(path: Path | None = None) -> Config:
    file = Path(path) if path else DEFAULT_CONFIG
    if not file.is_file():
        raise UserInputError(f"설정 파일이 없습니다: {file}")
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise UserInputError(f"설정 파일이 객체가 아닙니다: {file}")
    groups = []
    for item in raw.get("sector_groups") or []:
        if not isinstance(item, dict):
            continue
        groups.append(
            SectorGroup(
                name=str(item.get("name") or ""),
                tickers=[str(t).zfill(6) if str(t).isdigit() else str(t) for t in item.get("tickers") or []],
            )
        )
    data = {k: v for k, v in raw.items() if k != "sector_groups"}
    known = {f.name for f in Config.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = [k for k in data if k not in known]
    if unknown:
        raise UserInputError(f"설정에서 모르는 키입니다: {', '.join(unknown)}")
    if "trend_mas" in data and data["trend_mas"] is not None:
        data["trend_mas"] = tuple(int(x) for x in data["trend_mas"])
    if "reference_tickers" in data and data["reference_tickers"] is not None:
        data["reference_tickers"] = tuple(str(x).zfill(6) if str(x).isdigit() else str(x) for x in data["reference_tickers"])
    if data.get("stop_mode"):
        data["stop_mode"] = normalize_stop_mode(data["stop_mode"])
    cfg = Config(**data, sector_groups=groups)
    _validate(cfg)
    return cfg


def normalize_stop_mode(value: str | None, default: str = "pct") -> str:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower().replace(" ", "")
    if text in {"pct", "percent", "7.5%", "-7.5%", "percent7.5"}:
        return "pct"
    if text in {"atr", "2atr", "2.0atr", "atr2"}:
        return "atr"
    raise UserInputError("손절 방식은 pct 또는 atr 입니다. -7.5% 처럼 붙이면 옵션으로 오인됩니다.")


def apply_cli(cfg: Config, args) -> Config:
    updates = {}
    mapping = {
        "account": "account_krw",
        "risk_pct": "risk_pct",
        "max_weight": "max_position_weight_pct",
        "max_total_risk": "max_total_open_risk_pct",
        "cash": "cash_krw",
        "workers": "workers",
    }
    for src, dest in mapping.items():
        value = getattr(args, src, None)
        if value is not None:
            updates[dest] = value
    if getattr(args, "stop_mode", None):
        updates["stop_mode"] = normalize_stop_mode(args.stop_mode)
    if getattr(args, "ma_window", None):
        updates["ma_window"] = int(args.ma_window)
    cfg = replace(cfg, **updates) if updates else cfg
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.account_krw <= 0:
        raise UserInputError("계좌 금액은 0보다 커야 합니다.")
    if not 0 < cfg.risk_pct <= 100:
        raise UserInputError("위험 비율은 0 초과 100 이하입니다.")
    if cfg.ma_window not in {20, 50}:
        raise UserInputError("이평 기간은 20 또는 50 입니다.")
    if cfg.stop_mode not in {"pct", "atr"}:
        raise UserInputError("손절 방식은 pct 또는 atr 입니다.")
    if cfg.workers < 1:
        raise UserInputError("workers 는 1 이상입니다.")
    if cfg.breadth_top_n < 1:
        raise UserInputError("시장 폭 상위 종목 수는 1 이상입니다.")
    if cfg.breadth_ma_window < 2:
        raise UserInputError("시장 폭 이평은 2일 이상입니다.")
    if not 0 <= cfg.breadth_min_pct <= 100:
        raise UserInputError("시장 폭 기준은 0에서 100 사이입니다.")
    fast, mid, slow = cfg.trend_mas
    if not (fast < mid < slow):
        raise UserInputError("추세 이평은 짧은 것, 중간, 긴 것 순서입니다.")
