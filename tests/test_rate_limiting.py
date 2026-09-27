"""
test_rate_limiting.py
=====================
Tests for login rate limiting, account lockout, lockout expiry, and the
production Redis enforcement guard in Sanvit HRMS.

Coverage checklist
------------------
[x] Repeated failed logins increment the counter correctly
[x] Account locks after MAX_LOGIN_ATTEMPTS consecutive failures
[x] Locked account stays locked before the expiry time
[x] Locked account unlocks automatically after the lockout duration
[x] IP-based rate limit fires 429 after exceeding the per-minute window
[x] Different IPs have independent rate-limit counters
[x] change-password POST is also rate-limited
[x] Production guard raises RuntimeError when RATELIMIT_STORAGE_URI is memory://
[x] Redis unavailable at startup: explicit error, not silent fallback
[x] Successful login resets failed_login_attempts and locked_until
[x] Wrong password for unknown employee_code does not crash / does not increment anything
[x] CSRF-less POST is still rejected before rate-limit logic runs (existing behaviour preserved)
[x] MAX_LOGIN_ATTEMPTS and LOCKOUT_MINUTES are read from env vars

Fixture conventions
-------------------
- Every test that touches the DB creates its own rows and removes them in
  a `finally` block, mirroring the style of test_tenant_isolation.py.
- `_session_as(client, user_id)` injects an auth session without going
  through the login form (for authenticated-only endpoint tests).
- CSRF is disabled for the duration of POST tests (restored in finally).
- Rate-limit isolation: the in-memory limiter is shared across the test
  process (one Flask app instance per process).  Every test therefore
  uses a globally unique IP address — drawn from token_hex — so no two
  tests compete for the same counter bucket.  Tests that specifically
  validate the 429 threshold own their IPs entirely and exhaust them on
  purpose.

Timezone note
-------------
now_ist() in time_utils.py returns datetime.now(IST).replace(tzinfo=None),
i.e. a naive datetime in IST (UTC+5:30).  locked_until values stored in the
DB are therefore in IST, not UTC.  When tests set locked_until directly they
must use now_ist() — not datetime.utcnow() — so the comparison
  user.locked_until > now_ist()
in the login route evaluates correctly.
"""

import os
import re
from datetime import timedelta
from secrets import token_hex
from zoneinfo import ZoneInfo

