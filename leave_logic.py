"""
Leave proration and balance helpers.

Leave year = calendar year (Jan-Dec) for everyone.

Proration rule: if an employee's date of joining falls within the leave year
being calculated, they get a fraction of the annual quota based on the number
of months remaining in that year (inclusive of the joining month), rounded to
the nearest whole day. If they joined in an earlier year (or have no DOJ on
file yet), they get the full annual quota for that year.
"""
from datetime import date
from models import db, LeaveType, LeaveBalance, LeaveApplication


def months_remaining_in_year(doj, year):
    """Number of months from the joining month through December, inclusive."""
    if doj.year < year:
        return 12
    if doj.year > year:
        return 0
    return 12 - doj.month + 1


def prorated_quota(annual_quota, doj, year):
    """Full quota if employee joined before this year; else prorated by month, rounded."""
    if doj is None:
        return float(annual_quota)
    if doj.year < year:
        return float(annual_quota)
    if doj.year > year:
        return 0.0
    months = months_remaining_in_year(doj, year)
    raw = annual_quota * (months / 12)
    return float(round(raw))


def get_or_create_balance(user, leave_type, year):
    """
    Fetch the LeaveBalance row for this user/type/year, creating it (with the
    correct prorated allotment) if it doesn't exist yet.
    """
    bal = LeaveBalance.query.filter_by(
        user_id=user.id, leave_type_id=leave_type.id, year=year
    ).first()
    if bal:
        return bal

    doj = user.profile.date_of_joining if user.profile else None
    allotted = prorated_quota(leave_type.annual_quota, doj, year)

    bal = LeaveBalance(user_id=user.id, leave_type_id=leave_type.id, year=year,
                        allotted=allotted, used=0)
    db.session.add(bal)
    db.session.commit()
    return bal


def ensure_balances_for_user(user, year=None):
    """Make sure this user has a balance row for every active leave type this year."""
    year = year or date.today().year
    active_types = LeaveType.query.filter_by(is_active=True).all()
    balances = []
    for lt in active_types:
        balances.append(get_or_create_balance(user, lt, year))
    return balances


def recalculate_user_balances(user, year=None):
    """
    Recompute `allotted` (proration may have changed if DOJ or quota changed)
    and `used` (sum of approved applications) for every leave type, for the
    given year. Does not touch other years.
    """
    year = year or date.today().year
    doj = user.profile.date_of_joining if user.profile else None

    for lt in LeaveType.query.filter_by(is_active=True).all():
        bal = LeaveBalance.query.filter_by(
            user_id=user.id, leave_type_id=lt.id, year=year
        ).first()
        allotted = prorated_quota(lt.annual_quota, doj, year)

        if not bal:
            bal = LeaveBalance(user_id=user.id, leave_type_id=lt.id, year=year, allotted=0, used=0)
            db.session.add(bal)

        bal.allotted = allotted

        used = db.session.query(db.func.coalesce(db.func.sum(LeaveApplication.days), 0.0)).filter(
            LeaveApplication.user_id == user.id,
            LeaveApplication.leave_type_id == lt.id,
            LeaveApplication.status == "approved",
            db.extract("year", LeaveApplication.start_date) == year,
        ).scalar()
        bal.used = float(used or 0)

    db.session.commit()


def recalculate_all_balances_for_type(leave_type, year=None):
    """Used when admin edits a leave type's quota — refresh everyone's allotment."""
    from models import User
    year = year or date.today().year
    people = User.query.filter(User.role.in_(["employee", "manager"])).all()
    for person in people:
        if not person.profile:
            continue
        doj = person.profile.date_of_joining
        bal = LeaveBalance.query.filter_by(
            user_id=person.id, leave_type_id=leave_type.id, year=year
        ).first()
        allotted = prorated_quota(leave_type.annual_quota, doj, year)
        if not bal:
            bal = LeaveBalance(user_id=person.id, leave_type_id=leave_type.id,
                                year=year, allotted=allotted, used=0)
            db.session.add(bal)
        else:
            bal.allotted = allotted
    db.session.commit()


def business_days_count(start_date, end_date):
    """Simple inclusive day count (no weekend/holiday exclusion, by design choice)."""
    return float((end_date - start_date).days + 1)
