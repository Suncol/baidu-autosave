import json

import cli
from cli import main


def test_validate_and_list_do_not_require_network(config_file, tmp_path, capsys):
    common = ["--config", str(config_file), "--log-dir", str(tmp_path / "logs")]
    assert main(common + ["validate"]) == 0
    assert main(common + ["list"]) == 0
    output = capsys.readouterr().out
    assert "订阅一" in output
    assert "BDUSS" not in output


def test_run_with_no_tasks_fails_before_network(tmp_path, valid_config):
    valid_config["baidu"]["tasks"] = []
    valid_config["baidu"]["current_user"] = None
    valid_config["baidu"]["users"] = {}
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(valid_config, ensure_ascii=False), encoding="utf-8")
    assert main(
        [
            "--config",
            str(path),
            "--log-dir",
            str(tmp_path / "logs"),
            "run",
        ]
    ) == 2


def test_init_uses_packaged_template_fallback(tmp_path, monkeypatch):
    packaged_template = tmp_path / "template" / "config.template.json"
    packaged_template.parent.mkdir()
    packaged_template.write_text('{"source": "packaged"}', encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "CONFIG_TEMPLATE_PATHS",
        (tmp_path / "missing.json", packaged_template),
    )
    target = tmp_path / "config" / "config.json"

    assert cli._init_config(target, force=False) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": "packaged"
    }
