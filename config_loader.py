import json
import math
import re
from pathlib import Path

import pytz
from apscheduler.triggers.cron import CronTrigger

from cron_utils import CronExpressionError, normalize_cron_expression


DEFAULT_CONFIG_PATH = Path("config/config.json")
BAIDU_SHARE_URL_RE = re.compile(
    r"^https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+$"
)
LOG_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(ValueError):
    """Configuration is missing, malformed, or semantically invalid."""


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"JSON 中存在重复字段: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value):
    raise ConfigError(f"JSON 不允许非有限数值: {value}")


def _reject_nonfinite_values(value, location="$"):
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"JSON 不允许非有限数值: {location}={value}")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_nonfinite_values(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_values(child, f"{location}[{index}]")


def load_config(path=DEFAULT_CONFIG_PATH):
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    if config_path.stat().st_size == 0:
        raise ConfigError(f"配置文件为空: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as stream:
            config = json.load(
                stream,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
            )
    except ConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是有效 JSON: 第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}: {exc}") from exc

    validate_config(config)
    return config


def _validate_cron(value, location, errors, timezone):
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location} 必须是非空字符串")
        return
    try:
        converted = normalize_cron_expression(value)
        CronTrigger.from_crontab(converted, timezone=timezone)
    except (CronExpressionError, ValueError) as exc:
        errors.append(f"{location} 无效: {exc}")


def _validate_positive_int(mapping, key, location, errors, minimum=1):
    if key not in mapping:
        return
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{location}.{key} 必须是大于等于 {minimum} 的整数")


