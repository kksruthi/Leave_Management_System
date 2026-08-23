"""Tests for authentication, permissions and the role-based API.

The routing rule this file exists to pin: an employee's leave goes to their
manager, a MANAGER's own leave goes to HR, HR's goes to a director, and a
director — with nobody above them — self-approves.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import app
from app.auth import (
    AuthError,
    authenticate,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.authorization import is_top_of_chain, resolve_assigned_approver
from app.classification import submit_leave_request
from app.approval import submit_approval_chain
from app.db import SessionLocal
from app.models import Employee, LeaveRequest
from app.permissions import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    PermissionDenied,
    has_permission,
    require_permission,
)

PASSWORD = "leave1234"
FUTURE = dt.datetime.now(dt.timezone.utc)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


def auth(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


EMPLOYEE = "priya@northbridge.example"
MANAGER = "anitha.rajan@northbridge.example"
HR = "fatima.khan@northbridge.example"
DIRECTOR = "nathan.cole@northbridge.example"


# ===========================================================================
# Passwords and tokens
# ===========================================================================
def test_password_round_trip():
    encoded = hash_password("correct horse battery")
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong", encoded)


def test_hash_is_salted():
    """Two users with the same password must not share a hash."""
    assert hash_password("same-password") != hash_password("same-password")


def test_short_passwords_are_refused():
    with pytest.raises(AuthError):
        hash_password("short")


def test_missing_hash_never_verifies():
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False


def test_token_round_trip(session):
    person = session.scalars(select(Employee).where(Employee.email == EMPLOYEE)).one()
    token, expires = create_access_token(person)
    claims = decode_access_token(token)
    assert claims["sub"] == str(person.id)
    assert claims["role"] == person.role
    assert expires > 0


def test_wrong_password_is_rejected(session):
    with pytest.raises(AuthError):
        authenticate(session, EMPLOYEE, "nope")


def test_unknown_email_gives_the_same_error(session):
    """No account-enumeration oracle: both failures read identically."""
    with pytest.raises(AuthError) as unknown:
        authenticate(session, "nobody@northbridge.example", "nope")
    with pytest.raises(AuthError) as bad_password:
        authenticate(session, EMPLOYEE, "nope")
    assert str(unknown.value) == str(bad_password.value)


# ===========================================================================
# Permissions
# ===========================================================================
def test_roles_are_cumulative():
    assert ROLE_PERMISSIONS["employee"] < ROLE_PERMISSIONS["manager"]
    assert ROLE_PERMISSIONS["manager"] < ROLE_PERMISSIONS["hr_admin"]


def test_employees_cannot_administer():
    for permission in ("view_all_leave", "manage_policies", "correct_ledger", "view_audit"):
        assert not has_permission("employee", permission)
        assert not has_permission("manager", permission)


def test_managers_can_approve_but_not_manage_policy():
    assert has_permission("manager", "approve_team_leave")
    assert not has_permission("manager", "manage_policies")
    assert not has_permission("manager", "manage_employees")


def test_unknown_permission_raises_rather_than_passing():
    """A typo'd permission must not silently evaluate to 'allowed'."""
    with pytest.raises(KeyError):
        has_permission("hr_admin", "manage_evertyhing")


def test_require_permission_message_names_the_permission():
    with pytest.raises(PermissionDenied, match="manage_policies"):
        require_permission("employee", "manage_policies")


def test_every_permission_is_documented():
    granted = set().union(*ROLE_PERMISSIONS.values())
    assert granted <= set(PERMISSIONS)


# ===========================================================================
# API auth
# ===========================================================================
def test_unauthenticated_calls_are_refused(client):
    assert client.get("/api/me/dashboard").status_code == 401


