import re


class CronExpressionError(ValueError):
    """Raised when a five-field cron expression cannot be represented exactly."""


_DAY_NAME_TO_STANDARD = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
_MONTH_NAME_TO_NUMBER = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_numeric_day(value):
    try:
        day = int(value)
    except (TypeError, ValueError) as exc:
        raise CronExpressionError(f"星期字段包含非数字值: {value}") from exc

    if not 0 <= day <= 7:
        raise CronExpressionError(f"星期数字必须在 0 到 7 之间: {day}")
    return day


def _parse_day(value, sunday_as_range_end=False, range_start=None):
    lowered = value.lower()
    if lowered in _DAY_NAME_TO_STANDARD:
        day = _DAY_NAME_TO_STANDARD[lowered]
        if sunday_as_range_end and day == 0 and range_start not in (None, 0):
            return 7
        return day
    if re.search(r"[A-Za-z]", value):
        raise CronExpressionError(f"未知的星期名称: {value}")
    return _parse_numeric_day(value)


def _expand_numeric_day_item(item):
    if not item:
        raise CronExpressionError("星期字段包含空项")

    if item.count("/") > 1:
        raise CronExpressionError(f"无效的星期步长表达式: {item}")

    if "/" in item:
        base, raw_step = item.split("/", 1)
        try:
            step = int(raw_step)
        except ValueError as exc:
            raise CronExpressionError(f"星期步长必须是正整数: {item}") from exc
        if step <= 0:
            raise CronExpressionError(f"星期步长必须大于 0: {item}")
    else:
        base = item
        step = 1

    if base == "*":
        # 标准 cron 的星期字段范围为 0..7，其中 0 和 7 都代表星期日。
        values = range(0, 8, step)
    elif "-" in base:
        if base.count("-") != 1:
            raise CronExpressionError(f"无效的星期范围: {item}")
        raw_start, raw_end = base.split("-", 1)
        start = _parse_day(raw_start)
        end = _parse_day(raw_end, sunday_as_range_end=True, range_start=start)
        if start > end:
            raise CronExpressionError(
                f"不支持倒序星期范围 {base}；请用逗号拆成两个正序范围"
            )
        values = range(start, end + 1, step)
    else:
        if "/" in item:
            raise CronExpressionError(
                f"星期步长必须作用于 * 或范围，不能作用于单个值: {item}"
            )
        values = (_parse_day(base),)

    # 标准 cron: 0/7=周日, 1=周一...6=周六。
    # APScheduler: 0=周一...6=周日。
    return {(0 if day == 7 else day) - 1 for day in values}


def convert_cron_weekday(cron_exp):
    """Convert standard-cron weekday numbering to APScheduler exactly.

    Both named and numeric forms are expanded into an explicit weekday set. This
    also preserves steps on named ranges, which APScheduler 3.x otherwise parses
    without retaining the step suffix.
    """
    if not isinstance(cron_exp, str) or not cron_exp.strip():
        raise CronExpressionError("cron 表达式必须是非空字符串")

    parts = cron_exp.strip().split()
    if len(parts) != 5:
        raise CronExpressionError("cron 表达式必须包含 5 个字段")

    day_field = parts[4]
    if day_field == "*":
        return " ".join(parts)

    selected_days = set()
    for item in day_field.split(","):
        selected_days.update(_expand_numeric_day_item(item))

    # Python 的 -1 正是 APScheduler 中的周日 6。
    normalized_days = {day % 7 for day in selected_days}
    parts[4] = "*" if len(normalized_days) == 7 else ",".join(
        str(day) for day in sorted(normalized_days)
    )
    return " ".join(parts)


def _parse_bounded_value(value, minimum, maximum, names, field_name):
    lowered = value.lower()
    if names and lowered in names:
        return names[lowered]
    if re.search(r"[A-Za-z]", value):
        raise CronExpressionError(f"{field_name}包含未知名称: {value}")
    try:
        number = int(value)
    except ValueError as exc:
        raise CronExpressionError(f"{field_name}包含非数字值: {value}") from exc
    if not minimum <= number <= maximum:
        raise CronExpressionError(
            f"{field_name}必须在 {minimum} 到 {maximum} 之间: {number}"
        )
    return number


def _normalize_bounded_field(field, minimum, maximum, field_name, names=None):
    selected = set()
    for item in field.split(","):
        if not item:
            raise CronExpressionError(f"{field_name}包含空项")
        if item.count("/") > 1:
            raise CronExpressionError(f"{field_name}步长表达式无效: {item}")
        if "/" in item:
            base, raw_step = item.split("/", 1)
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise CronExpressionError(f"{field_name}步长必须是正整数: {item}") from exc
            if step <= 0:
                raise CronExpressionError(f"{field_name}步长必须大于 0: {item}")
        else:
            base = item
            step = 1

        if base == "*":
            values = range(minimum, maximum + 1, step)
        elif "-" in base:
            if base.count("-") != 1:
                raise CronExpressionError(f"{field_name}范围无效: {item}")
            raw_start, raw_end = base.split("-", 1)
            start = _parse_bounded_value(
                raw_start, minimum, maximum, names, field_name
            )
            end = _parse_bounded_value(raw_end, minimum, maximum, names, field_name)
            if start > end:
                raise CronExpressionError(f"{field_name}不支持倒序范围: {base}")
            values = range(start, end + 1, step)
        else:
            if "/" in item:
                raise CronExpressionError(
                    f"{field_name}步长必须作用于 * 或范围: {item}"
                )
            values = (
                _parse_bounded_value(base, minimum, maximum, names, field_name),
            )
        selected.update(values)

    full_range = set(range(minimum, maximum + 1))
    return "*" if selected == full_range else ",".join(
        str(value) for value in sorted(selected)
    )


def normalize_cron_expression(cron_exp):
    """Validate and canonicalize every field before APScheduler sees it.

    APScheduler uses AND when day-of-month and day-of-week are both restricted,
    while traditional cron commonly uses OR. The ambiguous combination is
    rejected instead of silently changing the user's intended schedule.
    """
    if not isinstance(cron_exp, str) or not cron_exp.strip():
        raise CronExpressionError("cron 表达式必须是非空字符串")
    parts = cron_exp.strip().split()
    if len(parts) != 5:
        raise CronExpressionError("cron 表达式必须包含 5 个字段")

    parts[0] = _normalize_bounded_field(parts[0], 0, 59, "分钟字段")
    parts[1] = _normalize_bounded_field(parts[1], 0, 23, "小时字段")
    parts[2] = _normalize_bounded_field(parts[2], 1, 31, "日期字段")
    parts[3] = _normalize_bounded_field(
        parts[3], 1, 12, "月份字段", names=_MONTH_NAME_TO_NUMBER
    )
    weekday_expression = convert_cron_weekday("0 0 * * " + parts[4])
    parts[4] = weekday_expression.split()[4]

    if parts[2] != "*" and parts[4] != "*":
        raise CronExpressionError(
            "日期字段和星期字段不能同时受限；两者在传统 cron 与 APScheduler "
            "中的 OR/AND 语义不同，请拆分需求或只限制其中一个字段"
        )
    return " ".join(parts)
