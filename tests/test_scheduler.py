import copy
import json
import threading
import time
from pathlib import Path

import pytest
from loguru import logger

import scheduler as scheduler_module
from config_loader import ConfigError
from runtime_logging import SubscriptionLogManager, configure_logging
from runtime_state import RuntimeStateStore
from scheduler import TaskScheduler
from storage import BaiduStorage


class FakeStorage:
    def __init__(self, config, outcomes=None, delay=0):
        self.config = copy.deepcopy(config)
        self.outcomes = list(outcomes or [])
        self.delay = delay
        self.status_updates = []
        self.transfer_count = 0
        self.client = object()

    def list_tasks(self):
        return self.config["baidu"]["tasks"]

    def resolve_task(self, task_ref=None, task_uid=None, order=None, url=None):
        if isinstance(task_ref, dict):
            task_uid = task_uid or task_ref.get("task_uid")
            order = order or task_ref.get("order")
            url = url or task_ref.get("url")
        elif isinstance(task_ref, int):
            order = order or task_ref
        elif isinstance(task_ref, str):
            task_uid = task_uid or task_ref
            url = url or task_ref
        for task in self.list_tasks():
            if task_uid and task.get("task_uid") == task_uid:
                return task
            if order and task.get("order") == order:
                return task
            if url and task.get("url") == url:
                return task
        return None

    def update_task_status_by_order(
        self, order, status, message=None, error=None, transferred_files=None
    ):
        self.status_updates.append((order, status, message, transferred_files))
        return True

    def transfer_share(
        self,
        url,
        pwd,
        new_files,
        save_dir,
        progress_callback,
        task_config,
    ):
        self.transfer_count += 1
        logger.debug("底层完整日志标记: {}", task_config["task_uid"])
        progress_callback("info", "【步骤1/4】访问分享链接")
        if self.delay:
            time.sleep(self.delay)
        if self.outcomes:
            return self.outcomes.pop(0)
        return {
            "success": True,
            "message": "成功转存 1/1 个文件",
            "transferred_files": [f"{task_config['task_uid']}.mp4"],
        }

    def get_user_info(self):
        return {
            "user_name": "tester",
            "quota": {"total": 100 * 1024**3, "used": 50 * 1024**3},
        }


def make_scheduler(tmp_path, valid_config, storage=None):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    fake = storage or FakeStorage(valid_config)
    scheduler = TaskScheduler(
        fake,
        log_manager=SubscriptionLogManager(tmp_path / "logs"),
        state_store=RuntimeStateStore(tmp_path / "state.json"),
        show_progress=False,
    )
    return scheduler, fake


def test_single_execution_uses_one_path_and_writes_complete_log(tmp_path, valid_config):
    scheduler, storage = make_scheduler(tmp_path, valid_config)
    result = scheduler.execute_task("task-one")

    assert result["success"] is True
    assert result["outcome"] == "成功"
    state = scheduler.state_store.get("task-one")
    assert state["status"] == "success"
    assert state["transferred_files"] == ["task-one.mp4"]
    assert storage.status_updates == []
    assert "status" not in storage.config["baidu"]["tasks"][0]
    log_path = Path(result["log_path"])
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert "订阅运行日志开始" in content
    assert "底层完整日志标记: task-one" in content
    assert "订阅执行成功" in content
    assert "订阅运行日志结束" in content
    assert "a1b2" not in content


def test_skipped_and_failed_outcomes_are_preserved(tmp_path, valid_config):
    outcomes = [
        {"success": True, "skipped": True, "message": "没有新文件需要转存"},
        {"success": False, "error": "share unavailable"},
    ]
    valid_config["baidu"]["tasks"].append(
        {
            "order": 2,
            "task_uid": "task-two",
            "name": "订阅二",
            "url": "https://pan.baidu.com/s/def_456",
            "save_dir": "/自动转存/订阅二",
        }
    )
    scheduler, storage = make_scheduler(
        tmp_path, valid_config, FakeStorage(valid_config, outcomes=outcomes)
    )

    first = scheduler.execute_task("task-one")
    second = scheduler.execute_task("task-two")
    assert first["success"] is True and first["outcome"] == "跳过"
    assert second["success"] is False and second["outcome"] == "失败"
    assert scheduler.state_store.get("task-one")["status"] == "skipped"
    assert scheduler.state_store.get("task-two")["status"] == "failed"
    assert scheduler.state_store.get("task-two")["error"] == "share unavailable"


