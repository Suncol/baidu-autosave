from contextlib import AbstractContextManager

from tqdm import tqdm

from runtime_logging import redact_sensitive_text


class SubscriptionProgress(AbstractContextManager):
    """Accurate task-level progress without inventing file-level percentages."""

    def __init__(self, task, enabled=True):
        self.task = task
        self.enabled = enabled
        display_name = task.get("name") or task.get("url") or f"订阅 {task.get('order', '?')}"
        self.name = redact_sensitive_text(display_name)
        self.bar = None
        self._finished = False

    def __enter__(self):
        self.bar = tqdm(
            total=1,
            desc=self.name,
            unit="订阅",
            dynamic_ncols=True,
            disable=not self.enabled,
            leave=True,
        )
        return self

    def update(self, status, message):
        if self.bar is None or not self.enabled:
            return
        text = redact_sensitive_text(message).replace("\n", " ")
        if len(text) > 100:
            text = text[:97] + "..."
        self.bar.set_postfix_str(f"{status}: {text}", refresh=True)

    def finish(self, outcome):
        if self.bar is None or self._finished:
            return
        self._finished = True
        self.bar.set_postfix_str(outcome, refresh=False)
        self.bar.update(1)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.bar is not None:
            if not self._finished:
                self.finish("异常" if exc_type else "完成")
            self.bar.close()
        return False
