from runtime_state import RuntimeStateStore


def test_runtime_state_is_atomic_and_separate_from_config(tmp_path):
    path = tmp_path / "state" / "task_status.json"
    store = RuntimeStateStore(path)
    task = {"task_uid": "uid-1", "order": 1, "name": "订阅"}

    store.update(task, "running", "执行中", log_path="run.log")
    store.update(
        task,
        "success",
        "完成",
        log_path="run.log",
        transferred_files=["episode.mp4"],
    )

    state = store.get("uid-1")
    assert state["status"] == "success"
    assert state["transferred_files"] == ["episode.mp4"]
    assert state["last_log_path"] == "run.log"
    assert list(path.parent.glob(".*.tmp")) == []


def test_success_clears_previous_runtime_error(tmp_path):
    store = RuntimeStateStore(tmp_path / "state.json")
    task = {"task_uid": "uid-1", "order": 1, "name": "订阅"}
    store.update(task, "failed", "失败", error="remote error")
    store.update(task, "success", "完成", transferred_files=[])
    assert "error" not in store.get("uid-1")


def test_new_run_clears_files_from_previous_success(tmp_path):
    store = RuntimeStateStore(tmp_path / "state.json")
    task = {"task_uid": "uid-1", "order": 1, "name": "订阅"}
    store.update(task, "success", "完成", transferred_files=["old.mp4"])
    store.update(task, "running", "再次执行")
    store.update(task, "failed", "远端错误", error="remote error")

    state = store.get("uid-1")
    assert state["status"] == "failed"
    assert state["transferred_files"] == []


def test_runtime_state_redacts_url_password_and_cookie_values(tmp_path):
    store = RuntimeStateStore(tmp_path / "state.json")
    task = {
        "task_uid": "uid-1",
        "order": 1,
        "url": "https://pan.baidu.com/s/example?pwd=a1b2",
    }
    store.update(task, "failed", "BDUSS=secret", error="STOKEN=secret-token")

    state = store.get("uid-1")
    serialized = str(state)
    assert "a1b2" not in serialized
    assert "secret-token" not in serialized
    assert "BDUSS=secret" not in serialized
