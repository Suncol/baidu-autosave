from loguru import logger

from runtime_logging import (
    SubscriptionLogManager,
    configure_logging,
    redact_sensitive_text,
)


def test_subscription_log_redacts_common_cookie_secrets(tmp_path):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    manager = SubscriptionLogManager(tmp_path / "logs")
    task = {"task_uid": "uid-1", "order": 1, "name": "订阅"}

    with manager.capture(task) as path:
        logger.info('cookies: "BDUSS=top-secret; STOKEN=also-secret"')

    content = path.read_text(encoding="utf-8")
    assert "top-secret" not in content
    assert "also-secret" not in content
    assert "[已隐藏]" in content


def test_url_password_and_extended_cookie_keys_are_redacted(tmp_path):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    logger.info(
        "url=https://pan.baidu.com/s/example?pwd=a1b2&from=copy "
        "BDUSS_BFESS=secret-bduss PANPSC=secret-panpsc"
    )
    logger.complete()

    content = next((tmp_path / "logs").glob("application_*.log")).read_text(
        encoding="utf-8"
    )
    assert "pwd=a1b2" not in content
    assert "secret-bduss" not in content
    assert "secret-panpsc" not in content
    assert content.count("[已隐藏]") >= 3


def test_subscription_uid_cannot_escape_log_root(tmp_path):
    configure_logging(tmp_path / "logs", level="DEBUG", retention_days=0)
    manager = SubscriptionLogManager(tmp_path / "logs")
    task = {"task_uid": "..", "order": 1, "name": "订阅"}

    with manager.capture(task) as path:
        logger.info("inside isolated log")

    assert path.resolve().is_relative_to(manager.root.resolve())
    assert path.parent != manager.root.parent


def test_sensitive_text_redaction_is_reusable_for_terminal_output():
    redacted = redact_sensitive_text(
        "https://pan.baidu.com/s/example?pwd=a1b2&from=copy"
    )
    assert "a1b2" not in redacted
    assert "pwd=[已隐藏]" in redacted
