import hashlib
import re
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from loguru import logger
from tqdm import tqdm


LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)

_SENSITIVE_PATTERNS = (
    (
        re.compile(
            r"(?i)(BDUSS_BFESS|BDUSS|STOKEN|BAIDUID|PTOKEN|PANPSC)"
            r"(\s*[=:]\s*)[^;\s,'\"}]+"
        ),
        r"\1\2[已隐藏]",
    ),
    (re.compile(r"(?i)([?&]pwd=)[^&#\s'\"]+"), r"\1[已隐藏]"),
    (re.compile(r"(?i)(['\"]?cookies['\"]?\s*:\s*['\"])[^'\"]+"), r"\1[已隐藏]"),
)


def redact_sensitive_text(value):
    message = str(value)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


def _redact_record(record):
    record["message"] = redact_sensitive_text(record["message"])


def _console_sink(message):
    # tqdm.write keeps progress bars intact while Loguru writes from worker threads.
    tqdm.write(str(message), file=sys.stderr, end="")


def configure_logging(log_dir="log", level="INFO", retention_days=14):
    """Configure console and application logs, returning the resolved log path."""
    resolved_log_dir = Path(log_dir).expanduser().resolve()
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.configure(patcher=_redact_record)
    logger.add(
        _console_sink,
        level=level.upper(),
        colorize=False,
        format=LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )

    retention = None if retention_days == 0 else f"{retention_days} days"
    application_log = resolved_log_dir / "application_{time:YYYY-MM-DD}.log"
    logger.add(
        str(application_log),
        level="DEBUG",
        rotation="00:00",
        retention=retention,
        encoding="utf-8",
        format=LOG_FORMAT,
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )
    return resolved_log_dir


class SubscriptionLogManager:
    """Create one complete, isolated log file for every subscription run."""

    def __init__(self, log_dir="log"):
        self.root = Path(log_dir).expanduser().resolve() / "subscriptions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._sink_lock = threading.Lock()

    @contextmanager
    def capture(self, task):
        task_uid = str(task.get("task_uid") or f"order-{task.get('order', 'unknown')}")
        safe_uid = re.sub(r"[^A-Za-z0-9_.-]", "_", task_uid)
        if safe_uid != task_uid or safe_uid in {".", ".."} or len(safe_uid) > 120:
            readable_prefix = safe_uid.strip(".")[:80] or "subscription"
            uid_digest = hashlib.sha256(task_uid.encode("utf-8")).hexdigest()[:12]
            safe_uid = f"{readable_prefix}_{uid_digest}"
        run_id = uuid.uuid4().hex
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        task_dir = self.root / safe_uid
        task_dir.mkdir(parents=True, exist_ok=True)
        log_path = task_dir / f"{timestamp}_{run_id}.log"

        def only_this_run(record):
            return record["extra"].get("subscription_run_id") == run_id

        with self._sink_lock:
            sink_id = logger.add(
                str(log_path),
                level="DEBUG",
                encoding="utf-8",
                format=LOG_FORMAT,
                filter=only_this_run,
                backtrace=False,
                diagnose=False,
                enqueue=True,
            )

        try:
            with logger.contextualize(
                subscription_uid=task_uid,
                subscription_run_id=run_id,
            ):
                logger.info(
                    "订阅运行日志开始 | 名称={} | order={} | task_uid={}",
                    task.get("name") or task.get("url") or "未命名订阅",
                    task.get("order"),
                    task_uid,
                )
                try:
                    yield log_path
                finally:
                    logger.info("订阅运行日志结束 | 文件={}", log_path)
        finally:
            with self._sink_lock:
                logger.remove(sink_id)
