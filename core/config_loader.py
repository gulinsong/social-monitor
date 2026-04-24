import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_config_cache = None


def load_config(path: str | Path = None, reload: bool = False) -> dict:
    global _config_cache
    if _config_cache and not reload and not path:
        return _config_cache

    cfg_path = Path(path) if path else CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

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
