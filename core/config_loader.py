import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_config_cache = None

_VALID_PLATFORMS = {"weibo", "wechat", "maimai", "xiaohongshu"}


def _validate_config(config: dict) -> list[str]:
    """Return list of warning messages for invalid config values"""
    warnings = []
    app = config.get("app", {})

    port = app.get("port", 5000)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        warnings.append(f"app.port={port} is not a valid port (1-65535)")

    if app.get("secret_key") in (None, "change-me-in-production", "change-me"):
        warnings.append("app.secret_key is using default value — change in production")

    retention = app.get("retention_days", 0)
    if not isinstance(retention, int) or retention < 0:
        warnings.append(f"app.retention_days={retention} must be a non-negative integer")

    for name, pcfg in config.get("platforms", {}).items():
        if name not in _VALID_PLATFORMS:
            warnings.append(f"Unknown platform '{name}' in config")
            continue
        if not isinstance(pcfg.get("enabled", True), bool):
            warnings.append(f"platforms.{name}.enabled must be boolean")

        interval = pcfg.get("interval_hours", 6)
        if not isinstance(interval, (int, float)) or interval <= 0:
            warnings.append(f"platforms.{name}.interval_hours={interval} must be positive")

        delay = pcfg.get("request_delay", {})
        if delay:
            dmin = delay.get("min", 0)
            dmax = delay.get("max", 0)
            if dmin < 0 or dmax < 0:
                warnings.append(f"platforms.{name}.request_delay has negative values")
            elif dmin > dmax:
                warnings.append(f"platforms.{name}.request_delay.min ({dmin}) > max ({dmax})")

    return warnings


def load_config(path: str | Path = None, reload: bool = False) -> dict:
    global _config_cache
    if _config_cache and not reload and not path:
        return _config_cache

    cfg_path = Path(path) if path else CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    warnings = _validate_config(config)
    for w in warnings:
        log.warning("Config: %s", w)

    _config_cache = config
    return config


def save_config(config: dict, path: str | Path = None):
    global _config_cache
    cfg_path = Path(path) if path else CONFIG_PATH
    tmp = cfg_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    tmp.replace(cfg_path)
    _config_cache = config


def get_platform_config(platform_name: str, config: dict = None) -> dict:
    cfg = config or load_config()
    return cfg.get("platforms", {}).get(platform_name, {})


def get_default_keywords(config: dict = None) -> list[str]:
    cfg = config or load_config()
    return cfg.get("default_keywords", [])
