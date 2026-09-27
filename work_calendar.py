from datetime import timedelta

from models import Holiday


def holiday_for_date(day, state_code=None):
    query = Holiday.query.filter_by(date=day)
    holidays = query.all()
    state = (state_code or "").strip().upper() or None
    for holiday in holidays:
        if holiday.state_code is None or holiday.state_code.upper() == state:
            return holiday
    return None


def is_working_day(day, state_code=None):
    return day.weekday() < 5 and holiday_for_date(day, state_code) is None


def business_days_count(start_date, end_date, state_code=None):
    if end_date < start_date:
        return 0.0
    current = start_date
    count = 0.0
    while current <= end_date:
        if is_working_day(current, state_code):
            count += 1.0
        current += timedelta(days=1)
    return count


def working_days_in_month(year, month, state_code=None):
    from calendar import monthrange
    from datetime import date

    last_day = monthrange(year, month)[1]
    return sum(
        1 for day_number in range(1, last_day + 1)
        if is_working_day(date(year, month, day_number), state_code)
    )
