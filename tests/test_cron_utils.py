import pytest
from apscheduler.triggers.cron import CronTrigger

from cron_utils import (
    CronExpressionError,
    convert_cron_weekday,
    normalize_cron_expression,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("0 0 * * 0", "0 0 * * 6"),
        ("0 0 * * 7", "0 0 * * 6"),
        ("0 0 * * 1-5", "0 0 * * 0,1,2,3,4"),
        ("0 0 * * 0-2", "0 0 * * 0,1,6"),
        ("0 0 * * 0-6", "0 0 * * *"),
        ("0 0 * * */2", "0 0 * * 1,3,5,6"),
        ("0 0 * * mon-fri/2", "0 0 * * 0,2,4"),
        ("0 0 * * fri-sun", "0 0 * * 4,5,6"),
        ("0 0 * * mon,0", "0 0 * * 0,6"),
    ],
)
def test_convert_cron_weekday_exactly(source, expected):
    converted = convert_cron_weekday(source)
    assert converted == expected
    CronTrigger.from_crontab(converted)


@pytest.mark.parametrize(
    "expression",
    [
        "0 0 * *",
        "0 0 * * 8",
        "0 0 * * 5-1",
        "0 0 * * someday",
        "0 0 * * 1/2",
        "0 0 * * */0",
    ],
)
def test_invalid_or_ambiguous_weekday_fails_loudly(expression):
    with pytest.raises(CronExpressionError):
        convert_cron_weekday(expression)


def test_all_cron_fields_are_canonicalized_before_apscheduler():
    normalized = normalize_cron_expression("*/15 8-10 * jan-mar mon-fri/2")
    assert normalized == "0,15,30,45 8,9,10 * 1,2,3 0,2,4"
    CronTrigger.from_crontab(normalized)


@pytest.mark.parametrize(
    "expression",
    [
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * jan-foo *",
        "0 0 1 * mon",
    ],
)
def test_invalid_or_semantically_ambiguous_cron_fails(expression):
    with pytest.raises(CronExpressionError):
        normalize_cron_expression(expression)
