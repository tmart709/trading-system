"""Config loading. YAML for behavior, environment variables (.env) for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MODES = ("record", "paper", "demo", "live")


class ConfigError(ValueError):
    pass


VARIABLES = ("high", "low")


@dataclass(frozen=True)
class CityConfig:
    code: str
    kalshi_series: str | None            # daily high
    polymarket_event_prefix: str | None  # daily high (Polymarket US has no lows)
    station: str
    cli_code: str
    lat: float
    lon: float
    tz: str
    name: str | None = None
    kalshi_low_series: str | None = None

    def kalshi_series_for(self, variable: str) -> str | None:
        return self.kalshi_series if variable == "high" else self.kalshi_low_series

    def variable_for_series(self, series: str | None) -> str | None:
        if series and series == self.kalshi_series:
            return "high"
        if series and series == self.kalshi_low_series:
            return "low"
        return None


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    cities: dict[str, CityConfig] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        p = Path(self.raw.get("db_path", "db/trading.sqlite"))
        return p if p.is_absolute() else ROOT / p

    def venue(self, name: str) -> dict[str, Any]:
        return self.raw["venues"][name]

    def venue_mode(self, name: str) -> str:
        return self.venue(name)["mode"]

    def strategy(self, name: str) -> dict[str, Any]:
        return self.raw["strategies"][name]

    def strategy_cities(self, name: str) -> list[CityConfig]:
        sel = self.strategy(name).get("cities", "all")
        return list(self.cities.values()) if sel == "all" else [self.cities[c] for c in sel]

    def effective_mode(self, strategy: str, venue: str) -> str:
        """A strategy trades a venue at the lower of the two modes (record < paper < demo < live)."""
        s, v = self.strategy(strategy)["mode"], self.venue_mode(venue)
        return MODES[min(MODES.index(s), MODES.index(v))]

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw["risk"]

    @property
    def recorder(self) -> dict[str, Any]:
        return self.raw["recorder"]

    @property
    def weather(self) -> dict[str, Any]:
        return self.raw["weather"]


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config" / "config.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    cfg = Config(raw=raw, path=path)
    _validate(cfg)
    for code, c in raw["weather"]["cities"].items():
        cfg.cities[code] = CityConfig(code=code, **c)
    return cfg


def _validate(cfg: Config) -> None:
    for name, v in cfg.raw["venues"].items():
        if v["mode"] not in MODES:
            raise ConfigError(f"venue {name}: unknown mode {v['mode']!r}")
        if v["mode"] == "live" and v.get("live_enabled") is not True:
            raise ConfigError(f"venue {name}: mode=live requires live_enabled: true")
        if name == "polymarket_us" and v["mode"] == "demo":
            raise ConfigError("polymarket_us has no sandbox; demo mode is not available")
    for name, s in cfg.raw["strategies"].items():
        if s["mode"] not in MODES:
            raise ConfigError(f"strategy {name}: unknown mode {s['mode']!r}")


def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val if val else default
