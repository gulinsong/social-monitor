"""Tests for core.config_loader"""
import tempfile
import yaml
from pathlib import Path
from core.config_loader import _validate_config


def test_valid_config_no_warnings():
    config = {
        "app": {"port": 5000, "secret_key": "my-secret"},
        "platforms": {
            "weibo": {
                "enabled": True,
                "interval_hours": 6,
                "request_delay": {"min": 3.0, "max": 8.0},
            }
        },
    }
    warnings = _validate_config(config)
    assert len(warnings) == 0


def test_default_secret_key_warns():
    config = {
        "app": {"port": 5000, "secret_key": "change-me-in-production"},
    }
    warnings = _validate_config(config)
    assert any("secret_key" in w for w in warnings)


def test_invalid_port_warns():
    config = {"app": {"port": 99999}}
    warnings = _validate_config(config)
    assert any("port" in w for w in warnings)


def test_negative_delay_warns():
    config = {
        "platforms": {
            "weibo": {
                "enabled": True,
                "interval_hours": 6,
                "request_delay": {"min": -1, "max": 5},
            }
        },
    }
    warnings = _validate_config(config)
    assert any("negative" in w for w in warnings)


def test_min_greater_than_max_warns():
    config = {
        "platforms": {
            "weibo": {
                "enabled": True,
                "interval_hours": 6,
                "request_delay": {"min": 10, "max": 3},
            }
        },
    }
    warnings = _validate_config(config)
    assert any("min" in w and "max" in w for w in warnings)


def test_unknown_platform_warns():
    config = {
        "platforms": {
            "twitter": {"enabled": True, "interval_hours": 6},
        },
    }
    warnings = _validate_config(config)
    assert any("Unknown platform" in w for w in warnings)


def test_zero_interval_warns():
    config = {
        "platforms": {
            "weibo": {
                "enabled": True,
                "interval_hours": 0,
            }
        },
    }
    warnings = _validate_config(config)
    assert any("interval" in w for w in warnings)


def test_load_config_from_file():
    from core.config_loader import load_config
    config = {
        "app": {"port": 5000, "secret_key": "test"},
        "platforms": {},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        f.flush()
        loaded = load_config(f.name, reload=True)
        assert loaded["app"]["port"] == 5000
