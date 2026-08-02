import argparse
import json
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from threading import Event

from loguru import logger

from config_loader import ConfigError, DEFAULT_CONFIG_PATH, load_config
from notify import send as notify_send
from runtime_logging import (
    SubscriptionLogManager,
    configure_logging,
    redact_sensitive_text,
)
from runtime_state import RuntimeStateStore
from scheduler import TaskScheduler
from storage import BaiduStorage, _normalize_share_reference


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_TEMPLATE_PATHS = (
    PROJECT_DIR / "config" / "config.template.json",
    PROJECT_DIR / "template" / "config.template.json",
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="百度网盘自动转存（配置文件/命令行版）"
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="配置文件路径（默认: config/config.json）",
    )
    parser.add_argument("--log-dir", help="覆盖 runtime.log_dir")
    parser.add_argument("--no-progress", action="store_true", help="关闭 tqdm 进度显示")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="从模板创建配置文件")
    init_parser.add_argument(
        "--force", action="store_true", help="备份并覆盖已经存在的配置文件"
    )

    subparsers.add_parser("validate", help="仅校验配置，不访问百度网盘")
    subparsers.add_parser("list", help="列出配置中的用户和订阅，不访问百度网盘")

    run_parser = subparsers.add_parser("run", help="立即执行订阅")
    run_parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="UID|ORDER|NAME|URL",
        help="选择一个订阅；可重复。省略时执行全部订阅",
    )

    subparsers.add_parser("daemon", help="常驻运行定时调度器；SIGHUP 重新加载配置")
    subparsers.add_parser("quota", help="立即检查当前用户的网盘容量")
    subparsers.add_parser("notify-test", help="按配置发送一条测试通知")

    inspect_parser = subparsers.add_parser(
        "inspect-share", help="读取分享链接的顶层名称，便于编写订阅配置"
    )
    inspect_parser.add_argument("url")
    inspect_parser.add_argument("--password", default=None)

    share_parser = subparsers.add_parser("share", help="为网盘路径生成分享链接")
    share_target = share_parser.add_mutually_exclusive_group(required=True)
    share_target.add_argument("--path", help="要分享的网盘绝对路径")
    share_target.add_argument(
        "--task", metavar="UID|ORDER|NAME|URL", help="分享订阅的 save_dir"
    )
    share_parser.add_argument("--password", help="4 位分享密码；省略则使用配置默认值")
    share_parser.add_argument("--period-days", type=int, help="有效天数；0 表示永久")
    return parser


def _init_config(config_path, force):
    target = Path(config_path).expanduser().resolve()
    if target.exists() and not force:
        raise ConfigError(f"配置文件已存在: {target}；如需覆盖请显式使用 --force")
    template = next((path for path in CONFIG_TEMPLATE_PATHS if path.is_file()), None)
    if template is None:
        searched = ", ".join(str(path) for path in CONFIG_TEMPLATE_PATHS)
        raise ConfigError(f"配置模板不存在；已检查: {searched}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(
            f"{target.name}.backup.{int(time.time())}.{uuid.uuid4().hex}"
        )
        shutil.copy2(target, backup)
        logger.warning("原配置已备份到: {}", backup)
    shutil.copy2(template, target)
    logger.success("配置模板已创建: {}", target)
    logger.info("请填写 baidu.users、baidu.current_user 和 baidu.tasks 后运行 validate")
    return 0


def _select_tasks(storage, selectors):
    tasks = storage.list_tasks()
    if not selectors:
        return sorted(tasks, key=lambda item: item.get("order", float("inf")))

    selected = []
    seen = set()
    for selector in selectors:
        matches = []
        if selector.isdigit():
            task = storage.get_task_by_order(int(selector))
            if task is not None:
                matches.append(task)

        for task in tasks:
            if selector in {
                str(task.get("task_uid", "")),
                str(task.get("url", "")),
                str(task.get("name", "")),
            } and task not in matches:
                matches.append(task)

        if not matches:
            raise ConfigError(
                f"未找到订阅选择器: {redact_sensitive_text(selector)}"
            )
        if len(matches) > 1:
            raise ConfigError(
                "订阅选择器不唯一: "
                f"{redact_sensitive_text(selector)}；请改用 task_uid 或 order"
            )

        task = matches[0]
        stable_key = task.get("task_uid") or f"order:{task.get('order')}"
        if stable_key not in seen:
            selected.append(task)
            seen.add(stable_key)
    return selected


def _storage(config_path, initialize_client):
    return BaiduStorage(
        config_path=config_path,
        initialize_client=initialize_client,
        create_if_missing=False,
    )


def _show_config(config_path):
    config = load_config(config_path)
    state_store = RuntimeStateStore(
        config.get("runtime", {}).get("state_file", "state/task_status.json")
    )
    baidu = config["baidu"]
    current_user = baidu.get("current_user") or "（未设置）"
    print(f"当前用户: {current_user}")
    print(f"已配置用户: {', '.join(baidu.get('users', {}).keys()) or '（无）'}")
    print("订阅:")
    tasks = sorted(baidu.get("tasks", []), key=lambda item: item.get("order", float("inf")))
    if not tasks:
        print("  （无）")
        return 0
    for task in tasks:
        schedule = task.get("cron") or "默认定时"
        runtime_state = state_store.get(task.get("task_uid")) or {}
        status = runtime_state.get("status", "未运行")
        display_name = redact_sensitive_text(task.get("name") or task.get("url"))
        print(
            f"  {task.get('order')}. {display_name} "
            f"[uid={task.get('task_uid', '首次运行时生成')}] "
            f"[schedule={schedule}] [status={status}] -> {task.get('save_dir')}"
        )
    return 0


