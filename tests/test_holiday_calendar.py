from datetime import date

from app import app
from leave_logic import business_days_count
from models import Holiday, db
from work_calendar import holiday_for_date


def test_business_days_exclude_weekends_and_holidays():
    with app.app_context():
        Holiday.query.filter_by(date=date(2026, 9, 25)).delete()
        db.session.add(Holiday(date=date(2026, 9, 25), name="Test Holiday"))
        db.session.commit()
        assert business_days_count(date(2026, 9, 25), date(2026, 9, 28)) == 1.0


def test_state_specific_holiday_only_matches_that_state():
    with app.app_context():
        holiday = Holiday.query.filter_by(date=date(2026, 10, 2)).first()
        if holiday is None:
            holiday = Holiday(date=date(2026, 10, 2), name="State Holiday", state_code="MH")
            db.session.add(holiday)
            db.session.commit()
        assert holiday_for_date(date(2026, 10, 2), "MH") is not None
        assert holiday_for_date(date(2026, 10, 2), "KA") is None