def test_simultaneous_subscriptions_wait_instead_of_being_dropped(tmp_path, valid_config):
    valid_config["baidu"]["tasks"].append(
        {
            "order": 2,
            "task_uid": "task-two",
            "name": "订阅二",
            "url": "https://pan.baidu.com/s/def_456",
            "save_dir": "/自动转存/订阅二",
        }
    )
    storage = FakeStorage(valid_config, delay=0.05)
    scheduler, storage = make_scheduler(tmp_path, valid_config, storage)
    results = []

    threads = [
        threading.Thread(target=lambda uid=uid: results.append(scheduler.execute_task(uid)))
        for uid in ("task-one", "task-two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert storage.transfer_count == 2
    assert len(results) == 2
    assert all(result["success"] for result in results)
    for result in results:
        content = Path(result["log_path"]).read_text(encoding="utf-8")
        other_uid = "task-two" if result["task_uid"] == "task-one" else "task-one"
        assert f"底层完整日志标记: {result['task_uid']}" in content
        assert f"底层完整日志标记: {other_uid}" not in content


def test_waiting_task_never_falls_back_from_uid_to_reused_order(
    tmp_path, valid_config, monkeypatch
):
    scheduler, storage = make_scheduler(tmp_path, valid_config)
    original_resolve = storage.resolve_task
    resolve_count = 0

    def resolve_once_then_remove(task_ref=None, task_uid=None, order=None, url=None):
        nonlocal resolve_count
        resolve_count += 1
        if resolve_count == 1:
            return original_resolve(task_ref, task_uid=task_uid, order=order, url=url)
        assert task_uid == "task-one"
        assert order is None
        assert url is None
        return None

    monkeypatch.setattr(storage, "resolve_task", resolve_once_then_remove)
    result = scheduler.execute_task("task-one")

    assert result["success"] is False
    assert result["error"] == "订阅在执行前已被移除"
    assert storage.transfer_count == 0


def test_user_uid_cannot_collide_with_default_job_id_components(
    tmp_path, valid_config
):
    valid_config["baidu"]["tasks"] = [
        {
            "order": 1,
            "task_uid": "same:default:0",
            "name": "自定义计划",
            "url": "https://pan.baidu.com/s/custom_1",
            "save_dir": "/custom",
            "cron": "0 9 * * *",
        },
        {
            "order": 2,
            "task_uid": "same",
            "name": "默认计划",
            "url": "https://pan.baidu.com/s/default_2",
            "save_dir": "/default",
        },
    ]
    scheduler, _storage = make_scheduler(tmp_path, valid_config)

    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert len(job_ids) == 2
    assert "subscription:uid-same%3Adefault%3A0" in job_ids
    assert "subscription:uid-same:default:0" in job_ids


def test_notifications_are_disabled_exactly(tmp_path, valid_config, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler_module, "notify_send", lambda *args: calls.append(args))
    scheduler, _storage = make_scheduler(tmp_path, valid_config)
    scheduler.execute_task("task-one")
    assert scheduler.flush_notifications() is False
    assert calls == []


def test_enabled_notification_is_flushed_once(tmp_path, valid_config, monkeypatch):
    valid_config["notify"]["enabled"] = True
    calls = []
    monkeypatch.setattr(scheduler_module, "notify_send", lambda *args: calls.append(args))
    scheduler, _storage = make_scheduler(tmp_path, valid_config)
    scheduler.execute_task("task-one")
    assert scheduler.flush_notifications() is False
    assert len(calls) == 1


def test_quota_units_and_threshold_are_consistent(tmp_path, valid_config):
    scheduler, _storage = make_scheduler(tmp_path, valid_config)
    result = scheduler.check_disk_quota(send_notification=False)
    assert result["total_gb"] == 100.0
    assert result["used_gb"] == 50.0
    assert result["used_percent"] == 50.0
    assert result["exceeded"] is False


def test_invalid_reload_keeps_last_known_good_scheduler_running(
    tmp_path, config_file
):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(tmp_path / "logs"),
        state_store=RuntimeStateStore(tmp_path / "state.json"),
        show_progress=False,
    )
    scheduler.start()
    config_file.write_text("{invalid", encoding="utf-8")
    try:
        with pytest.raises(ConfigError):
            scheduler.reload()
        assert scheduler.scheduler.running is True
    finally:
        scheduler.stop()


def test_execution_never_writes_runtime_status_into_declarative_config(
    tmp_path, config_file, monkeypatch
):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    monkeypatch.setattr(
        storage,
        "transfer_share",
        lambda *args, **kwargs: {
            "success": True,
            "message": "完成",
            "transferred_files": ["episode.mp4"],
        },
    )
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(tmp_path / "logs"),
        state_store=RuntimeStateStore(tmp_path / "state.json"),
        show_progress=False,
    )
    before = config_file.read_bytes()
    result = scheduler.execute_task("task-one")
    after = config_file.read_bytes()

    assert result["success"] is True
    assert after == before