def _run_tasks(args, runtime, log_dir):
    storage = _storage(args.config, initialize_client=False)
    selected = _select_tasks(storage, args.task)
    if not selected:
        raise ConfigError("配置中没有可执行的订阅")

    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(log_dir),
        log_dir_override=args.log_dir,
        show_progress=False if args.no_progress else None,
    )
    results = scheduler.execute_tasks(selected, flush_notifications=True)
    success_count = sum(item.get("success") and not item.get("skipped") for item in results)
    skipped_count = sum(bool(item.get("skipped")) for item in results)
    failed = [item for item in results if not item.get("success")]
    logger.info(
        "执行汇总: 成功={} 跳过={} 失败={}",
        success_count,
        skipped_count,
        len(failed),
    )
    for item in results:
        logger.info(
            "订阅结果: {} | {} | 日志={}",
            item.get("name") or item.get("order"),
            item.get("outcome") or "失败",
            item.get("log_path", "未创建"),
        )
    return 1 if failed else 0


def _run_daemon(args, runtime, log_dir):
    storage = _storage(args.config, initialize_client=True)
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(log_dir),
        log_dir_override=args.log_dir,
        show_progress=False if args.no_progress else None,
    )
    stop_event = Event()
    reload_event = Event()

    def request_stop(signum, _frame):
        logger.info("收到停止信号 {}", signum)
        stop_event.set()

    def request_reload(signum, _frame):
        logger.info("收到配置重载信号 {}", signum)
        reload_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, request_reload)

    scheduler.start()
    try:
        while not stop_event.wait(0.5):
            if reload_event.is_set():
                reload_event.clear()
                try:
                    scheduler.reload()
                except Exception as exc:
                    logger.exception("配置重载失败；请修正配置后再次发送 SIGHUP: {}", exc)
    finally:
        scheduler.stop(wait=True)
    return 0


def _quota(args, runtime, log_dir):
    storage = _storage(args.config, initialize_client=True)
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(log_dir),
        show_progress=False,
    )
    result = scheduler.check_disk_quota(send_notification=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _notify_test(args):
    storage = _storage(args.config, initialize_client=False)
    if not storage.config.get("notify", {}).get("enabled", False):
        raise ConfigError("notify.enabled=false，未发送测试通知")
    TaskScheduler(storage, show_progress=False)
    timestamp = datetime_now()
    notify_send("百度网盘自动转存", f"测试通知；发送时间: {timestamp}")
    logger.success("测试通知调用完成；请同时检查所配置渠道的接收端")
    return 0


def datetime_now():
    # Kept as a tiny seam for deterministic tests.
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def _inspect_share(args):
    try:
        url, password = _normalize_share_reference(args.url, args.password)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    storage = _storage(args.config, initialize_client=True)
    result = storage.get_share_folder_name(url, password)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


def _share(args):
    if args.path is not None and not args.path.strip():
        raise ConfigError("--path 不能为空")
    if args.password is not None and len(args.password) != 4:
        raise ConfigError("--password 必须恰好包含 4 个字符")
    if args.period_days is not None and args.period_days < 0:
        raise ConfigError("--period-days 必须是非负整数")

    storage = _storage(args.config, initialize_client=True)
    task = None
    if args.task:
        task = _select_tasks(storage, [args.task])[0]
        remote_path = task["save_dir"]
    else:
        remote_path = args.path

    share_config = storage.config.get("share", {})
    password = (
        args.password
        if args.password is not None
        else share_config.get("default_password")
    )
    period_days = (
        args.period_days
        if args.period_days is not None
        else share_config.get("default_period_days", 7)
    )
    result = storage.share_file(remote_path, password=password, period_days=period_days)
    if result.get("success") and task is not None:
        storage.update_task_share_info(task["order"], result["share_info"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        configure_logging(args.log_dir or "log", level="INFO", retention_days=14)
        try:
            return _init_config(args.config, args.force)
        except ConfigError as exc:
            logger.error(str(exc))
            return 2

    try:
        config = load_config(args.config)
        runtime = config.get("runtime", {})
        log_dir = args.log_dir or runtime.get("log_dir", "log")
        configure_logging(
            log_dir,
            level=runtime.get("log_level", "INFO"),
            retention_days=runtime.get("general_log_retention_days", 14),
        )

        if args.command == "validate":
            logger.success("配置校验通过: {}", Path(args.config).expanduser().resolve())
            return 0
        if args.command == "list":
            return _show_config(args.config)
        if args.command == "run":
            return _run_tasks(args, runtime, log_dir)
        if args.command == "daemon":
            return _run_daemon(args, runtime, log_dir)
        if args.command == "quota":
            return _quota(args, runtime, log_dir)
        if args.command == "notify-test":
            return _notify_test(args)
        if args.command == "inspect-share":
            return _inspect_share(args)
        if args.command == "share":
            return _share(args)
        parser.error(f"未知命令: {args.command}")
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.warning("操作已由用户中断")
        return 130
    except Exception as exc:
        logger.exception("命令执行失败: {}", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
