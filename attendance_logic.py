"""
Attendance business logic module.
Handles multi-session clock-in/out, outdoor/break tracking, OT hours, off-day work, and bulk marking.
"""

from datetime import datetime, date
from models import db, Attendance, AttendanceSession, User, EmployeeProfile, PayrollSetting


def get_today_attendance(user_id):
    """Return today's attendance record for user if exists."""
    today = date.today()
    return Attendance.query.filter_by(user_id=user_id, date=today).first()


def is_active_clocked_in(attendance_rec):
    """Check if the user currently has an active open session (clocked in, not clocked out)."""
    if not attendance_rec or not attendance_rec.sessions:
        return False
    latest_session = max(attendance_rec.sessions, key=lambda s: s.id)
    return latest_session.clock_out is None


def clock_in_user(user_id, notes=None, reason=None):
    """
    Record clock-in time for user. Supports multiple sessions per day!
    If user is already clocked in without clocking out, return warning.
    """
    today = date.today()
    now = datetime.now()
    is_sunday = today.weekday() == 6

    rec = Attendance.query.filter_by(user_id=user_id, date=today).first()

    if not rec:
        status = "Late" if now.hour >= 10 and now.minute > 15 else "Present"
        if is_sunday:
            status = "Present"
        rec = Attendance(
            user_id=user_id,
            date=today,
            clock_in=now,
            status=status,
            notes=notes,
            is_off_day=is_sunday
        )
        db.session.add(rec)
        db.session.flush()
    else:
        if is_active_clocked_in(rec):
            return rec, False, "You are already clocked in. Clock out first before starting a new session."
        if not rec.clock_in:
            rec.clock_in = now

    # Create new session
    session = AttendanceSession(
        attendance_id=rec.id,
        clock_in=now,
        reason=reason or notes
    )
    db.session.add(session)

    if notes:
        rec.notes = f"{rec.notes or ''} | In ({now.strftime('%I:%M %p')}): {notes}".strip(" |")

    db.session.commit()
    return rec, True, f"Clocked in successfully at {now.strftime('%I:%M %p')}!"


def clock_out_user(user_id, notes=None):
    """
    Record clock-out time for the active session. Calculates daily hours & OT hours.
    """
    today = date.today()
    now = datetime.now()

    rec = Attendance.query.filter_by(user_id=user_id, date=today).first()
    if not rec or not rec.sessions:
        return None, False, "You haven't clocked in today yet."

    open_session = next((s for s in rec.sessions if s.clock_out is None), None)
    if not open_session:
        return rec, False, "All your clock-in sessions are already clocked out. Clock in to start a new session."

    open_session.clock_out = now
    rec.clock_out = now

    if notes:
        rec.notes = f"{rec.notes or ''} | Out ({now.strftime('%I:%M %p')}): {notes}".strip(" |")

    total_hours = rec.total_hours
    std_hours = 8.0

    if rec.is_off_day:
        rec.ot_hours = total_hours
    elif total_hours > std_hours:
        rec.ot_hours = round(total_hours - std_hours, 2)
    else:
        rec.ot_hours = 0.0

    db.session.commit()
    return rec, True, f"Clocked out successfully at {now.strftime('%I:%M %p')}! Total work today: {rec.work_duration}."


def bulk_mark_attendance(user_ids, start_date, end_date, status, notes=None):
    """
    Mark attendance for multiple users across a date range.
    """
    from datetime import timedelta
    cur = start_date
    count = 0

    while cur <= end_date:
        is_sunday = cur.weekday() == 6
        for uid in user_ids:
            rec = Attendance.query.filter_by(user_id=uid, date=cur).first()
            if rec:
                rec.status = status
                if notes:
                    rec.notes = notes
            else:
                rec = Attendance(
                    user_id=uid,
                    date=cur,
                    status=status,
                    notes=notes,
                    is_off_day=is_sunday
                )
                db.session.add(rec)
            count += 1
        cur += timedelta(days=1)

    db.session.commit()
    return count, f"Attendance marked for {len(user_ids)} employee(s) across {count} record(s)."


def get_user_monthly_attendance(user_id, month=None, year=None):
    """Retrieve all attendance records and totals for a given month/year."""
    today = date.today()
    month = month or today.month
    year = year or today.year

    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    records = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= start_date,
        Attendance.date < end_date
    ).order_by(Attendance.date.desc()).all()

    present_count = sum(1 for r in records if r.status in ["Present", "Late"])
    late_count = sum(1 for r in records if r.status == "Late")
    absent_count = sum(1 for r in records if r.status == "Absent")
    half_day_count = sum(1 for r in records if r.status == "Half Day")
    total_ot_hours = sum(r.ot_hours or 0.0 for r in records)

    return {
        "records": records,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "half_day_count": half_day_count,
        "total_ot_hours": round(total_ot_hours, 2),
        "month": month,
        "year": year
    }


