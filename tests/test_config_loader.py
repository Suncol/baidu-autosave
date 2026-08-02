import copy
import json

import pytest

from config_loader import ConfigError, load_config, validate_config


def test_repository_template_is_valid():
    assert load_config("config/config.template.json")["baidu"]["tasks"] == []


def test_valid_config_passes(valid_config):
    assert validate_config(valid_config) is True


def test_removed_web_auth_config_is_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["auth"] = {"users": "admin", "password": "admin123"}
    with pytest.raises(ConfigError, match="已移除的 Web 登录配置"):
        validate_config(config)


def test_known_but_ineffective_legacy_fields_are_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["retry"] = {"max_attempts": 3}
    config["regex"] = {"enabled": True, "pattern": ".*"}
    config["cron"]["auto_install"] = True
    config["file_operations"]["batch_size"] = 50
    with pytest.raises(ConfigError) as error:
        validate_config(config)
    message = str(error.value)
    assert "retry 是未生效的旧配置" in message
    assert "顶层 regex 是未生效的旧配置" in message
    assert "cron.auto_install 已移除" in message
    assert "file_operations.batch_size" in message


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"baidu": {}, "baidu": {}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="重复字段"):
        load_config(path)


def test_task_requires_current_user(valid_config):
    config = copy.deepcopy(valid_config)
    config["baidu"]["current_user"] = None
    with pytest.raises(ConfigError, match="必须设置 baidu.current_user"):
        validate_config(config)


def test_invalid_regex_is_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["baidu"]["tasks"][0]["regex_pattern"] = "["
    with pytest.raises(ConfigError, match="regex_pattern 无效"):
        validate_config(config)


def test_password_must_be_separate_from_subscription_url(valid_config):
    config = copy.deepcopy(valid_config)
    config["baidu"]["tasks"][0]["url"] += "?pwd=a1b2"
    with pytest.raises(ConfigError, match=r"写入 baidu.tasks\[0\]\.pwd"):
        validate_config(config)


def test_duplicate_order_and_uid_are_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["baidu"]["tasks"].append(copy.deepcopy(config["baidu"]["tasks"][0]))
    with pytest.raises(ConfigError) as error:
        validate_config(config)
    assert ".order 重复" in str(error.value)
    assert ".task_uid 重复" in str(error.value)


def test_invalid_cron_and_timezone_are_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["runtime"]["timezone"] = "Mars/Olympus"
    config["cron"]["default_schedule"] = ["bad cron"]
    with pytest.raises(ConfigError) as error:
        validate_config(config)
    assert "IANA 时区" in str(error.value)
    assert "default_schedule" in str(error.value)


def test_load_reports_json_line_and_column(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{\n  "baidu":,\n}', encoding="utf-8")
    with pytest.raises(ConfigError, match="第 2 行"):
        load_config(path)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "1e999"])
def test_nonstandard_nonfinite_json_number_is_rejected(tmp_path, literal):
    path = tmp_path / "nonfinite.json"
    path.write_text(f'{{"value": {literal}}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="非有限数值"):
        load_config(path)


def test_in_memory_nonfinite_numeric_setting_is_rejected(valid_config):
    config = copy.deepcopy(valid_config)
    config["file_operations"]["rename_delay_seconds"] = float("nan")
    with pytest.raises(ConfigError, match="rename_delay_seconds"):
        validate_config(config)