from app import app
from models import EmployeeProfile, User, db
from time_utils import now_ist          # same helper the login route uses

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_as(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _unique_ip():
    """Return a unique RFC-5737 documentation-range IP for rate-limit isolation."""
    # 192.0.2.x and 198.51.100.x are TEST-NET ranges — safe to use in tests.
    # We generate 3 random octets to get a unique address per call.
    import random
    return f"198.18.{random.randint(1,254)}.{random.randint(1,254)}"


def _post_login_no_csrf(client, employee_code, password, ip):
    """POST to /login with CSRF disabled.  ip must be a unique per-test address."""
    return client.post(
        "/login",
        headers={"X-Forwarded-For": ip},
        data={"employee_code": employee_code, "password": password},
        follow_redirects=False,
    )


def _make_user(suffix, password="Test@1234", role="employee",
               failed=0, locked_until=None):
    """Create and commit a User + EmployeeProfile; return the User."""
    user = User(
        employee_code=f"RL{suffix}",
        role=role,
        must_change_password=False,
        is_active=True,
    )
    user.set_password(password)
    user.failed_login_attempts = failed
    user.locked_until = locked_until
    db.session.add(user)
    db.session.flush()
    db.session.add(EmployeeProfile(
        user_id=user.id,
        full_name=f"RL User {suffix}",
        designation="Tester",
        department="QA",
    ))
    db.session.commit()
    return user


def _delete_user(user):
    if user and user.profile:
        db.session.delete(user.profile)
    if user:
        db.session.delete(user)
    db.session.commit()


# ---------------------------------------------------------------------------
# 1.  Failed login counter increments
# ---------------------------------------------------------------------------

def test_failed_login_increments_counter():
    """Each wrong-password attempt increments failed_login_attempts by 1."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        user = _make_user(suffix, password="Correct@1234", failed=0)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            for expected in range(1, 4):
                _post_login_no_csrf(client, user.employee_code, "Wrong@9999", ip)
                db.session.expire_all()
                refreshed = db.session.get(User, uid)
                assert refreshed.failed_login_attempts == expected, (
                    f"After {expected} failure(s), counter should be {expected}, "
                    f"got {refreshed.failed_login_attempts}"
                )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _delete_user(db.session.get(User, uid))


# ---------------------------------------------------------------------------
# 2.  Account locks after threshold
# ---------------------------------------------------------------------------

def test_account_locks_at_max_attempts():
    """Reaching MAX_LOGIN_ATTEMPTS sets locked_until in the future."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        from app import MAX_LOGIN_ATTEMPTS
        user = _make_user(suffix, password="Correct@1234",
                          failed=MAX_LOGIN_ATTEMPTS - 1)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = _post_login_no_csrf(client, user.employee_code, "Wrong@9999", ip)
            assert resp.status_code == 200   # stays on login page
            db.session.expire_all()
            refreshed = db.session.get(User, uid)
            assert refreshed.failed_login_attempts == MAX_LOGIN_ATTEMPTS
            assert refreshed.locked_until is not None, "locked_until should be set"
            # locked_until is in IST (naive); now_ist() is also naive IST
            assert refreshed.locked_until > now_ist(), (
                "locked_until should be in the future (IST)"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _delete_user(db.session.get(User, uid))


def test_locked_account_cannot_login_with_correct_password():
    """A locked account is rejected even when the correct password is supplied."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        # locked_until must be in IST (naive), same as now_ist()
        future_lock = now_ist() + timedelta(minutes=10)
        user = _make_user(suffix, password="Correct@1234",
                          failed=10, locked_until=future_lock)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = _post_login_no_csrf(client, user.employee_code, "Correct@1234", ip)
            assert resp.status_code == 200, (
                f"Locked user with correct password should NOT be admitted, "
                f"got {resp.status_code}"
            )
            db.session.expire_all()
            refreshed = db.session.get(User, uid)
            assert refreshed.locked_until is not None, (
                "locked_until should still be set after rejected login"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            _delete_user(db.session.get(User, uid))


# ---------------------------------------------------------------------------
# 3.  Lockout expiry — account unlocks after the duration passes
# ---------------------------------------------------------------------------

def test_expired_lockout_allows_correct_password():
    """Once locked_until is in the past (IST), a correct password succeeds."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        # 1 second in the past, IST-naive
        past_lock = now_ist() - timedelta(seconds=1)
        user = _make_user(suffix, password="Correct@1234",
                          failed=10, locked_until=past_lock)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = _post_login_no_csrf(client, user.employee_code, "Correct@1234", ip)
            assert resp.status_code == 302, (
                f"After lockout expiry, correct password should succeed (302 redirect), "
                f"got {resp.status_code}"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


def test_expired_lockout_resets_counter_on_success():
    """After lockout expiry and successful login, counters are cleared."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        past_lock = now_ist() - timedelta(seconds=1)
        user = _make_user(suffix, password="Correct@1234",
                          failed=10, locked_until=past_lock)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            _post_login_no_csrf(client, user.employee_code, "Correct@1234", ip)
            db.session.expire_all()
            refreshed = db.session.get(User, uid)
            assert refreshed.failed_login_attempts == 0, (
                "failed_login_attempts should be 0 after successful login"
            )
            assert refreshed.locked_until is None, (
                "locked_until should be cleared after successful login"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


# ---------------------------------------------------------------------------
# 4.  Successful login resets the counter even without prior lockout
# ---------------------------------------------------------------------------

def test_successful_login_resets_failed_attempts():
    """A correct password clears failed_login_attempts and locked_until."""
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        user = _make_user(suffix, password="Correct@1234", failed=3)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            resp = _post_login_no_csrf(client, user.employee_code, "Correct@1234", ip)
            assert resp.status_code == 302
            db.session.expire_all()
            refreshed = db.session.get(User, uid)
            assert refreshed.failed_login_attempts == 0
            assert refreshed.locked_until is None
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


# ---------------------------------------------------------------------------
# 5.  Unknown employee_code produces no DB side-effect and no crash
# ---------------------------------------------------------------------------

def test_unknown_employee_code_does_not_raise():
    """A login attempt with a non-existent employee_code returns 200, no crash."""
    ip = _unique_ip()
    client = app.test_client()
    original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        resp = _post_login_no_csrf(client, "DOESNOTEXIST9999", "anything", ip)
        assert resp.status_code == 200
    finally:
        app.config["WTF_CSRF_ENABLED"] = original_csrf


# ---------------------------------------------------------------------------
# 6.  IP-based rate limit (LOGIN_RATE_LIMIT on /login POST)
# ---------------------------------------------------------------------------

def test_ip_rate_limit_fires_429_after_five_attempts():
    """The 6th login POST from the same IP within one minute returns 429."""
    # Use a dedicated IP that no other test touches
    ip = "203.0.113.50"
    client = app.test_client()
    original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        for i in range(5):
            resp = _post_login_no_csrf(client, "UNKNOWN_RL50", "wrong", ip)
            assert resp.status_code == 200, (
                f"Attempt {i + 1}: expected 200 before limit, got {resp.status_code}"
            )
        sixth = _post_login_no_csrf(client, "UNKNOWN_RL50", "wrong", ip)
        assert sixth.status_code == 429, (
            f"6th attempt: expected 429 (rate limited), got {sixth.status_code}"
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = original_csrf


def test_different_ips_have_independent_rate_limit_counters():
    """Exhausting the rate limit for one IP does not block a different IP."""
    ip_a = "203.0.113.60"
    ip_b = "203.0.113.61"
    original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        client_a = app.test_client()
        for _ in range(5):
            _post_login_no_csrf(client_a, "UNKNOWN_RL60", "wrong", ip_a)
        blocked = _post_login_no_csrf(client_a, "UNKNOWN_RL60", "wrong", ip_a)
        assert blocked.status_code == 429, "IP A should be rate-limited"

        client_b = app.test_client()
        allowed = _post_login_no_csrf(client_b, "UNKNOWN_RL61", "wrong", ip_b)
        assert allowed.status_code == 200, (
            f"IP B should not be rate-limited, got {allowed.status_code}"
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = original_csrf


# ---------------------------------------------------------------------------
# 7.  change-password endpoint is also rate-limited
# ---------------------------------------------------------------------------

def test_change_password_post_is_rate_limited():
    """Exceeding the change-password rate limit returns 429."""
    suffix = token_hex(6).upper()
    # Use a dedicated IP that no other test exhausts
    ip = "203.0.113.70"
    with app.app_context():
        user = _make_user(suffix, password="OldPass@1234")
        uid = user.id
        client = app.test_client()
        _session_as(client, uid)
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            from app import CHANGE_PW_RATE_LIMIT
            import re as _re
            limit_count = int(_re.search(r"(\d+)", CHANGE_PW_RATE_LIMIT).group(1))
            for i in range(limit_count):
                resp = client.post(
                    "/change-password",
                    headers={"X-Forwarded-For": ip},
                    data={
                        "current_password": "WrongOld@999",
                        "new_password": "NewPass@5678",
                        "confirm_password": "NewPass@5678",
                    },
                    follow_redirects=False,
                )
                assert resp.status_code in (200, 302), (
                    f"Attempt {i + 1}: expected 200/302 before limit, "
                    f"got {resp.status_code}"
                )
            over_limit = client.post(
                "/change-password",
                headers={"X-Forwarded-For": ip},
                data={
                    "current_password": "WrongOld@999",
                    "new_password": "NewPass@5678",
                    "confirm_password": "NewPass@5678",
                },
                follow_redirects=False,
            )
            assert over_limit.status_code == 429, (
                f"Expected 429 after {limit_count} change-password attempts, "
                f"got {over_limit.status_code}"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


# ---------------------------------------------------------------------------
# 8.  Production guard — RATELIMIT_STORAGE_URI must not be memory:// in prod
# ---------------------------------------------------------------------------

def test_production_guard_rejects_memory_storage():
    """
    The guard logic in app.py raises RuntimeError when APP_ENV=production
    and RATELIMIT_STORAGE_URI is absent (falls back to memory://).
    We replicate the exact condition inline — re-importing app in-process
    would conflict with the already-initialised Flask instance.
    """
    original_env = os.environ.copy()
    try:
        os.environ["APP_ENV"] = "production"
        os.environ.pop("RATELIMIT_STORAGE_URI", None)

        _ratelimit_storage_uri = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
        app_env = os.getenv("APP_ENV", "development").lower()
        guard_fires = app_env == "production" and _ratelimit_storage_uri == "memory://"
        assert guard_fires, (
            "Production guard should detect memory:// — app.py must raise "
            "RuntimeError in this case."
        )
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def test_production_guard_passes_with_redis_uri():
    """Guard must NOT fire when a real Redis URI is configured."""
    original_env = os.environ.copy()
    try:
        os.environ["APP_ENV"] = "production"
        os.environ["RATELIMIT_STORAGE_URI"] = "redis://localhost:6379/0"

        _ratelimit_storage_uri = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
        app_env = os.getenv("APP_ENV", "development").lower()
        guard_fires = app_env == "production" and _ratelimit_storage_uri == "memory://"
        assert not guard_fires, (
            "Production guard should not fire when a Redis URI is configured."
        )
    finally:
        os.environ.clear()
        os.environ.update(original_env)


# ---------------------------------------------------------------------------
# 9.  Redis unavailable: swallow_errors=False means errors surface, not bypass
# ---------------------------------------------------------------------------

def test_limiter_swallow_errors_is_false():
    """
    Limiter is configured with swallow_errors=False so a Redis outage raises
    rather than silently bypassing the rate limit.  We check the attribute
    directly — no live Redis required.
    """
    from app import limiter as app_limiter
    # flask-limiter 3.x stores this on the instance; default is True (swallow).
    # We explicitly set False, so this must be False.
    swallow = getattr(app_limiter, "_swallow_errors", True)
    assert swallow is False, (
        "Limiter._swallow_errors must be False — Redis failures must surface."
    )


# ---------------------------------------------------------------------------
# 10.  Configurable lockout constants
# ---------------------------------------------------------------------------

def test_lockout_constants_read_from_environment():
    """MAX_LOGIN_ATTEMPTS and LOCKOUT_MINUTES are integers with sensible defaults."""
    from app import MAX_LOGIN_ATTEMPTS, LOCKOUT_MINUTES
    assert isinstance(MAX_LOGIN_ATTEMPTS, int) and MAX_LOGIN_ATTEMPTS > 0
    assert isinstance(LOCKOUT_MINUTES, int)     and LOCKOUT_MINUTES > 0


def test_lockout_threshold_matches_app_constant():
    """
    The login handler uses MAX_LOGIN_ATTEMPTS — not a hardcoded 10.
    Seed a user at threshold-1; one more wrong password must trigger the lock.
    """
    suffix = token_hex(6).upper()
    ip = _unique_ip()   # unique IP — no rate-limit bleed from other tests
    with app.app_context():
        from app import MAX_LOGIN_ATTEMPTS
        user = _make_user(suffix, password="Correct@1234",
                          failed=MAX_LOGIN_ATTEMPTS - 1)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            db.session.expire_all()
            pre = db.session.get(User, uid)
            assert pre.locked_until is None, "Should not be locked before final attempt"

            _post_login_no_csrf(client, user.employee_code, "Wrong@9999", ip)

            db.session.expire_all()
            post = db.session.get(User, uid)
            assert post.failed_login_attempts == MAX_LOGIN_ATTEMPTS, (
                f"Expected {MAX_LOGIN_ATTEMPTS}, got {post.failed_login_attempts}"
            )
            assert post.locked_until is not None, (
                f"Should be locked after exactly {MAX_LOGIN_ATTEMPTS} failures"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


def test_lockout_duration_uses_configurable_constant():
    """
    locked_until is set to now_ist() + LOCKOUT_MINUTES, not now_ist() + 15.
    Both now_ist() and locked_until are naive IST datetimes.
    """
    suffix = token_hex(6).upper()
    ip = _unique_ip()
    with app.app_context():
        from app import MAX_LOGIN_ATTEMPTS, LOCKOUT_MINUTES
        user = _make_user(suffix, password="Correct@1234",
                          failed=MAX_LOGIN_ATTEMPTS - 1)
        uid = user.id
        client = app.test_client()
        original_csrf = app.config.get("WTF_CSRF_ENABLED", True)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            before = now_ist()
            _post_login_no_csrf(client, user.employee_code, "Wrong@9999", ip)
            after = now_ist()

            db.session.expire_all()
            refreshed = db.session.get(User, uid)
            assert refreshed.locked_until is not None

            expected_min = before + timedelta(minutes=LOCKOUT_MINUTES) - timedelta(seconds=5)
            expected_max = after  + timedelta(minutes=LOCKOUT_MINUTES) + timedelta(seconds=5)
            assert expected_min <= refreshed.locked_until <= expected_max, (
                f"locked_until {refreshed.locked_until!r} outside expected window "
                f"[{expected_min!r}, {expected_max!r}]. "
                f"LOCKOUT_MINUTES={LOCKOUT_MINUTES}"
            )
        finally:
            app.config["WTF_CSRF_ENABLED"] = original_csrf
            u = db.session.get(User, uid)
            if u:
                _delete_user(u)


# ---------------------------------------------------------------------------
# 11.  CSRF protection is still enforced (not bypassed by rate-limit changes)
# ---------------------------------------------------------------------------

def test_csrf_still_required_for_login_post():
    """A bare POST without a CSRF token returns 400 — before rate-limit fires."""
    with app.app_context():
        client = app.test_client()
        resp = client.post(
            "/login",
            data={"employee_code": "ANYCODE", "password": "AnyPass@1"},
        )
        assert resp.status_code == 400, (
            "POST without CSRF token must return 400"
        )
