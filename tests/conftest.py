import json

import pytest


@pytest.fixture
def valid_config():
    return {
        "runtime": {
            "timezone": "Asia/Shanghai",
            "progress": False,
            "log_dir": "log",
            "state_file": "state/task_status.json",
            "log_level": "INFO",
            "general_log_retention_days": 0,
        },
        "baidu": {
            "users": {"main": {"cookies": "BDUSS=secret; STOKEN=secret"}},
            "current_user": "main",
            "tasks": [
                {
                    "order": 1,
                    "task_uid": "task-one",
                    "name": "订阅一",
                    "url": "https://pan.baidu.com/s/abc_123",
                    "pwd": "a1b2",
                    "save_dir": "/自动转存/订阅一",
                }
            ],
        },
        "cron": {"default_schedule": ["0 10 * * *"]},
        "notify": {
            "enabled": False,
            "notification_delay": 0,
            "direct_fields": {},
        },
        "scheduler": {
            "max_workers": 1,
            "misfire_grace_time": 3600,
            "coalesce": True,
            "max_instances": 1,
        },
        "quota_alert": {
            "enabled": False,
            "threshold_percent": 90,
            "check_schedule": "0 0 * * *",
        },
        "share": {"default_password": "1234", "default_period_days": 7},
        "file_operations": {"rename_delay_seconds": 0.5},
    }


@pytest.fixture
def config_file(tmp_path, valid_config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config, ensure_ascii=False), encoding="utf-8")
    return path
