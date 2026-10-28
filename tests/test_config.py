import pytest

from openathlete.config import (
    DEFAULT_RIDE_TRAINING_MIN_KM, activity_since, configured_tzinfo,
    configured_timezone, load_config, primary_platform, ride_threshold_confirmed,
    ride_training_min_km, set_config_values, timezone_source, workout_platform,
)


def test_config_defaults_and_confirmation(tmp_path):
    config = load_config(tmp_path)
    assert ride_training_min_km(config) == DEFAULT_RIDE_TRAINING_MIN_KM
    assert not ride_threshold_confirmed(config)
    assert primary_platform(config) is None
    assert activity_since(config) is None
    assert workout_platform(config) is None
    assert configured_timezone(config)
    assert configured_tzinfo(config) is not None
    assert timezone_source(config) == "system"


def test_set_config_values_preserves_sections_and_replaces_root_keys(tmp_path):
    path = set_config_values(tmp_path, primary_platform="garmin", custom=1)
    path.write_text(path.read_text(encoding="utf-8") + "\n[sync]\nttl_s = 21600\n",
                    encoding="utf-8")
    set_config_values(tmp_path, primary_platform="coros", ride_training_min_km=18,
                      timezone="UTC", activity_since="2026-01-02",
                      workout_platform="GARMIN")
    config = load_config(tmp_path)
    assert primary_platform(config) == "coros"
    assert ride_training_min_km(config) == 18
    assert ride_threshold_confirmed(config)
    assert config["custom"] == 1
    assert config["sync"]["ttl_s"] == 21600
    assert configured_timezone(config) == "UTC"
    assert timezone_source(config) == "config"
    assert activity_since(config) == "2026-01-02"
    assert workout_platform(config) == "garmin"


def test_ride_threshold_must_be_positive(tmp_path):
    set_config_values(tmp_path, ride_training_min_km=0)
    with pytest.raises(ValueError):
        ride_training_min_km(load_config(tmp_path))


@pytest.mark.parametrize("config", [
    {"timezone": "Not/AZone"},
    {"timezone": "中国标准时间"},
    {"timezone": ""},
    {"activity_since": "2026-02-30"},
    {"workout_platform": "other"},
])
def test_new_config_values_reject_invalid(config):
    key = next(iter(config))
    if key == "timezone":
        with pytest.raises(ValueError, match="IANA"):
            configured_timezone(config)
    elif key == "activity_since":
        with pytest.raises(ValueError):
            activity_since(config)
    else:
        with pytest.raises(ValueError, match="coros"):
            workout_platform(config)


def test_cli_config_set_supports_new_root_keys(tmp_path, monkeypatch):
    from openathlete.db import athlete_db

    monkeypatch.setattr(athlete_db, "OPENATHLETE_HOME", tmp_path)
    args = type("Args", (), {
        "primary_platform": None,
        "ride_training_min_km": None,
        "timezone": "UTC",
        "activity_since": "2026-01-02",
        "workout_platform": "garmin",
    })()
    assert athlete_db.cmd_config_set(args) == 0
    config = load_config(tmp_path)
    assert configured_timezone(config) == "UTC"
    assert activity_since(config) == "2026-01-02"
    assert workout_platform(config) == "garmin"


def test_system_timezone_resolves_to_iana(monkeypatch):
    import openathlete.config as config_module

    monkeypatch.setattr(config_module, "get_localzone_name", lambda: "Asia/Shanghai")
    config = {}
    assert configured_timezone(config) == "Asia/Shanghai"
    assert configured_tzinfo(config).key == "Asia/Shanghai"
    assert timezone_source(config) == "system"


def test_unresolved_system_timezone_requires_explicit_iana(monkeypatch):
    import openathlete.config as config_module

    monkeypatch.setattr(config_module, "get_localzone_name", lambda: "中国标准时间")
    with pytest.raises(ValueError, match="系统时区.*IANA"):
        configured_timezone({})


def test_missing_timezone_database_has_actionable_error(monkeypatch):
    import openathlete.config as config_module
    from zoneinfo import ZoneInfoNotFoundError

    def missing_database(_key):
        raise ZoneInfoNotFoundError

    monkeypatch.setattr(config_module, "ZoneInfo", missing_database)
    with pytest.raises(ValueError, match="tzdata"):
        configured_timezone({"timezone": "UTC"})