def validate_config(config):
    """Validate configuration without mutating it.

    An empty users/tasks setup is valid as an initial template. A configured task
    requires a usable current user because it is otherwise impossible to run.
    """
    errors = []
    if not isinstance(config, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")
    _reject_nonfinite_values(config)
    if "auth" in config:
        errors.append("auth 是已移除的 Web 登录配置，请从配置文件中删除")
    if "retry" in config:
        errors.append("retry 是未生效的旧配置，请删除；API 重试仍沿用核心实现的固定策略")
    if "regex" in config:
        errors.append("顶层 regex 是未生效的旧配置；请将规则写入各任务的 regex_pattern/regex_replace")

    runtime = config.get("runtime", {})
    if not isinstance(runtime, dict):
        errors.append("runtime 必须是对象")
        runtime = {}

    timezone_name = runtime.get("timezone", "Asia/Shanghai")
    if not isinstance(timezone_name, str) or timezone_name not in pytz.all_timezones_set:
        errors.append(f"runtime.timezone 不是有效的 IANA 时区: {timezone_name}")
        timezone = pytz.UTC
    else:
        timezone = pytz.timezone(timezone_name)

    if "progress" in runtime and not isinstance(runtime["progress"], bool):
        errors.append("runtime.progress 必须是布尔值")
    if "log_dir" in runtime and (
        not isinstance(runtime["log_dir"], str) or not runtime["log_dir"].strip()
    ):
        errors.append("runtime.log_dir 必须是非空字符串")
    if "state_file" in runtime and (
        not isinstance(runtime["state_file"], str) or not runtime["state_file"].strip()
    ):
        errors.append("runtime.state_file 必须是非空字符串")
    if "log_level" in runtime:
        level = runtime["log_level"]
        if not isinstance(level, str) or level.upper() not in LOG_LEVELS:
            errors.append(f"runtime.log_level 必须是以下值之一: {', '.join(sorted(LOG_LEVELS))}")
    _validate_positive_int(runtime, "general_log_retention_days", "runtime", errors, minimum=0)

    baidu = config.get("baidu")
    if not isinstance(baidu, dict):
        errors.append("baidu 必须是对象")
        baidu = {}

    users = baidu.get("users", {})
    if not isinstance(users, dict):
        errors.append("baidu.users 必须是对象")
        users = {}
    else:
        for username, user in users.items():
            location = f"baidu.users[{username!r}]"
            if not isinstance(username, str) or not username.strip():
                errors.append("baidu.users 的用户名必须是非空字符串")
            if not isinstance(user, dict):
                errors.append(f"{location} 必须是对象")
                continue
            cookies = user.get("cookies")
            if not isinstance(cookies, str) or not cookies.strip():
                errors.append(f"{location}.cookies 必须是非空字符串")
            else:
                cookie_keys = {
                    item.split("=", 1)[0].strip()
                    for item in cookies.split(";")
                    if "=" in item
                }
                missing_cookies = {"BDUSS", "STOKEN"} - cookie_keys
                if missing_cookies:
                    errors.append(
                        f"{location}.cookies 缺少必要字段: "
                        + ", ".join(sorted(missing_cookies))
                    )

    current_user = baidu.get("current_user")
    if current_user is not None and (
        not isinstance(current_user, str) or not current_user.strip()
    ):
        errors.append("baidu.current_user 必须是用户名字符串或 null")
    elif current_user is not None and current_user not in users:
        errors.append(f"baidu.current_user 指向不存在的用户: {current_user}")

    tasks = baidu.get("tasks", [])
    if not isinstance(tasks, list):
        errors.append("baidu.tasks 必须是数组")
        tasks = []
    if tasks and current_user is None:
        errors.append("存在订阅任务时必须设置 baidu.current_user")

    seen_orders = set()
    seen_uids = set()
    for index, task in enumerate(tasks):
        location = f"baidu.tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{location} 必须是对象")
            continue

        url = task.get("url")
        if not isinstance(url, str) or not BAIDU_SHARE_URL_RE.fullmatch(url):
            if isinstance(url, str) and re.search(r"[?&]pwd=", url, re.IGNORECASE):
                errors.append(
                    f"{location}.url 不应包含提取码；请移除 ?pwd=... 并写入 {location}.pwd"
                )
            else:
                errors.append(f"{location}.url 不是受支持的百度网盘分享链接")

        save_dir = task.get("save_dir")
        if not isinstance(save_dir, str) or not save_dir.strip():
            errors.append(f"{location}.save_dir 必须是非空字符串")

        order = task.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            errors.append(f"{location}.order 必须是正整数")
        elif order in seen_orders:
            errors.append(f"{location}.order 重复: {order}")
        else:
            seen_orders.add(order)

        task_uid = task.get("task_uid")
        if task_uid is not None:
            if not isinstance(task_uid, str) or not task_uid.strip():
                errors.append(f"{location}.task_uid 必须是非空字符串")
            elif task_uid in seen_uids:
                errors.append(f"{location}.task_uid 重复: {task_uid}")
            else:
                seen_uids.add(task_uid)

        if "pwd" in task and task["pwd"] is not None and not isinstance(task["pwd"], str):
            errors.append(f"{location}.pwd 必须是字符串或 null")
        if "name" in task and (
            not isinstance(task["name"], str) or not task["name"].strip()
        ):
            errors.append(f"{location}.name 必须是非空字符串")
        if "cron" in task:
            _validate_cron(task["cron"], f"{location}.cron", errors, timezone)

        pattern = task.get("regex_pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                errors.append(f"{location}.regex_pattern 必须是字符串")
            elif pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"{location}.regex_pattern 无效: {exc}")
        replacement = task.get("regex_replace")
        if replacement is not None and not isinstance(replacement, str):
            errors.append(f"{location}.regex_replace 必须是字符串")

    cron = config.get("cron", {})
    if not isinstance(cron, dict):
        errors.append("cron 必须是对象")
    else:
        if "auto_install" in cron:
            errors.append("cron.auto_install 已移除；daemon 直接运行内置调度器")
        schedules = cron.get("default_schedule", [])
        if isinstance(schedules, str):
            schedules = [item.strip() for item in schedules.split(";") if item.strip()]
        if not isinstance(schedules, list):
            errors.append("cron.default_schedule 必须是 cron 字符串或字符串数组")
        else:
            for index, schedule in enumerate(schedules):
                _validate_cron(
                    schedule,
                    f"cron.default_schedule[{index}]",
                    errors,
                    timezone,
                )

    scheduler = config.get("scheduler", {})
    if not isinstance(scheduler, dict):
        errors.append("scheduler 必须是对象")
    else:
        _validate_positive_int(scheduler, "max_workers", "scheduler", errors)
        _validate_positive_int(scheduler, "max_instances", "scheduler", errors)
        _validate_positive_int(scheduler, "misfire_grace_time", "scheduler", errors)
        if "coalesce" in scheduler and not isinstance(scheduler["coalesce"], bool):
            errors.append("scheduler.coalesce 必须是布尔值")

    notify = config.get("notify", {})
    if not isinstance(notify, dict):
        errors.append("notify 必须是对象")
    else:
        if "enabled" in notify and not isinstance(notify["enabled"], bool):
            errors.append("notify.enabled 必须是布尔值")
        _validate_positive_int(notify, "notification_delay", "notify", errors, minimum=0)
        for field_name in ("direct_fields", "custom_fields", "channels"):
            if field_name in notify and not isinstance(notify[field_name], dict):
                errors.append(f"notify.{field_name} 必须是对象")

    quota = config.get("quota_alert", {})
    if not isinstance(quota, dict):
        errors.append("quota_alert 必须是对象")
    else:
        if "enabled" in quota and not isinstance(quota["enabled"], bool):
            errors.append("quota_alert.enabled 必须是布尔值")
        threshold = quota.get("threshold_percent", 90)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or (isinstance(threshold, float) and not math.isfinite(threshold))
            or not 0 <= threshold <= 100
        ):
            errors.append("quota_alert.threshold_percent 必须是 0 到 100 之间的数字")
        if quota.get("enabled", False):
            _validate_cron(
                quota.get("check_schedule", "0 0 * * *"),
                "quota_alert.check_schedule",
                errors,
                timezone,
            )

    file_operations = config.get("file_operations", {})
    if not isinstance(file_operations, dict):
        errors.append("file_operations 必须是对象")
    else:
        for obsolete_key in ("batch_size", "concurrent_limit"):
            if obsolete_key in file_operations:
                errors.append(
                    f"file_operations.{obsolete_key} 是未生效的旧配置，请删除"
                )
        delay = file_operations.get("rename_delay_seconds", 0.5)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or (isinstance(delay, float) and not math.isfinite(delay))
            or delay < 0
        ):
            errors.append("file_operations.rename_delay_seconds 必须是非负数字")

    share = config.get("share", {})
    if not isinstance(share, dict):
        errors.append("share 必须是对象")
    else:
        password = share.get("default_password")
        if password not in (None, "") and (
            not isinstance(password, str) or len(password) != 4
        ):
            errors.append("share.default_password 必须为空或恰好 4 个字符")
        period = share.get("default_period_days", 7)
        if isinstance(period, bool) or not isinstance(period, int) or period < 0:
            errors.append("share.default_period_days 必须是非负整数")

    if errors:
        raise ConfigError("配置校验失败:\n- " + "\n- ".join(errors))
    return True
