"""Tests for common/data.py — config loading, business config, and path resolution."""
from __future__ import annotations

import pytest
import yaml

from common.data import (
    _BUSINESS_DEFAULTS,
    CONFIG_DEFAULT,
    get_business_config,
    get_paths,
    load_config,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear lru_cache between every test to ensure isolation."""
    get_business_config.cache_clear()
    get_paths.cache_clear()
    yield
    get_business_config.cache_clear()
    get_paths.cache_clear()


# ---------------------------------------------------------------------------
# TestLoadConfig
# ---------------------------------------------------------------------------

class TestLoadConfig:

    def test_no_config_yaml_returns_defaults(self, tmp_path, monkeypatch):
        """When config.yaml does not exist, load_config returns CONFIG_DEFAULT."""
        monkeypatch.chdir(tmp_path)
        result = load_config()
        assert result == CONFIG_DEFAULT

    def test_config_yaml_merged_with_defaults(self, tmp_path, monkeypatch):
        """Keys from config.yaml are merged on top of CONFIG_DEFAULT."""
        monkeypatch.chdir(tmp_path)
        custom = {"extra_key": "extra_value"}
        (tmp_path / "config.yaml").write_text(yaml.dump(custom), encoding="utf-8")

        result = load_config()

        # Default keys still present
        assert "data_files" in result
        assert "images_dir" in result
        # Custom key merged in
        assert result["extra_key"] == "extra_value"

    def test_config_yaml_overrides_data_files(self, tmp_path, monkeypatch):
        """config.yaml can override nested keys like data_files."""
        monkeypatch.chdir(tmp_path)
        custom = {
            "data_files": {
                "main_table": "custom/table.xlsx",
                "flavor_map": "custom/flavors.csv",
            },
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(custom), encoding="utf-8")

        result = load_config()

        assert result["data_files"]["main_table"] == "custom/table.xlsx"
        assert result["data_files"]["flavor_map"] == "custom/flavors.csv"
        # images_dir still comes from CONFIG_DEFAULT
        assert result["images_dir"] == "assets"

    def test_empty_config_yaml_returns_defaults(self, tmp_path, monkeypatch):
        """An empty config.yaml (yaml.safe_load returns None) yields defaults."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("", encoding="utf-8")

        result = load_config()

        assert result == CONFIG_DEFAULT


# ---------------------------------------------------------------------------
# TestGetBusinessConfig
# ---------------------------------------------------------------------------

class TestGetBusinessConfig:

    def test_no_business_section_returns_defaults(self, tmp_path, monkeypatch):
        """Without a 'business' section, all defaults are returned."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text(yaml.dump({}), encoding="utf-8")

        result = get_business_config()

        assert result == _BUSINESS_DEFAULTS

    def test_scalar_overrides(self, tmp_path, monkeypatch):
        """Scalar business values (ddm_days, price_ref_hl) can be overridden."""
        monkeypatch.chdir(tmp_path)
        custom = {
            "business": {
                "ddm_days": 180,
                "price_ref_hl": 550.0,
            },
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(custom), encoding="utf-8")

        result = get_business_config()

        assert result["ddm_days"] == 180
        assert result["price_ref_hl"] == 550.0
        # Other defaults unchanged
        assert result["default_loss_large"] == 800
        assert result["max_slots"] == 6

    def test_custom_tanks_merged_with_defaults(self, tmp_path, monkeypatch):
        """Custom tanks in config.yaml are merged with default tanks."""
        monkeypatch.chdir(tmp_path)
        custom_tank = {
            "Cuve de 3000L (1 gout)": {
                "capacity": 3000,
                "transfer_loss": 100,
                "bottling_loss": 100,
                "nb_gouts": 1,
                "nominal_hL": 28.0,
            },
        }
        config = {"business": {"tanks": custom_tank}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

        result = get_business_config()

        # Custom tank present
        assert "Cuve de 3000L (1 gout)" in result["tanks"]
        assert result["tanks"]["Cuve de 3000L (1 gout)"]["capacity"] == 3000
        # Default tanks still present
        assert "Cuve de 7200L (1 go\u00fbt)" in result["tanks"]
        assert "Cuve de 5200L (1 go\u00fbt)" in result["tanks"]

    def test_default_tanks_preserved_when_not_overridden(self, tmp_path, monkeypatch):
        """When business section has no tanks key, default tanks are kept."""
        monkeypatch.chdir(tmp_path)
        config = {"business": {"ddm_days": 200}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")

        result = get_business_config()

        assert result["tanks"] == _BUSINESS_DEFAULTS["tanks"]

    def test_all_default_keys_present(self, tmp_path, monkeypatch):
        """Every key in _BUSINESS_DEFAULTS appears in the result."""
        monkeypatch.chdir(tmp_path)
        # No config.yaml at all
        result = get_business_config()

        for key in _BUSINESS_DEFAULTS:
            assert key in result, f"Missing default key: {key}"


# ---------------------------------------------------------------------------
# TestGetPaths
# ---------------------------------------------------------------------------

class TestGetPaths:

    def test_default_paths(self, tmp_path, monkeypatch):
        """Without config.yaml, get_paths returns the default tuple."""
        monkeypatch.chdir(tmp_path)

        main_table, flavor_map, images_dir = get_paths()

        assert main_table == "data/production.xlsx"
        assert flavor_map == "data/flavor_map.csv"
        assert images_dir == "assets"

    def test_custom_config_overrides_paths(self, tmp_path, monkeypatch):
        """A config.yaml with custom data_files overrides get_paths output."""
        monkeypatch.chdir(tmp_path)
        custom = {
            "data_files": {
                "main_table": "other/data.xlsx",
                "flavor_map": "other/map.csv",
            },
            "images_dir": "other_assets",
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(custom), encoding="utf-8")

        main_table, flavor_map, images_dir = get_paths()

        assert main_table == "other/data.xlsx"
        assert flavor_map == "other/map.csv"
        assert images_dir == "other_assets"

    def test_returns_tuple_of_three(self, tmp_path, monkeypatch):
        """get_paths always returns a 3-element tuple."""
        monkeypatch.chdir(tmp_path)

        result = get_paths()

        assert isinstance(result, tuple)
        assert len(result) == 3
