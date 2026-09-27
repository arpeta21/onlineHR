# HRMS Security Audit Report

Audit date: 2026-09-24
Scope: Flask HRMS application and the supplied security checklist.

## Executive Summary

The application has useful baseline controls: password hashing, CSRF protection, role decorators, tenant fields on many models, private upload storage, file signature checks, security headers, and audit events. Module 1 startup/configuration fixes have been applied and verified, but the application is **not production-ready** until the remaining critical/high findings below are addressed and the failing tests are updated or fixed.

The existing suite currently reports 38 passing tests and 9 failures. Several failures assert legacy behavior that the current statutory implementation intentionally changed, but the authorization/export failures require remediation rather than being dismissed.

## Findings

| ID | Severity | Area | Finding | Impact | Recommended fix |
|---|---|---|---|---|---|
| SEC-001 | Critical | Secrets | `app.py` prints the reset admin password from the `reset-admin` CLI command. | Password disclosure through CI, shell history, process logs, or hosted logs. | Never print passwords. Deliver through an interactive secure channel or require the operator to set the password directly. |
| SEC-002 | High | Authentication | Admin password reset renders the temporary password in an HTTP response at `admin/reset_password_result.html`; employee creation also flashes a temporary password. | Temporary credentials can be captured by browser history, screenshots, proxy logs, or shared links. | Show the secret once only in a controlled admin flow, or deliver it through an out-of-band channel. Do not put it in flash messages or persistent responses. |
| SEC-003 | High | RBAC | `role_required("admin")` automatically adds `demo_admin` to the allowed roles. | Every route using only this decorator must be separately reviewed; a missed `real_admin_only()` call grants demo-admin access to admin functions. | Make `admin` and `demo_admin` explicit roles, or create separate decorators such as `real_admin_required` and `demo_admin_required`. Default deny. |
| SEC-004 | High | Tenant isolation | Payroll component/CTC paths and several query paths use global queries (`PayComponent.query`, `EmployeeCTC.query`) without consistent tenant filters. | Cross-tenant salary structures, components, or calculations can leak or be modified. | Enforce tenant scope in model services and every lookup; add cross-tenant tests for payroll, reports, appraisals, recruitment, documents, and policies. |
| SEC-005 | High | Rate limiting | Flask-Limiter defaults to `memory://`. | Limits do not coordinate across Gunicorn workers and can be bypassed by spreading requests across workers. | Configure Redis or another shared limiter backend in production and test multi-worker behavior. |
| SEC-006 | High | File security | Upload validation checks extension/signature but does not provide malware scanning, archive-bomb protection, PDF/DOCX page limits, or parser resource limits. | Malicious or oversized documents can cause storage, CPU, or memory denial of service. | Add upload quotas, decompression limits, parser timeouts, page/character limits, and antivirus scanning. |
| SEC-007 | High | Exports | Styled XLSX generation writes user-controlled cell values directly. Formula-like values beginning with `=`, `+`, `-`, or `@` are not neutralized. | Spreadsheet formula injection when reports are opened in Excel. | Prefix dangerous values with an apostrophe or write all untrusted export values as safe text. Add formula-injection tests. |
| SEC-008 | High | Authorization | Multiple direct `get_or_404`/query lookups must be reviewed individually; the test suite reports authorization failures for manager report export and demo-admin report/recruitment access. | IDOR, cross-role access, or cross-tenant data exposure. | Scope objects by actor and tenant before lookup; return 403/404 consistently; fix the failing authorization tests before release. |
| SEC-009 | Medium | CSP | CSP allows `unsafe-inline` and loads Chart.js from a third-party CDN without an integrity hash. | XSS impact is increased and a CDN compromise can affect the application. | Move scripts/styles to static files, use nonces/hashes, add SRI, and restrict third-party origins. |
| SEC-010 | Medium | Proxy/session | `ProxyFix(x_for=1, x_proto=1, x_host=1)` trusts one proxy hop without deployment verification. | Incorrect proxy topology can permit spoofed client IP/protocol/host values. | Configure trusted proxy count per deployment and test forwarded-header behavior. |
| SEC-011 | Medium | Database lifecycle | Startup calls `db.create_all()` while Flask-Migrate is also configured. | Concurrent workers can race on startup; schema changes are not safely versioned by deployment. | Run migrations as a single release step and remove production startup schema mutation. |
| SEC-012 | Medium | Data minimization | Reports and payroll input exports include sensitive fields such as PAN, Aadhaar, bank account, and IFSC, even where the report purpose may not require them. | Unnecessary sensitive-data distribution and retention. | Use report-specific allowlists, masking, authorization, expiry, and audit logging. |
| SEC-013 | Medium | Business logic | Payroll, leave, appraisal, and recruitment workflows need concurrency/idempotency tests beyond current coverage. | Double approvals, duplicate deductions, duplicate payroll, or repeated submissions may create inconsistent records. | Add transaction-level tests with concurrent requests and unique constraints for workflow identities. |
| SEC-014 | Low | Test maintenance | Existing tests still expect the old PF ceiling and the removed high-income TDS `NotImplementedError`; export tests expect CSV after the export format was changed to XLSX. | CI is red and can conceal real regressions. | Update tests to the intended statutory/export contract, then restore a green baseline. |

