import json
import os
import stat

import pytest

import storage as storage_module
from storage import BaiduStorage, _normalize_share_reference


@pytest.mark.parametrize(
    ("source", "password", "expected_url", "expected_password"),
    [
        (
            "https://pan.baidu.com/s/example?pwd=a1b2",
            None,
            "https://pan.baidu.com/s/example",
            "a1b2",
        ),
        (
            "https://pan.baidu.com/share/init?surl=example&pwd=a1b2",
            None,
            "https://pan.baidu.com/s/example",
            "a1b2",
        ),
        (
            "http://pan.baidu.com/s/example#fragment",
            "z9y8",
            "http://pan.baidu.com/s/example",
            "z9y8",
        ),
    ],
)
def test_backend_share_reference_normalization(
    source, password, expected_url, expected_password
):
    assert _normalize_share_reference(source, password) == (
        expected_url,
        expected_password,
    )


def test_backend_share_reference_rejects_password_conflict():
    with pytest.raises(ValueError, match="不一致"):
        _normalize_share_reference(
            "https://pan.baidu.com/s/example?pwd=a1b2", "z9y8"
        )


def test_custom_config_path_gets_stable_uid_and_atomic_save(tmp_path, valid_config):
    valid_config["baidu"]["tasks"][0].pop("task_uid")
    path = tmp_path / "nested" / "custom.json"
    path.parent.mkdir()
    path.write_text(json.dumps(valid_config, ensure_ascii=False), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)

    storage = BaiduStorage(
        config_path=path,
        initialize_client=False,
        create_if_missing=False,
    )

    task_uid = storage.config["baidu"]["tasks"][0]["task_uid"]
    assert len(task_uid) == 32
    assert json.loads(path.read_text(encoding="utf-8"))["baidu"]["tasks"][0][
        "task_uid"
    ] == task_uid
    assert list(path.parent.glob(".*.tmp")) == []
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_user_defined_non_hex_uid_is_resolvable(config_file):
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    assert storage.resolve_task("task-one")["order"] == 1
    assert storage.resolve_task("https://pan.baidu.com/s/abc_123")["task_uid"] == "task-one"


def test_invalid_regex_does_not_fall_back_to_original_file():
    storage = object.__new__(BaiduStorage)
    with pytest.raises(ValueError, match="无效的正则表达式"):
        storage._apply_regex_rules("episode.mp4", {"regex_pattern": "["})


def test_directory_scan_error_is_not_treated_as_empty():
    storage = object.__new__(BaiduStorage)
    storage.client = object()
    storage._normalize_path = lambda path, file_only=False: path
    storage._is_missing_path_error = lambda error: False

    def fail_listing(path, client=None):
        raise RuntimeError("network unavailable")

    storage._list_dir_entries_with_fallback = fail_listing
    with pytest.raises(RuntimeError, match="network unavailable"):
        storage.list_local_files("/target")


def test_shared_subdirectory_scan_does_not_return_partial_results():
    storage = object.__new__(BaiduStorage)
    storage.client = object()
    storage._list_shared_paths_with_retry = lambda *args, **kwargs: object()
    path = type(
        "SharedPath",
        (),
        {"path": "/folder", "uk": 1, "share_id": 2, "bdstoken": "token"},
    )()
    with pytest.raises(TypeError, match="子目录内容格式错误"):
        storage._list_shared_dir_files(path, 1, 2, "token")


def test_shared_access_is_non_interactive():
    storage = object.__new__(BaiduStorage)

    class FakeClient:
        def access_shared(self, url, password, show_vcode=True):
            assert url == "https://pan.baidu.com/s/example"
            assert password == "a1b2"
            assert show_vcode is False
            return "ok"

    assert (
        storage._access_shared_with_retry(
            "https://pan.baidu.com/s/example", "a1b2", client=FakeClient()
        )
        == "ok"
    )


