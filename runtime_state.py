import json
import os
import time
import uuid
from pathlib import Path
from threading import RLock

from runtime_logging import redact_sensitive_text


class RuntimeStateError(RuntimeError):
    pass


class RuntimeStateStore:
    """Atomically persist execution state outside the declarative config file."""

    def __init__(self, path="state/task_status.json"):
        self.path = Path(path).expanduser().resolve()
        self._lock = RLock()

    def _read_unlocked(self):
        if not self.path.exists():
            return {"subscriptions": {}}
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeStateError(f"无法读取运行状态文件 {self.path}: {exc}") from exc
        if not isinstance(state, dict) or not isinstance(
            state.get("subscriptions", {}), dict
        ):
            raise RuntimeStateError(f"运行状态文件结构无效: {self.path}")
        state.setdefault("subscriptions", {})
        return state

    def read(self):
        with self._lock:
            return self._read_unlocked()

    def get(self, task_uid):
        return self.read()["subscriptions"].get(str(task_uid))

    def update(
        self,
        task,
        status,
        message,
        *,
        log_path=None,
        transferred_files=None,
        error=None,
    ):
        task_uid = task.get("task_uid")
        if not task_uid:
            raise RuntimeStateError("写入运行状态需要稳定的 task_uid")

        with self._lock:
            state = self._read_unlocked()
            entry = state["subscriptions"].setdefault(str(task_uid), {})
            entry.update(
                {
                    "task_uid": str(task_uid),
                    "order": task.get("order"),
                    "name": redact_sensitive_text(
                        task.get("name") or task.get("url")
                    ),
                    "status": status,
                    "message": redact_sensitive_text(message),
                    "updated_at": int(time.time()),
                }
            )
            if status != "running":
                entry["last_execute_time"] = int(time.time())
            if log_path is not None:
                entry["last_log_path"] = str(log_path)
            if transferred_files is not None:
                entry["transferred_files"] = list(transferred_files)
            elif status in {"running", "failed", "skipped"}:
                # transferred_files belongs to the current run. Keeping a prior
                # successful run's list during a new failure/skip is misleading.
                entry["transferred_files"] = []
            if error:
                entry["error"] = redact_sensitive_text(error)
            elif status in {"running", "success", "skipped"}:
                entry.pop("error", None)

            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temp_path.open("w", encoding="utf-8") as stream:
                    json.dump(state, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
            return dict(entry)
