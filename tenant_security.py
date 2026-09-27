from datetime import datetime

from flask import abort
from flask_login import current_user


def is_platform_admin():
    return current_user.is_authenticated and current_user.role == "admin" and current_user.is_active


def is_demo_admin():
    return current_user.is_authenticated and current_user.role == "demo_admin" and current_user.is_active


def require_tenant_access(record):
    if not current_user.is_authenticated:
        abort(401)
    if is_platform_admin():
        return True
    if getattr(record, "tenant_id", None) != current_user.tenant_id:
        abort(403)
    return True


def ensure_tenant_active():
    if not current_user.is_authenticated:
        abort(401)
    tenant = current_user.tenant
    if tenant is None or not tenant.is_active:
        abort(403, description="This workspace is inactive.")
    if (
        tenant.subscription_status == "trial"
        and tenant.trial_end_date
        and datetime.utcnow() > tenant.trial_end_date
    ):
        abort(403, description="Your trial period has expired.")