def get_user_full_month_calendar(user_id, month=None, year=None):
    """
    Returns a complete day-by-day calendar matrix for the entire month (1st to last day).
    Maps Attendance records, Leave applications, and Backdated Regularizations.
    """
    import calendar
    today = date.today()
    month = month or today.month
    year = year or today.year

    last_day = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    from models import LeaveApplication, AttendanceRegularization

    att_recs = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).all()
    att_map = {r.date: r for r in att_recs}

    reg_recs = AttendanceRegularization.query.filter(
        AttendanceRegularization.user_id == user_id,
        AttendanceRegularization.date >= start_date,
        AttendanceRegularization.date <= end_date
    ).all()
    reg_map = {r.date: r for r in reg_recs}

    leaves = LeaveApplication.query.filter(
        LeaveApplication.user_id == user_id,
        LeaveApplication.status == "approved",
        LeaveApplication.start_date <= end_date,
        LeaveApplication.end_date >= start_date
    ).all()
    leave_dates = set()
    for l in leaves:
        cur = max(start_date, l.start_date)
        l_end = min(end_date, l.end_date)
        from datetime import timedelta
        while cur <= l_end:
            leave_dates.add(cur)
            cur += timedelta(days=1)

    calendar_days = []
    present_count = 0
    late_count = 0
    absent_count = 0
    half_day_count = 0
    missed_count = 0

    for d in range(1, last_day + 1):
        cur_d = date(year, month, d)
        att = att_map.get(cur_d)
        reg = reg_map.get(cur_d)
        is_sunday = cur_d.weekday() == 6
        is_future = cur_d > today

        display_status = "Not Marked"

        if att:
            display_status = att.status
            if att.status in ["Present", "Late"]:
                present_count += 1
                if att.status == "Late":
                    late_count += 1
            elif att.status == "Half Day":
                half_day_count += 1
            elif att.status == "Absent":
                absent_count += 1
        elif cur_d in leave_dates:
            display_status = "On Leave"
        elif is_sunday:
            display_status = "Off Day / Sunday"
        elif is_future:
            display_status = "Upcoming"
        else:
            display_status = "Missed / Not Marked"
            missed_count += 1

        calendar_days.append({
            "date": cur_d,
            "day_num": d,
            "day_name": cur_d.strftime("%a"),
            "attendance": att,
            "regularization": reg,
            "display_status": display_status,
            "is_sunday": is_sunday,
            "is_future": is_future
        })

    return {
        "calendar_days": calendar_days,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "half_day_count": half_day_count,
        "missed_count": missed_count,
        "month": month,
        "year": year,
        "month_name": calendar.month_name[month]
    }


def apply_attendance_regularization(user_id, date_obj, clock_in_time, clock_out_time, status, reason):
    """
    Apply for backdated attendance regularization.
    Founding members auto-approve instantly!
    """
    from models import AttendanceRegularization
    user = User.query.get(user_id)
    if not user:
        return None, False, "User not found."

    is_founding = user.profile.is_founding_member if user.profile else False
    req_status = "approved" if is_founding else "pending"

    reg = AttendanceRegularization(
        user_id=user_id,
        approver_id=user_id if is_founding else (user.profile.reporting_manager_id if user.profile else None),
        date=date_obj,
        requested_clock_in=clock_in_time,
        requested_clock_out=clock_out_time,
        requested_status=status or "Present",
        reason=reason,
        status=req_status,
        is_self_approved=is_founding,
        decided_at=datetime.utcnow() if is_founding else None,
        decision_note="Auto self-approved (Founding Member)" if is_founding else None
    )
    db.session.add(reg)
    db.session.flush()

    if is_founding:
        att = Attendance.query.filter_by(user_id=user_id, date=date_obj).first()
        if not att:
            att = Attendance(
                user_id=user_id,
                date=date_obj,
                clock_in=clock_in_time,
                clock_out=clock_out_time,
                status=status or "Present",
                notes=f"Regularized: {reason}"
            )
            db.session.add(att)
        else:
            att.clock_in = clock_in_time or att.clock_in
            att.clock_out = clock_out_time or att.clock_out
            att.status = status or att.status
            att.notes = f"{att.notes or ''} | Regularized: {reason}".strip(" |")

    db.session.commit()

    if is_founding:
        msg = f"Backdated attendance for {date_obj.strftime('%d-%b-%Y')} auto-approved & updated (Founding Member status)."
    else:
        msg = f"Backdated attendance request for {date_obj.strftime('%d-%b-%Y')} submitted to manager for approval."

    return reg, True, msg


def decide_attendance_regularization(reg_id, approver_id, decision, decision_note=None):
    """
    Approve or reject a backdated attendance request.
    Upon approval, creates or updates the Attendance record!
    """
    from models import AttendanceRegularization
    reg = AttendanceRegularization.query.get(reg_id)
    if not reg:
        return None, False, "Request not found."

    if reg.status != "pending":
        return reg, False, "This request has already been decided."

    reg.status = decision
    reg.approver_id = approver_id
    reg.decided_at = datetime.utcnow()
    reg.decision_note = decision_note

    if decision == "approved":
        att = Attendance.query.filter_by(user_id=reg.user_id, date=reg.date).first()
        if not att:
            att = Attendance(
                user_id=reg.user_id,
                date=reg.date,
                clock_in=reg.requested_clock_in,
                clock_out=reg.requested_clock_out,
                status=reg.requested_status or "Present",
                notes=f"Regularized by Manager: {reg.reason}"
            )
            db.session.add(att)
        else:
            att.clock_in = reg.requested_clock_in or att.clock_in
            att.clock_out = reg.requested_clock_out or att.clock_out
            att.status = reg.requested_status or att.status
            att.notes = f"{att.notes or ''} | Approved Regularization: {reg.reason}".strip(" |")

    db.session.commit()
    return reg, True, f"Backdated attendance request {decision} successfully."

