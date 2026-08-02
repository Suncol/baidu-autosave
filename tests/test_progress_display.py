import progress_display
from progress_display import SubscriptionProgress


def test_progress_reports_one_exact_subscription_unit(monkeypatch):
    created = []

    class FakeBar:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.postfixes = []
            self.increments = []
            self.closed = False
            created.append(self)

        def set_postfix_str(self, value, refresh=True):
            self.postfixes.append((value, refresh))

        def update(self, amount):
            self.increments.append(amount)

        def close(self):
            self.closed = True

    monkeypatch.setattr(progress_display, "tqdm", FakeBar)
    task = {
        "order": 1,
        "url": "https://pan.baidu.com/s/example?pwd=a1b2",
    }

    with SubscriptionProgress(task, enabled=True) as progress:
        progress.update("info", "扫描目录")
        progress.finish("成功")

    bar = created[0]
    assert bar.kwargs["total"] == 1
    assert bar.kwargs["unit"] == "订阅"
    assert "a1b2" not in bar.kwargs["desc"]
    assert bar.increments == [1]
    assert bar.closed is True