def test_a_garbage_token_is_refused(client):
    r = client.get("/api/me/dashboard", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


def test_login_returns_the_permission_list(client):
    r = client.post("/api/auth/login", json={"email": EMPLOYEE, "password": PASSWORD})
    user = r.json()["user"]
    assert user["role"] == "employee"
    assert "create_leave_request" in user["permissions"]
    assert "manage_policies" not in user["permissions"]
    assert user["is_admin"] is False


def test_bad_credentials_return_401(client):
    r = client.post("/api/auth/login", json={"email": EMPLOYEE, "password": "wrong"})
    assert r.status_code == 401


# ===========================================================================
# Role-scoped access
# ===========================================================================
@pytest.mark.parametrize("path", [
    "/api/hr/overview", "/api/hr/employees", "/api/hr/policies", "/api/hr/requests",
])
def test_employees_are_blocked_from_hr_endpoints(client, path):
    assert client.get(path, headers=auth(client, EMPLOYEE)).status_code == 403


@pytest.mark.parametrize("path", ["/api/team/overview", "/api/approvals", "/api/team/balances"])
def test_employees_are_blocked_from_team_endpoints(client, path):
    assert client.get(path, headers=auth(client, EMPLOYEE)).status_code == 403


def test_managers_are_blocked_from_policy_administration(client):
    assert client.get("/api/hr/policies", headers=auth(client, MANAGER)).status_code == 403


def test_managers_can_reach_team_endpoints(client):
    assert client.get("/api/team/overview", headers=auth(client, MANAGER)).status_code == 200
    assert client.get("/api/approvals", headers=auth(client, MANAGER)).status_code == 200


def test_hr_can_reach_everything(client):
    headers = auth(client, HR)
    for path in ("/api/me/dashboard", "/api/team/overview",
                 "/api/hr/overview", "/api/hr/policies"):
        assert client.get(path, headers=headers).status_code == 200, path


def test_employee_only_sees_their_own_dashboard(client, session):
    """Row-level scoping, not just endpoint permission."""
    raj = session.scalars(select(Employee).where(Employee.name == "Raj")).one()
    r = client.get(f"/api/hr/employees/{raj.id}", headers=auth(client, EMPLOYEE))
    assert r.status_code == 403


def test_manager_sees_their_report_but_not_a_stranger(client, session):
    headers = auth(client, MANAGER)
    priya = session.scalars(select(Employee).where(Employee.name == "Priya")).one()
    lena = session.scalars(select(Employee).where(Employee.name == "Lena Ortiz")).one()
    assert client.get(f"/api/hr/employees/{priya.id}", headers=headers).status_code == 200
    assert client.get(f"/api/hr/employees/{lena.id}", headers=headers).status_code == 403


# ===========================================================================
# THE ROUTING RULE
# ===========================================================================
def _chain_for(session, employee, days_ahead=200):
    start = dt.date.today() + dt.timedelta(days=days_ahead)
    while start.weekday() != 0:
        start += dt.timedelta(days=1)
    result = submit_leave_request(
        session, employee, "EL", start, start + dt.timedelta(days=1), commit=False
    )
    return result.request, submit_approval_chain(session, result.request)


def test_an_employees_leave_goes_to_their_manager(session):
    priya = session.scalars(select(Employee).where(Employee.name == "Priya")).one()
    anitha = session.scalars(select(Employee).where(Employee.name == "Anitha Rajan")).one()
    _, steps = _chain_for(session, priya, 200)
    assert steps[0].role == "manager"
    assert steps[0].assigned_approver_id == anitha.id


def test_a_managers_own_leave_goes_to_hr(session):
    """The rule you specified: a manager cannot approve their own leave, and
    there is no manager above them, so HR signs it off."""
    anitha = session.scalars(select(Employee).where(Employee.name == "Anitha Rajan")).one()
    fatima = session.scalars(select(Employee).where(Employee.name == "Fatima Khan")).one()
    _, steps = _chain_for(session, anitha, 210)
    assert steps[0].assigned_approver_id == fatima.id


def test_hrs_own_leave_goes_to_a_director(session):
    fatima = session.scalars(select(Employee).where(Employee.name == "Fatima Khan")).one()
    _, steps = _chain_for(session, fatima, 220)
    approver = session.get(Employee, steps[0].assigned_approver_id)
    assert approver.role == "director"


def test_a_role_tier_lands_on_someone_in_the_same_region(session):
    """Two HR admins, two regions: a Chennai request must not go to Texas.

    Picking the lowest-id holder of a role was fine with one HR admin and
    became wrong the moment there were two — a Texas HR admin does not know
    Tamil Nadu's holidays, leave year or statutory minimums.
    """
    fatima = session.scalars(select(Employee).where(Employee.name == "Fatima Khan")).one()
    _, steps = _chain_for(session, fatima, 221)
    approver = session.get(Employee, steps[0].assigned_approver_id)
    directors = session.scalars(
        select(Employee).where(Employee.role == "director", Employee.status == "active")
    ).all()
    if any(d.region == fatima.region for d in directors):
        assert approver.region == fatima.region


def test_the_director_self_approves(session):
    """Nobody outranks them, so the request is approved and labelled as such
    rather than pending forever against an empty assignee."""
    nathan = session.scalars(select(Employee).where(Employee.name == "Nathan Cole")).one()
    assert is_top_of_chain(session, nathan) is True

    request, steps = _chain_for(session, nathan, 230)
    assert session.get(LeaveRequest, request.id).status == "approved"
    assert all(s.status == "approved" for s in steps)
    assert "Self-approved" in steps[0].decision_reason


def test_a_manager_is_not_top_of_chain(session):
    anitha = session.scalars(select(Employee).where(Employee.name == "Anitha Rajan")).one()
    assert is_top_of_chain(session, anitha) is False


def test_an_approver_is_never_assigned_their_own_request(session):
    for name in ("Priya", "Anitha Rajan", "Fatima Khan"):
        person = session.scalars(select(Employee).where(Employee.name == name)).one()
        request = LeaveRequest(
            employee_id=person.id, leave_type_id="EL",
            start_date=dt.date(2030, 6, 3), end_date=dt.date(2030, 6, 4),
            duration_days=2, status="pending",
        )
        for role in ("manager", "hr_admin", "director"):
            approver = resolve_assigned_approver(session, request, role)
            assert approver is None or approver.id != person.id


# ===========================================================================
# The employee-facing flow
# ===========================================================================
def test_preview_shows_the_day_by_day_working(client):
    """The 'show the calculation' requirement, as an API contract."""
    start = dt.date.today() + dt.timedelta(days=120)
    while start.weekday() != 0:
        start += dt.timedelta(days=1)
    r = client.post("/api/me/requests/preview", headers=auth(client, EMPLOYEE), json={
        "leave_type_id": "EL",
        "start_date": start.isoformat(),
        "end_date": (start + dt.timedelta(days=6)).isoformat(),
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["days"]) == 7
    kinds = {d["kind"] for d in body["days"]}
    assert "weekend" in kinds and "leave" in kinds
    assert body["balance_before"] is not None
    assert body["balance_after"] is not None


def test_preview_reports_blockers_instead_of_erroring(client):
    """The form needs to show what is wrong while the employee is still editing."""
    past = dt.date.today() - dt.timedelta(days=10)
    r = client.post("/api/me/requests/preview", headers=auth(client, EMPLOYEE), json={
        "leave_type_id": "EL",
        "start_date": past.isoformat(),
        "end_date": (past + dt.timedelta(days=2)).isoformat(),
    })
    assert r.status_code == 200
    assert r.json()["can_submit"] is False
    assert any("Backdated" in b for b in r.json()["blockers"])


def test_rejecting_without_a_reason_is_refused(client, session):
    """A rejection the employee cannot understand is not an answer."""
    headers = auth(client, MANAGER)
    queue = client.get("/api/approvals", headers=headers).json()
    if not queue:
        pytest.skip("no request awaiting this manager in the demo data")
    step_id = queue[0]["step_id"]
    r = client.post(f"/api/approvals/{step_id}/decide", headers=headers,
                    json={"decision": "rejected", "comment": "  "})
    assert r.status_code == 400
    assert "reason is required" in r.json()["detail"].lower()


def test_approval_options_say_where_it_goes_next(client):
    """A manager's 'Approve' on a multi-tier request forwards to HR — the
    label has to say so, or they think they granted the leave."""
    queue = client.get("/api/approvals", headers=auth(client, MANAGER)).json()
    for item in queue:
        options = item["options"]
        if options["is_final"]:
            assert options["approve_label"] == "Approve"
        else:
            assert "send to" in options["approve_label"]
            assert options["forwards_to_role"]