def test_reload_application_failure_restores_running_configuration(
    tmp_path, config_file, monkeypatch
):
    old_log_dir = tmp_path / "old-logs"
    configure_logging(old_log_dir, level="DEBUG", retention_days=0)
    initial = json.loads(config_file.read_text(encoding="utf-8"))
    initial["runtime"]["log_dir"] = str(old_log_dir)
    config_file.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    monkeypatch.setattr(storage, "_init_client", lambda: True)
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(old_log_dir),
        state_store=RuntimeStateStore(tmp_path / "state.json"),
        show_progress=False,
    )
    scheduler.start()

    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    edited = json.loads(config_file.read_text(encoding="utf-8"))
    edited["runtime"]["log_dir"] = str(blocked_parent / "logs")
    edited["baidu"]["tasks"][0].pop("task_uid")
    config_file.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

    try:
        with pytest.raises(OSError):
            scheduler.reload()
        assert scheduler.scheduler.running is True
        assert storage.config["runtime"]["log_dir"] == str(old_log_dir)
        assert json.loads(config_file.read_text(encoding="utf-8"))["runtime"][
            "log_dir"
        ] == str(blocked_parent / "logs")
        assert "task_uid" not in json.loads(
            config_file.read_text(encoding="utf-8")
        )["baidu"]["tasks"][0]
    finally:
        scheduler.stop()


def test_successful_reload_persists_generated_uid_only_after_apply(
    tmp_path, config_file, monkeypatch
):
    log_dir = tmp_path / "logs"
    configure_logging(log_dir, level="DEBUG", retention_days=0)
    initial = json.loads(config_file.read_text(encoding="utf-8"))
    initial["runtime"]["log_dir"] = str(log_dir)
    config_file.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    monkeypatch.setattr(storage, "_init_client", lambda: True)
    scheduler = TaskScheduler(
        storage,
        log_manager=SubscriptionLogManager(log_dir),
        state_store=RuntimeStateStore(tmp_path / "state.json"),
        show_progress=False,
    )
    scheduler.start()

    edited = json.loads(config_file.read_text(encoding="utf-8"))
    edited["baidu"]["tasks"][0].pop("task_uid")
    config_file.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

    try:
        scheduler.reload()
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        generated_uid = saved["baidu"]["tasks"][0]["task_uid"]
        assert len(generated_uid) == 32
        assert storage.config["baidu"]["tasks"][0]["task_uid"] == generated_uid
        assert scheduler.scheduler.running is True
    finally:
        scheduler.stop()
