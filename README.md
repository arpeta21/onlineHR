# SaarthiEV Mobility Pvt Ltd — HRMS Portal

A simple HR Management System built with Flask, with three login roles:
**Admin**, **Manager**, and **Employee**.

## How the workflow works

1. **Admin** logs in and adds a new person: full name, designation, department,
   date of joining, reporting manager, and whether they're an "Employee" or a
   "Manager" account. Saving generates a login ID (e.g. `EMP1001`) and a
   temporary password.
2. **Employee** logs in with that ID, is forced to set a new password, and
   then sees the details the admin already entered (read-only) plus a form to
   fill in everything else: date of birth, current & permanent address, PAN,
   Aadhaar, bank account/IFSC/bank name, previous company details + relieving
   letter upload, emergency contact, spouse, and children.
3. **Manager** logs in the same way. They get an extra "My team" section that
   lists everyone whose reporting manager was set to them, with full access to
   each person's complete profile (including downloading their relieving
   letter). Managers also have their own "My profile" page, since they fill in
   their own details exactly like an employee does.

## Leave management

- **Admin** manages leave types under "Leave types" — by default, Sick Leave
  (12/year), Casual Leave (12/year), and Privilege Leave (18/year) are seeded
  automatically. Quotas can be edited anytime; editing one recalculates
  everyone's balance for the current year immediately.
- Leave years run **calendar year (Jan–Dec)**, the same for everyone.
- **Proration:** if an employee joins partway through a calendar year, their
  first-year allotment for each leave type is prorated by the number of
  months remaining (including the joining month), rounded to the nearest
  whole day. From their next calendar year onward, they get the full quota.
- **Employee/Manager** see their live balances and apply for leave under "My
  leaves." Applying checks the remaining balance and blocks the request if
  there isn't enough left. Pending requests can be cancelled by the applicant.
- Requests go to the employee's **reporting manager** for approval/rejection
  under "Team leave requests." Approving deducts the days from that person's
  balance for the relevant leave type and year; rejecting or cancelling does
  not.
- **Admin** can see every request — pending, approved, rejected — under
  "Leave requests," filterable by status, and can approve/reject directly as
  an override, exactly like the manager can.

## Editing and deactivating people

- From "All people," admin can click **Edit** on anyone to change their full
  name, **Employee ID** (their login), designation, department, date of
  joining, reporting manager, or account type (Employee ↔ Manager).
- Changing someone's **reporting manager** here is how you assign or
  reassign a manager after the fact — including for people who were created
  without one. Their leave balances and history stay attached to them and
  aren't affected by the move.
- Changing the **Employee ID** changes their login immediately — the old ID
  stops working and the new one takes over, with the same password. Make
  sure to tell the person their new ID.
- A few guard rails are built in: you can't set someone as their own
  manager, you can't create a reporting loop, and you can't demote or
  deactivate a manager while people still report to them — reassign their
  team first.
- **Deactivate** (instead of delete) blocks that person's login immediately
  but keeps all their data — profile, leave history, uploaded documents —
  fully intact. **Reactivate** restores their access. This is intentional:
  a hard delete would break historical leave records and any manager's view
  of past requests from that person.

## Running it

```bash
pip install -r requirements.txt
python app.py
```

The app runs at `http://127.0.0.1:5000`.

On first run it creates a default admin account:

- **Employee ID:** `ADMIN001`
- **Password:** `Admin@123`

You'll be forced to set a new password on first login.

## Notes

- Data is stored in `instance/hrms.db` (SQLite) — created automatically.
- Uploaded relieving letters are stored in `static/uploads/`.
- Employee IDs are auto-generated sequentially as `EMP1001`, `EMP1002`, ...
- Admin can reset anyone's password back to the default (`Welcome@123`),
  which forces that person to set a new one on their next login.
- This is a development setup (Flask's built-in server). For real deployment,
  run it behind a production WSGI server (e.g. gunicorn) and change
  `SECRET_KEY` in `app.py`.

## Project structure

```
hrms/
├── app.py                 # App setup, config, login manager
├── models.py               # Database models (User, EmployeeProfile, Child,
│                            #   LeaveType, LeaveBalance, LeaveApplication)
├── routes.py                # All routes for admin/manager/employee + leaves
├── leave_logic.py           # Proration math and balance recalculation
├── requirements.txt
├── templates/
│   ├── base.html             # Sidebar layout shell
│   ├── login.html
│   ├── change_password.html
│   ├── error.html
│   ├── profile_view.html     # Shared read-only profile (admin/manager/self)
│   ├── admin/                # incl. leave_types.html, leave_requests.html
│   ├── manager/               # incl. leave_requests.html
│   └── employee/               # incl. leaves.html, apply_leave.html
└── static/
    ├── css/style.css
    └── uploads/             # Uploaded relieving letters land here
```
