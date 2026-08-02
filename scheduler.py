import argparse
import copy
import datetime
import time
from threading import Lock, Timer
from urllib.parse import quote

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from cron_utils import convert_cron_weekday, normalize_cron_expression
from config_loader import load_config
from notify import push_config as notify_push_config
from notify import send as notify_send
from progress_display import SubscriptionProgress
from runtime_logging import (
    SubscriptionLogManager,
    configure_logging,
    redact_sensitive_text,
)
from runtime_state import RuntimeStateStore
from storage import BaiduStorage
from utils import generate_transfer_notification


_NOTIFY_DEFAULTS = dict(notify_push_config)


class TaskScheduler:
    """Schedule and execute configured subscriptions through one code path."""

    instance = None

    def __init__(
        self,
        storage=None,
        log_manager=None,
        log_dir_override=None,
        state_store=None,
        show_progress=None,
    ):
        self.storage = storage or BaiduStorage()
        runtime = self.storage.config.get("runtime", {})
        timezone_name = runtime.get("timezone", "Asia/Shanghai")
        self.timezone = pytz.timezone(timezone_name)
        self._show_progress_override = show_progress
        self.show_progress = (
            runtime.get("progress", True) if show_progress is None else show_progress
        )
        self._log_dir_override = (
            str(log_dir_override) if log_dir_override is not None else None
        )
        self._state_file_override = (
            str(state_store.path) if state_store is not None else None
        )
        self.log_manager = log_manager or SubscriptionLogManager(
            runtime.get("log_dir", "log")
        )
        self.state_store = state_store or RuntimeStateStore(
            runtime.get("state_file", "state/task_status.json")
        )

        # 百度转存及配置状态写入按顺序执行。阻塞等待可确保同一时刻触发的订阅
        # 不会像旧实现那样因抢锁失败而被永久跳过。
        self._execution_lock = Lock()
        self._notification_lock = Lock()
        self._notification_timer = None
        self._notification_delay = 30
        self._notification_buffer = self._empty_results()

        self.scheduler = None
        self.is_running = False
        self.default_schedule = []
        self._init_notify()
        self._init_scheduler()
        TaskScheduler.instance = self

    @staticmethod
    def _empty_results():
        return {
            "success": [],
            "failed": [],
            "skipped": [],
            "transferred_files": {},
        }

    def _get_current_tasks(self):
        return list(self.storage.config.get("baidu", {}).get("tasks", []))

    def _get_default_schedules(self):
        configured = self.storage.config.get("cron", {}).get("default_schedule", [])
        if isinstance(configured, str):
            schedules = [part.strip() for part in configured.split(";") if part.strip()]
        else:
            schedules = [part.strip() for part in configured if part.strip()]
        return schedules

    def _trigger(self, expression):
        return CronTrigger.from_crontab(
            normalize_cron_expression(expression), timezone=self.timezone
        )

    @staticmethod
    def _job_key(task):
        task_uid = task.get("task_uid")
        if task_uid:
            # Percent-encoding is injective for the complete UID and keeps ':'
            # available exclusively for the scheduler's own ID components.
            return f"uid-{quote(str(task_uid), safe='')}"
        return f"order-{task.get('order')}"

    def _init_scheduler(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=True)

        scheduler_config = self.storage.config.get("scheduler", {})
        self.scheduler = BackgroundScheduler(
            executors={
                "default": ThreadPoolExecutor(
                    max_workers=scheduler_config.get("max_workers", 1)
                )
            },
            jobstores={"default": MemoryJobStore()},
            job_defaults={
                "coalesce": scheduler_config.get("coalesce", True),
                "max_instances": scheduler_config.get("max_instances", 1),
                "misfire_grace_time": scheduler_config.get(
                    "misfire_grace_time", 3600
                ),
            },
            timezone=self.timezone,
        )
        self.is_running = False
        self.default_schedule = self._get_default_schedules()

        custom_count = 0
        default_job_count = 0
        for task in sorted(
            self._get_current_tasks(), key=lambda item: item.get("order", float("inf"))
        ):
            task_key = self._job_key(task)
            custom_schedule = task.get("cron")
            if custom_schedule:
                self.scheduler.add_job(
                    self._execute_single_task,
                    self._trigger(custom_schedule),
                    args=[task],
                    id=f"subscription:{task_key}",
                    replace_existing=True,
                )
                custom_count += 1
                logger.info(
                    "已添加自定义定时订阅: {} -> {}",
                    task.get("name") or task.get("url"),
                    custom_schedule,
                )
                continue

            for index, schedule in enumerate(self.default_schedule):
                self.scheduler.add_job(
                    self._execute_single_task,
                    self._trigger(schedule),
                    args=[task],
                    id=f"subscription:{task_key}:default:{index}",
                    replace_existing=True,
                )
                default_job_count += 1
                logger.info(
                    "已添加默认定时订阅: {} -> {}",
                    task.get("name") or task.get("url"),
                    schedule,
                )

        self._add_quota_check_job()
        logger.info(
            "调度器初始化完成: {} 个自定义任务, {} 个默认调度实例",
            custom_count,
            default_job_count,
        )

    def start(self):
        if self.scheduler is None:
            self._init_scheduler()
        if self.scheduler.running:
            return
        self.scheduler.start()
        self.is_running = True
        now = datetime.datetime.now(self.timezone)
        logger.success(
            "调度器已启动 | 时区={} | 当前时间={}",
            self.timezone.zone,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def stop(self, wait=True):
        if self._notification_timer:
            self._notification_timer.cancel()
            self._notification_timer = None

        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
        self.is_running = False
        self.flush_notifications()
        if TaskScheduler.instance is self:
            TaskScheduler.instance = None
        logger.info("调度器已停止")

    def reload(self):
        """Reload a fully validated configuration and rebuild all jobs."""
        # Validate before stopping the active scheduler. Invalid edits are rejected
        # while the last known-good schedule continues to run.
        validated_config = load_config(self.storage.config_path)
        old_config = copy.deepcopy(self.storage.config)
        old_client = self.storage.client
        old_timezone = self.timezone
        old_show_progress = self.show_progress
        old_log_manager = self.log_manager
        old_state_store = self.state_store
        was_running = bool(self.scheduler and self.scheduler.running)
        if was_running:
            self.scheduler.shutdown(wait=True)
            self.is_running = False
        self.flush_notifications()

        try:
            self.storage.reload_config(
                initialize_client=True,
                config=validated_config,
                persist_generated_uids=False,
            )
            runtime = self.storage.config.get("runtime", {})
            self.timezone = pytz.timezone(
                runtime.get("timezone", "Asia/Shanghai")
            )
            self.show_progress = (
                runtime.get("progress", True)
                if self._show_progress_override is None
                else self._show_progress_override
            )
            log_dir = self._log_dir_override or runtime.get("log_dir", "log")
            configure_logging(
                log_dir,
                level=runtime.get("log_level", "INFO"),
                retention_days=runtime.get("general_log_retention_days", 14),
            )
            self.log_manager = SubscriptionLogManager(log_dir)
            self.state_store = RuntimeStateStore(
                self._state_file_override
                or runtime.get("state_file", "state/task_status.json")
            )
            self._init_notify()
            self._init_scheduler()
            if was_running:
                self.start()
            # Persist generated task UIDs only after every runtime component was
            # applied successfully. A failed reload must not alter the edited file.
            self.storage._save_config(update_scheduler=False)
            logger.success("配置已重新加载并通过完整校验")
        except Exception:
            # Keep the last known-good in-memory configuration operational. The
            # edited file is left untouched so the user can correct and retry it.
            self.storage.config = old_config
            self.storage.client = old_client
            self.timezone = old_timezone
            self.show_progress = old_show_progress
            self.log_manager = old_log_manager
            self.state_store = old_state_store
            old_runtime = old_config.get("runtime", {})
            old_log_dir = self._log_dir_override or old_runtime.get("log_dir", "log")
            configure_logging(
                old_log_dir,
                level=old_runtime.get("log_level", "INFO"),
                retention_days=old_runtime.get("general_log_retention_days", 14),
            )
            self._init_notify()
            self._init_scheduler()
            if was_running:
                self.start()
            logger.exception("配置应用失败，已恢复上一份可运行配置")
            raise

    def update_tasks(self):
        was_running = bool(self.scheduler and self.scheduler.running)
        self._init_scheduler()
        if was_running:
            self.start()

    def _init_notify(self):
        notify_config = self.storage.config.get("notify", {})
        self._notification_delay = notify_config.get("notification_delay", 30)

        # 重载时先恢复模块初始值，防止已删除的渠道凭据继续生效。
        notify_push_config.clear()
        notify_push_config.update(_NOTIFY_DEFAULTS)
        if not notify_config.get("enabled", False):
            logger.info("通知功能未启用")
            return

        direct_fields = notify_config.get("direct_fields")
        if isinstance(direct_fields, dict):
            notify_push_config.update(direct_fields)
        elif isinstance(notify_config.get("channels"), dict):
            pushplus = notify_config["channels"].get("pushplus", {})
            if pushplus:
                notify_push_config["PUSH_PLUS_TOKEN"] = pushplus.get("token", "")
                notify_push_config["PUSH_PLUS_USER"] = pushplus.get("topic", "")

        custom_fields = notify_config.get("custom_fields", {})
        if isinstance(custom_fields, dict):
            notify_push_config.update(custom_fields)
        logger.info("通知配置已加载，合并延迟={} 秒", self._notification_delay)

    def update_notify_config(self, notify_config):
        self.storage.config["notify"] = notify_config
        self.storage._save_config(update_scheduler=False)
        self._init_notify()

    def _progress_callback(self, task, progress):
        task_name = task.get("name") or task.get("url")

        def callback(status, message):
            level = {
                "error": "ERROR",
                "warning": "WARNING",
                "success": "SUCCESS",
                "debug": "DEBUG",
            }.get(str(status).lower(), "INFO")
            logger.log(level, "[{}] {}", task_name, message)
            progress.update(status, message)

        return callback

    def _resolve_task_reference(self, task_ref):
        task = self.storage.resolve_task(task_ref)
        if task is not None:
            return task
        if isinstance(task_ref, str):
            matches = [
                item
                for item in self._get_current_tasks()
                if item.get("name") == task_ref
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def execute_task(self, task_ref, buffer_notification=True):
        """Execute one subscription and return its complete structured result."""
        task = self._resolve_task_reference(task_ref)
        if task is None:
            return {
                "success": False,
                "error": f"未找到订阅: {redact_sensitive_text(task_ref)}",
            }

        with self.log_manager.capture(task) as log_path:
            with SubscriptionProgress(task, enabled=self.show_progress) as progress:
                if self._execution_lock.locked():
                    logger.info("已有订阅正在执行，本订阅将按顺序等待")

                with self._execution_lock:
                    # 等待期间配置可能已重载；稳定标识优先，避免 order 变化后串任务。
                    if task.get("task_uid"):
                        current_task = self.storage.resolve_task(
                            task_uid=task["task_uid"]
                        )
                    else:
                        current_task = self.storage.resolve_task(
                            order=task.get("order"),
                            url=task.get("url"),
                        )
                    if current_task is None:
                        result = {
                            "success": False,
                            "error": "订阅在执行前已被移除",
                            "task_uid": task.get("task_uid"),
                            "order": task.get("order"),
                            "name": task.get("name") or task.get("url"),
                            "outcome": "失败",
                            "log_path": str(log_path),
                        }
                        logger.error(result["error"])
                        progress.finish("失败")
                        return result

                    task_order = current_task.get("order")
                    task_name = current_task.get("name") or f"订阅 {task_order}"
                    logger.info("开始执行订阅: {}", task_name)
                    logger.info("分享链接: {}", current_task.get("url"))
                    logger.info("保存目录: {}", current_task.get("save_dir"))
                    logger.info(
                        "提取码: {}",
                        "已配置（已隐藏）" if current_task.get("pwd") else "未配置",
                    )

                    if not current_task.get("url") or not current_task.get("save_dir"):
                        result = {"success": False, "error": "订阅缺少 url 或 save_dir"}
                    else:
                        self.state_store.update(
                            current_task,
                            "running",
                            "正在执行订阅",
                            log_path=log_path,
                        )
                        try:
                            result = self.storage.transfer_share(
                                current_task["url"],
                                current_task.get("pwd"),
                                None,
                                current_task["save_dir"],
                                self._progress_callback(current_task, progress),
                                current_task,
                            )
                        except Exception as exc:
                            logger.exception("订阅执行发生未处理异常: {}", exc)
                            result = {"success": False, "error": str(exc)}

                    notification_results = self._empty_results()
                    if result.get("success") and result.get("skipped"):
                        self.state_store.update(
                            current_task,
                            "skipped",
                            "没有新文件需要转存",
                            log_path=log_path,
                        )
                        notification_results["skipped"].append(current_task)
                        outcome = "跳过"
                        logger.info("订阅无需更新: {}", task_name)
                    elif result.get("success"):
                        transferred_files = result.get("transferred_files", [])
                        self.state_store.update(
                            current_task,
                            "success",
                            result.get("message", "转存成功"),
                            log_path=log_path,
                            transferred_files=transferred_files,
                        )
                        notification_results["success"].append(current_task)
                        notification_results["transferred_files"][
                            current_task["url"]
                        ] = transferred_files
                        outcome = "成功"
                        logger.success(
                            "订阅执行成功: {} | 转存/重命名文件数={}",
                            task_name,
                            len(transferred_files),
                        )
                    else:
                        error = redact_sensitive_text(
                            result.get("error", "未知错误")
                        )
                        result = dict(result)
                        result["error"] = error
                        self.state_store.update(
                            current_task,
                            "failed",
                            error,
                            log_path=log_path,
                            error=error,
                        )
                        failed_task = dict(current_task)
                        failed_task["error"] = error
                        notification_results["failed"].append(failed_task)
                        outcome = "失败"
                        logger.error("订阅执行失败: {} | {}", task_name, error)

                    if buffer_notification:
                        self._add_to_notification_buffer(notification_results)
                    progress.finish(outcome)

                    final_result = dict(result)
                    final_result.update(
                        {
                            "task_uid": current_task.get("task_uid"),
                            "order": task_order,
                            "name": task_name,
                            "outcome": outcome,
                            "log_path": str(log_path),
                        }
                    )
                    return final_result

    def _execute_single_task(self, task):
        result = self.execute_task(task, buffer_notification=True)
        return bool(result.get("success"))

    def execute_tasks(self, tasks=None, flush_notifications=False):
        selected = tasks if tasks is not None else self._get_current_tasks()
        selected = sorted(selected, key=lambda item: item.get("order", float("inf")))
        results = [self.execute_task(task) for task in selected]
        if flush_notifications:
            self.flush_notifications()
        return results

    def _execute_task_group(self, tasks=None):
        """Backward-compatible group entry using the unified executor."""
        return self.execute_tasks(tasks=tasks, flush_notifications=False)

    def _add_to_notification_buffer(self, results):
        if not self.storage.config.get("notify", {}).get("enabled", False):
            logger.debug("通知未启用，不写入通知缓冲区")
            return
        if not (results["success"] or results["failed"]):
            return

        with self._notification_lock:
            self._notification_buffer["success"].extend(results["success"])
            self._notification_buffer["failed"].extend(results["failed"])
            self._notification_buffer["skipped"].extend(results.get("skipped", []))
            for url, files in results["transferred_files"].items():
                self._notification_buffer["transferred_files"].setdefault(url, []).extend(
                    files
                )

            if self._notification_timer:
                self._notification_timer.cancel()
                self._notification_timer = None
            send_immediately = self._notification_delay == 0
            if not send_immediately:
                self._notification_timer = Timer(
                    self._notification_delay, self.flush_notifications
                )
                self._notification_timer.daemon = True
                self._notification_timer.start()
        if send_immediately:
            logger.info("通知合并延迟为 0，立即发送本次汇总")
            self.flush_notifications()
        else:
            logger.info("通知结果已入队，将在 {} 秒后汇总发送", self._notification_delay)

    def flush_notifications(self):
        with self._notification_lock:
            if self._notification_timer:
                self._notification_timer.cancel()
                self._notification_timer = None
            if not (
                self._notification_buffer["success"]
                or self._notification_buffer["failed"]
            ):
                return False
            buffered = self._notification_buffer
            self._notification_buffer = self._empty_results()

        if not self.storage.config.get("notify", {}).get("enabled", False):
            return False
        try:
            content = generate_transfer_notification(buffered)
            if content.strip():
                notify_send("百度网盘自动追更", content)
                logger.success(
                    "汇总通知发送流程已完成: 成功={} 失败={}",
                    len(buffered["success"]),
                    len(buffered["failed"]),
                )
                return True
            logger.warning("通知内容为空，未发送")
            return False
        except Exception as exc:
            logger.exception("发送汇总通知失败: {}", exc)
            return False

    def update_task(self, task_url, cron_exp):
        task = self.storage.resolve_task(url=task_url)
        if task is None:
            logger.error("未找到订阅: {}", task_url)
            return False
        task["cron"] = cron_exp
        self.storage._save_config(update_scheduler=False)
        self.update_tasks()
        return True

    def update_default_schedule(self, schedules):
        if isinstance(schedules, str):
            schedules = [part.strip() for part in schedules.split(";") if part.strip()]
        self.storage.config.setdefault("cron", {})["default_schedule"] = schedules
        self.storage._save_config(update_scheduler=False)
        self.update_tasks()
        return True

    def remove_task(self, task_url):
        removed = self.storage.remove_task(task_url)
        if removed:
            self.update_tasks()
        return removed

    def update_task_schedule(self, task_ref, cron_exp=None):
        task = self._resolve_task_reference(task_ref)
        if task is None:
            return False
        if cron_exp is not None:
            if cron_exp:
                task["cron"] = cron_exp
            else:
                task.pop("cron", None)
            self.storage._save_config(update_scheduler=False)
        self.update_tasks()
        return True

    def sync_task_info(self, task_ref):
        return self.update_task_schedule(task_ref)

    def add_single_task(self, task, schedule=None):
        # Configuration is authoritative; rebuilding avoids stale or duplicate jobs.
        self.update_tasks()

    def _add_quota_check_job(self):
        quota_alert = self.storage.config.get("quota_alert", {})
        if not quota_alert.get("enabled", False):
            return
        schedule = quota_alert.get("check_schedule", "0 0 * * *")
        self.scheduler.add_job(
            self._check_disk_quota,
            self._trigger(schedule),
            id="quota-check",
            replace_existing=True,
        )
        logger.info("已添加网盘容量检查: {}", schedule)

    def check_disk_quota(self, send_notification=True):
        if self.storage.client is None and not self.storage._init_client():
            raise RuntimeError("百度网盘客户端未登录或初始化失败")
        user_info = self.storage.get_user_info()
        if not user_info or "quota" not in user_info:
            raise RuntimeError("无法获取百度网盘容量信息")

        quota = user_info["quota"]
        total = quota.get("total", 0)
        used = quota.get("used", 0)
        if total <= 0:
            raise RuntimeError("网盘总容量必须大于 0")

        used_percent = round(used / total * 100, 2)
        total_gb = round(total / (1024**3), 2)
        used_gb = round(used / (1024**3), 2)
        quota_config = self.storage.config.get("quota_alert", {})
        threshold = quota_config.get("threshold_percent", 90)
        exceeded = used_percent >= threshold
        result = {
            "user": user_info.get("user_name")
            or self.storage.config["baidu"].get("current_user"),
            "total_bytes": total,
            "used_bytes": used,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "used_percent": used_percent,
            "threshold_percent": threshold,
            "exceeded": exceeded,
        }
        logger.info(
            "网盘容量: {}/{} GB ({}%) | 阈值={} %",
            used_gb,
            total_gb,
            used_percent,
            threshold,
        )

        notify_enabled = self.storage.config.get("notify", {}).get("enabled", False)
        if exceeded and send_notification and notify_enabled:
            content = (
                "## 网盘容量不足\n\n"
                f"**用户**: {result['user']}  \n"
                f"**已使用**: {used_gb}GB / {total_gb}GB  \n"
                f"**使用比例**: {used_percent}%  \n"
                f"**警告阈值**: {threshold}%"
            )
            notify_send(f"网盘容量不足 - {result['user']}", content)
            logger.warning("网盘容量告警发送流程已完成")
        elif exceeded:
            logger.warning("网盘容量已超过阈值，但通知未启用或本次禁止发送")
        return result

    def _check_disk_quota(self):
        try:
            self.check_disk_quota(send_notification=True)
            return True
        except Exception as exc:
            logger.exception("检查网盘容量失败: {}", exc)
            return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="百度网盘订阅调度器（兼容入口）")
    parser.add_argument("action", choices=["start", "run-all"])
    parser.add_argument("--config", default="config/config.json")
    args = parser.parse_args(argv)

    storage = BaiduStorage(config_path=args.config, create_if_missing=False)
    scheduler = TaskScheduler(storage)
    if args.action == "run-all":
        results = scheduler.execute_tasks(flush_notifications=True)
        return 1 if any(not item.get("success") for item in results) else 0

    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