def test_pan_directory_listing_retries_transient_non_json_response(monkeypatch):
    storage = object.__new__(BaiduStorage)
    storage._normalize_path = lambda path, file_only=False: path
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    class FakeResponse:
        def __init__(self, payload=None, error=None):
            self.payload = payload
            self.error = error

        def json(self):
            if self.error is not None:
                raise self.error
            return self.payload

    class FakeRawClient:
        def __init__(self):
            self.calls = []

        def _request_get(self, url, params):
            self.calls.append((url, params.copy()))
            if len(self.calls) == 1:
                return FakeResponse(error=ValueError("Expecting value at column 1"))
            return FakeResponse(payload={"errno": 0, "list": []})

    raw_client = FakeRawClient()
    client = type("FakeClient", (), {"_baidupcs": raw_client})()

    assert storage._list_dir_entries_via_pan_api("/target", client=client) == []
    assert len(raw_client.calls) == 2
    assert raw_client.calls[0] == raw_client.calls[1]


def test_pan_directory_listing_still_raises_after_retry_limit(monkeypatch):
    storage = object.__new__(BaiduStorage)
    storage._normalize_path = lambda path, file_only=False: path
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    class FakeResponse:
        def json(self):
            raise ValueError("persistent blank response")

    class FakeRawClient:
        def __init__(self):
            self.call_count = 0

        def _request_get(self, url, params):
            self.call_count += 1
            return FakeResponse()

    raw_client = FakeRawClient()
    client = type("FakeClient", (), {"_baidupcs": raw_client})()

    with pytest.raises(ValueError, match="persistent blank response"):
        storage._list_dir_entries_via_pan_api("/target", client=client)
    assert raw_client.call_count == 3


def test_transfer_retries_error_code_4_with_identical_arguments(monkeypatch):
    storage = object.__new__(BaiduStorage)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    class FakeClient:
        def __init__(self):
            self.calls = []

        def transfer_shared_paths(self, **kwargs):
            self.calls.append(kwargs.copy())
            if len(self.calls) < 3:
                raise RuntimeError("error_code: 4, message: storage unavailable")
            return "ok"

    client = FakeClient()
    expected = {
        "remotedir": "/target",
        "fs_ids": [1, 2],
        "uk": 3,
        "share_id": 4,
        "bdstoken": "token",
        "shared_url": "https://pan.baidu.com/s/example",
    }

    assert (
        storage._transfer_shared_paths_with_retry(client=client, **expected)
        == "ok"
    )
    assert client.calls == [expected, expected, expected]


def test_transfer_still_raises_after_retry_limit(monkeypatch):
    storage = object.__new__(BaiduStorage)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _seconds: None)

    class FakeClient:
        def __init__(self):
            self.call_count = 0

        def transfer_shared_paths(self, **kwargs):
            self.call_count += 1
            raise RuntimeError("error_code: 4, message: persistent failure")

    client = FakeClient()
    with pytest.raises(RuntimeError, match="persistent failure"):
        storage._transfer_shared_paths_with_retry(
            remotedir="/target",
            fs_ids=[1],
            uk=2,
            share_id=3,
            bdstoken="token",
            shared_url="https://pan.baidu.com/s/example",
            client=client,
        )
    assert client.call_count == 4


def test_success_status_clears_stale_error(config_file):
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    task = storage.config["baidu"]["tasks"][0]
    task["status"] = "error"
    task["error"] = "old failure"
    storage._save_config(update_scheduler=False)

    assert storage.update_task_status_by_order(1, "success", "转存成功") is True
    saved_task = json.loads(config_file.read_text(encoding="utf-8"))["baidu"]["tasks"][0]
    assert saved_task["status"] == "normal"
    assert "error" not in saved_task


def test_failed_status_persists_error(config_file):
    storage = BaiduStorage(
        config_path=config_file,
        initialize_client=False,
        create_if_missing=False,
    )
    assert storage.update_task_status_by_order(1, "failed", "remote error") is True
    saved_task = json.loads(config_file.read_text(encoding="utf-8"))["baidu"]["tasks"][0]
    assert saved_task["status"] == "error"
    assert saved_task["error"] == "remote error"