## Module 1 Remediation Completed

- Production debug mode now defaults to off and requires explicit `APP_DEBUG` plus development mode to enable it.
- Production startup rejects missing `DATABASE_URL`, rejects SQLite unless explicitly opted in, and validates `DATA_ENCRYPTION_KEY`.
- Database initialization failures are logged server-side and fail with a generic startup error in production.
- The admin reset command no longer prints generated or supplied passwords.
- A `/health` endpoint checks database reachability without exposing configuration.
- Production 500/413 responses are handled with generic messages and an incident reference for 500 errors.
- `UPLOAD_DIR` is honored by the application so the configured persistent deployment disk is used.
- Development schema checks found no missing model tables; login and stylesheet endpoints returned HTTP 200.

## Tenant Isolation Remediation Completed

- Published resource downloads now reject cross-tenant resource IDs for non-platform admins.
- Candidate resume, edit, delete, offer, and offer-letter routes now use a shared tenant-and-owner authorization helper.
- Demo-admin interview listings are tenant-filtered.
- Interview feedback validates candidate tenant ownership and manager interviewer ownership before mutation.
- Existing HR letter mutation IDs are tenant-validated before a letter can be rewritten.
- Appraisal meeting employee IDs are tenant-validated and meetings are stamped with the actor tenant.
- Recruitment screening, requisition approval, interview scheduling, appraisal manager decisions, and special-approval manager decisions now enforce tenant equality at the service boundary.
- Added `tests/test_tenant_isolation.py`; its cross-tenant resource, candidate, and offer access test passes.

## Tenant Isolation Final Status

**Complete for non-platform actors across the reviewed application paths.** Direct URL access, POST mutations, recruitment objects, resources, candidate/offer access, interview feedback, appraisal access, special approvals, attendance regularization, HR letter mutation, and tenant-scoped exports now enforce tenant/object authorization. The expanded `tests/test_tenant_isolation.py` passes.

Real platform administrators intentionally retain global visibility for platform-wide reports and administration. That is an explicit role boundary, not a cross-tenant bypass for tenant users. No separate JSON API or background-worker implementation was found in this audit.

## Verified Controls

- Passwords use Werkzeug password hashing rather than plaintext storage.
- Temporary-password first-login flow exists through `must_change_password`.
- Generic login failure messaging is used.
- CSRFProtect is enabled globally and most forms include tokens.
- Production cookie flags set HttpOnly, SameSite, and Secure based on environment.
- Upload paths use generated filenames and private upload storage.
- Several sensitive fields are encrypted/masked.
- Manager KYC masking and sensitive-document restrictions are present.
- Leave holiday calculations are state-aware and existing holiday tests pass.
- Appraisal workflow state checks and demo-admin ownership checks are present.

## Test Status

The current full suite is not green. Reported failures include:

- Manager report export authorization.
- Demo-admin report/recruitment authorization.
- Login lockout counter expectation.
- Attendance late-threshold behavior.
- Payroll input export contract.
- PF ceiling expectations after the statutory change.
- High-income new-regime TDS expectation after surcharge support.
- Automatic CTC/PF expectations after the ceiling change.

The full suite currently reports 35 passed and 8 failed. The tenant-isolation regression and manager authorization test pass; remaining failures concern legacy payroll export/statutory/attendance expectations and one demo-report test.

## Production Readiness

**Status: Not ready for production.** Module 1 startup/configuration is materially improved, but SEC-003 through SEC-008 and the remaining deployment/security controls still require remediation and verification.

Immediate release blockers are SEC-001 through SEC-008, followed by a green authorization/security test baseline, shared production rate limiting, upload malware/resource controls, and a migration-only deployment process.

## Manual Verification Still Required

The following require deployment or infrastructure access and were not fully verifiable from the local workspace: HTTPS termination, real Gunicorn multi-worker rate limiting, Redis configuration, proxy trust topology, backups/restoration, dependency vulnerability scan, malware scanning, production error handling, cache headers, log retention, disaster recovery, and external policy/RAG prompt-injection testing.
