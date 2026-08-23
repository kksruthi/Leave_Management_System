"""HTTP API for the leave engine.

A thin layer. Every endpoint does three things and no more:

  1. establish who is calling (`app/auth.py`),
  2. check the permission it needs (`app/permissions.py`),
  3. call the engine, which re-checks row-level access itself
     (`app/authorization.py`).

**No business logic lives here.** If an endpoint starts computing entitlements
or deciding what is paid, that belongs in a module. The API's job is to shape
data for a browser.

## Why the role is re-read on every request

The JWT carries a role so the UI can render the right navigation immediately.
The API never trusts it: `current_user` loads the employee row every time. A
token issued before somebody was moved out of HR must not keep HR's powers
until it expires.

## Two-layer authorization, again

`require(...)` checks the *capability* ("managers may approve"). The engine
then checks the *instance* ("...but only for their own reports"). Both must
pass. An endpoint that only did the first would let any manager approve any
request in the company.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.approval import (
    ApprovalError,
    decision_options,
    escalate_overdue_steps,
    forward_options,
    forward_request,
    recommended_reviewers,
    record_approval_decision,
    submit_approval_chain,
)
from app.leave_types import (
    LeaveTypeError,
    create_leave_type,
    list_leave_types,
    reactivate_leave_type,
    retire_leave_type,
    to_dict as leave_type_dict,
    update_leave_type,
)
from app.notifications import (
    KIND_BALANCE,
    inbox,
    mark_all_read,
    mark_read,
    notify,
    unread_count,
)
from app.region_transfer import preview_region_transfer, relocate_employee
from app.simulation import (
    ACTIONS,
    SimulationError,
    run_action,
    snapshot,
    timeline,
)
from app.policy_lifecycle import (
    all_region_status,
    policy_year_status,
    preview_publish,
    publish_policy_year,
    remind_expiring_policies,
)
from app.auth import (
    AuthError,
    TokenError,
    authenticate,
    create_access_token,
    decode_access_token,
    set_password,
    verify_password,
)
from app.authorization import (
    AuthorizationError,
    active_delegate_for,
    can_view_employee,
    management_chain,
)
from app.classification import (
    RequestValidationError,
    cancel_leave_request,
    preview_leave_request,
    submit_leave_request,
)
from app.copilot import advise
from app.dashboard import (
    EmployeeNotFoundError,
    get_balance_buckets,
    get_dashboard,
    get_expiring_soon,
    get_live_balance,
)
from app.db import SessionLocal
from app.holidays import REGION_CALENDARS, explain_days, holidays_in_range
from app.models import (
    ApprovalStep,
    Employee,
    EmployeeException,
    HolidayOverride,
    LeaveLedger,
    LeaveRequest,
    OrgPolicy,
)
from app.permissions import (
    PERMISSIONS,
    PermissionDenied,
    permissions_for,
    require_permission,
)
from app.policy_admin import PolicyAdminError, preview_roll_forward, roll_forward_year
from app.policy_engine import resolve_policy

log = logging.getLogger("leave_engine.api")


def _ensure_current_year_granted() -> None:
    """Make sure this leave year's annual lumps exist before anyone looks.

    CL and SL are `annual_lump` types: the whole entitlement lands in one row
    at the start of the leave year. If nobody has run the grant job for the
    current year — a fresh clone, a database restored from before the year
    turned, an operator who ran `seed` but not `cli annual` — then there is
    genuinely no CL or SL row in scope, and the dashboard correctly but
    uselessly reports zero.

    Correct is not the same as helpful. An entitlement the policy grants
    unconditionally on day one should not be invisible because a cron job was
    never wired up, so the API grants it on boot instead.

    This is safe to run every start: `run_annual_grant` is idempotent PER
    LEAVE YEAR, so the second and every subsequent boot writes nothing. It is
    also cheap — one query per employee per lump type, and it only ever adds
    rows the policy already promised.
    """
    from app.accrual import run_annual_grant

    try:
        with SessionLocal() as session:
            result = run_annual_grant(session, dt.date.today(), commit=True)
        if result.written:
            log.info(
                "startup: granted %d annual entitlement rows for the current "
                "leave year", len(result.written),
            )
    except Exception:                       # never let this stop the API
        log.exception("startup annual-grant check failed; continuing")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _ensure_current_year_granted()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Leave Engine API",
    version="1.0",
    description="Role-based leave management: employee, manager and HR.",
)


# The dev frontend runs on Vite's port; production serves the built bundle
# from this same origin, where CORS is irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bearer = HTTPBearer(auto_error=False)


# ===========================================================================
# Serialisation
# ===========================================================================
def jsonable(value: Any) -> Any:
    """Decimals as strings, dates as ISO. Floats would lose 0.833 precision."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


# ===========================================================================
# Dependencies
# ===========================================================================
def get_session():
    with SessionLocal() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Employee:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    employee = session.get(Employee, int(claims["sub"]))
    if employee is None or employee.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active.")
    return employee


UserDep = Annotated[Employee, Depends(current_user)]


def require(user: Employee, permission: str) -> None:
    try:
        require_permission(user.role, permission)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


# ===========================================================================
# Error handling — engine exceptions become sensible HTTP codes
# ===========================================================================
@app.exception_handler(AuthorizationError)
async def _authz(request: Request, exc: AuthorizationError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ApprovalError)
async def _approval(request: Request, exc: ApprovalError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PolicyAdminError)
async def _policy_admin(request: Request, exc: PolicyAdminError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(SimulationError)
async def _simulation(request: Request, exc: SimulationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(LeaveTypeError)
async def _leave_type(request: Request, exc: LeaveTypeError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(EmployeeNotFoundError)
async def _not_found(request: Request, exc: EmployeeNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# ===========================================================================
# Schemas
# ===========================================================================
class LoginBody(BaseModel):
    email: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class RequestBody(BaseModel):
    leave_type_id: str
    start_date: dt.date
    end_date: dt.date
    start_half_day: bool = False
    end_half_day: bool = False
    reason: str | None = None
    override_reason: str | None = None


class DecisionBody(BaseModel):
    decision: str  # "approved" | "rejected"
    comment: str | None = None


class ForwardBody(BaseModel):
    to_role: str
    note: str | None = None


class LeaveTypeBody(BaseModel):
    code: str
    name: str
    description: str | None = None
    color_token: str = "series-4"


class LeaveTypePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    color_token: str | None = None
    display_order: int | None = None
    is_active: bool | None = None


class TransferBody(BaseModel):
    new_region: str
    transfer_date: dt.date | None = None


class PublishBody(BaseModel):
    region: str
    from_year: int
    change_reason: str
    changes: dict[str, dict[str, Any]] | None = None
    leave_year_end: str | None = None


class RollForwardBody(BaseModel):
    region: str
    from_year: int
    changes: dict[str, dict[str, Any]] | None = None
    change_reason: str
    leave_year_end: str | None = None


class HolidayBody(BaseModel):
    region: str
    holiday_date: dt.date
    name: str
    is_working_day: bool = False


# ===========================================================================
# Auth
# ===========================================================================
@app.post("/api/auth/login")
def login(body: LoginBody, session: SessionDep):
    try:
        employee = authenticate(session, body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    token, expires_in = create_access_token(employee)
    session.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": _user_payload(session, employee),
    }


@app.get("/api/auth/me")
def me(user: UserDep, session: SessionDep):
    return _user_payload(session, user)


@app.post("/api/auth/change-password")
def change_password(body: PasswordBody, user: UserDep, session: SessionDep):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    set_password(user, body.new_password)
    session.commit()
    return {"ok": True}


def _user_payload(session: Session, user: Employee) -> dict:
    manager = session.get(Employee, user.manager_id) if user.manager_id else None
    delegate = active_delegate_for(session, user.id)
    return jsonable({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "region": user.region,
        "department": user.department,
        "join_date": user.join_date,
        "employment_fraction": user.employment_fraction,
        "manager": {"id": manager.id, "name": manager.name} if manager else None,
        "is_admin": user.role in ("hr_admin", "director"),
        "permissions": sorted(permissions_for(user.role)),
        "must_change_password": user.must_change_password,
        "delegating_to": {"id": delegate.id, "name": delegate.name} if delegate else None,
    })


# ===========================================================================
# Reference data
# ===========================================================================
@app.get("/api/meta")
def meta(user: UserDep, session: SessionDep):
    """Leave types available to the caller, plus vocabulary for the UI."""
    types = session.scalars(
        select(OrgPolicy.leave_type_id)
        .where(OrgPolicy.region == user.region)
        .distinct()
        .order_by(OrgPolicy.leave_type_id)
    ).all()
    regions = session.scalars(select(OrgPolicy.region).distinct().order_by(OrgPolicy.region)).all()
    return jsonable({
        "leave_types": list(types),
        "regions": list(regions),
        "calendar_regions": sorted(REGION_CALENDARS),
        "permissions": PERMISSIONS,
    })


# ===========================================================================
# EMPLOYEE — my leave
# ===========================================================================
@app.get("/api/me/dashboard")
def my_dashboard(user: UserDep, session: SessionDep, as_of: dt.date | None = None):
    require(user, "view_own_leave")
    dash = get_dashboard(session, user, as_of, viewer=user)
    return jsonable(_dashboard_payload(session, dash, user, as_of))


def _dashboard_payload(session, dash, user: Employee, as_of: dt.date | None) -> dict:
    as_of = as_of or dt.date.today()
    payload = dash.to_dict()
    # Enrich each type with expiry detail the raw payload leaves implicit.
    for view in payload["leave_types"]:
        expiring = get_expiring_soon(session, user.id, view["leave_type_id"], as_of)
        view["expiring"] = [
            {"date": d.isoformat(), "days": str(v)} for d, v in expiring
        ]
    payload["upcoming_approved"] = jsonable([
        {
            "id": r.id,
            "leave_type_id": r.leave_type_id,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "duration_days": r.duration_days,
        }
        for r in session.scalars(
            select(LeaveRequest)
            .where(
                LeaveRequest.employee_id == user.id,
                LeaveRequest.status == "approved",
                LeaveRequest.end_date >= as_of,
            )
            .order_by(LeaveRequest.start_date)
            .limit(10)
        )
    ])
    return payload


@app.get("/api/me/requests")
def my_requests(user: UserDep, session: SessionDep, limit: int = 50):
    require(user, "view_own_leave")
    rows = session.scalars(
        select(LeaveRequest)
        .where(LeaveRequest.employee_id == user.id)
        .order_by(LeaveRequest.start_date.desc())
        .limit(limit)
    ).all()
    return jsonable([_request_payload(session, r, viewer=user) for r in rows])


@app.post("/api/me/requests/preview")
def preview_request(body: RequestBody, user: UserDep, session: SessionDep):
    """Dry run — the day-by-day working, the split, and any blockers."""
    require(user, "create_leave_request")
    result = preview_leave_request(
        session, user, body.leave_type_id, body.start_date, body.end_date,
        start_half_day=body.start_half_day, end_half_day=body.end_half_day,
        override_reason=body.override_reason,
    )
    classification = result.pop("classification", None)
    payload = jsonable(result)
    payload["classification"] = jsonable(classification.to_dict()) if classification else None
    return payload


@app.post("/api/me/requests", status_code=201)
def create_request(body: RequestBody, user: UserDep, session: SessionDep):
    require(user, "create_leave_request")
    result = submit_leave_request(
        session, user, body.leave_type_id, body.start_date, body.end_date,
        start_half_day=body.start_half_day, end_half_day=body.end_half_day,
        override_reason=body.override_reason, commit=False,
    )
    if body.reason:
        # Employee's own note, distinct from an enforcement override.
        result.request.override_reason = (
            f"{body.reason}" if not result.request.override_reason
            else f"{body.reason} | override: {result.request.override_reason}"
        )
    submit_approval_chain(session, result.request)
    session.commit()

    payload = _request_payload(session, result.request, viewer=user)
    payload["warnings"] = result.warnings
    payload["classification"] = jsonable(result.classification.to_dict())
    return jsonable(payload)


@app.post("/api/me/requests/{request_id}/cancel")
def cancel_request(request_id: int, user: UserDep, session: SessionDep):
    require(user, "cancel_own_request")
    request_obj = session.get(LeaveRequest, request_id)
    if request_obj is None:
        raise HTTPException(404, "No such request.")
    cancel_leave_request(session, request_obj, actor=user, commit=True)
    return jsonable(_request_payload(session, request_obj, viewer=user))


# ===========================================================================
# Request serialisation — privacy-aware
# ===========================================================================
def _request_payload(
    session: Session, request: LeaveRequest, *, viewer: Employee, full: bool = False
) -> dict:
    """One request, shaped for whoever is looking.

    The employee's free-text reason is shown to the employee themselves and to
    administrators, but NOT to a manager approving the request. A manager
    needs to know who is away and for how long; "fertility treatment" is not
    theirs to read. Managers see the leave type, which is as much as the
    decision requires.
    """
    steps = session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.request_id == request.id)
        .order_by(ApprovalStep.tier)
    ).all()
    names = {
        e.id: e.name for e in session.scalars(
            select(Employee).where(Employee.id.in_(
                [s.acted_by for s in steps if s.acted_by]
                + [s.assigned_approver_id for s in steps if s.assigned_approver_id]
                + [request.employee_id]
            ))
        )
    }

    is_self = viewer.id == request.employee_id
    is_admin = viewer.role in ("hr_admin", "director")
    may_read_reason = is_self or is_admin

    return jsonable({
        "id": request.id,
        "employee_id": request.employee_id,
        "employee_name": names.get(request.employee_id),
        "leave_type_id": request.leave_type_id,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "duration_days": request.duration_days,
        "start_half_day": request.start_half_day,
        "end_half_day": request.end_half_day,
        "status": request.status,
        "paid_days": request.paid_days,
        "unpaid_days": request.unpaid_days,
        "submitted_at": request.submitted_at,
        # Withheld from managers on purpose — see the docstring.
        "reason": request.override_reason if may_read_reason else None,
        "reason_withheld": bool(request.override_reason) and not may_read_reason,
        "chain": [
            {
                "tier": s.tier,
                "role": s.role,
                "status": s.status,
                "routing_reason": s.routing_reason,
                "assigned_to": names.get(s.assigned_approver_id),
                "acted_by": names.get(s.acted_by),
                "acted_at": s.acted_at,
                "decision_reason": s.decision_reason,
                "due_at": s.due_at,
                "escalated_at": s.escalated_at,
            }
            for s in steps
        ],
        "can_cancel": is_self and request.status == "pending",
    })


# ===========================================================================
# MANAGER — team
# ===========================================================================
def _reports_of(session: Session, manager: Employee, direct_only: bool = False) -> list[Employee]:
    """Everyone whose chain runs through this person."""
    if manager.role in ("hr_admin", "director"):
        return list(session.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.name)
        ))
    direct = list(session.scalars(
        select(Employee)
        .where(Employee.manager_id == manager.id, Employee.status == "active")
        .order_by(Employee.name)
    ))
    if direct_only:
        return direct
    return [
        e for e in session.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.name)
        )
        if e.id != manager.id and any(m.id == manager.id for m in management_chain(session, e))
    ] or direct


@app.get("/api/team/overview")
def team_overview(user: UserDep, session: SessionDep, as_of: dt.date | None = None):
    require(user, "view_team_leave")
    as_of = as_of or dt.date.today()
    reports = _reports_of(session, user)
    ids = [e.id for e in reports] or [-1]

    on_leave_today = session.scalars(
        select(LeaveRequest).where(
            LeaveRequest.employee_id.in_(ids),
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= as_of,
            LeaveRequest.end_date >= as_of,
        )
    ).all()

    upcoming = session.scalars(
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id.in_(ids),
            LeaveRequest.status.in_(("approved", "pending")),
            LeaveRequest.end_date >= as_of,
        )
        .order_by(LeaveRequest.start_date)
        .limit(20)
    ).all()

    names = {e.id: e.name for e in reports}
    queue = _approval_queue(session, user)

    return jsonable({
        "team_size": len(reports),
        "pending_approvals": len(queue),
        "on_leave_today": [
            {"employee": names.get(r.employee_id), "leave_type_id": r.leave_type_id,
             "start_date": r.start_date, "end_date": r.end_date}
            for r in on_leave_today
        ],
        "upcoming": [
            {"employee": names.get(r.employee_id), "leave_type_id": r.leave_type_id,
             "start_date": r.start_date, "end_date": r.end_date,
             "status": r.status, "duration_days": r.duration_days}
            for r in upcoming
        ],
    })


@app.get("/api/team/calendar")
def team_calendar(
    user: UserDep,
    session: SessionDep,
    year: int = Query(default=None),
    month: int = Query(default=None),
):
    """Per-employee bars for a month, plus that region's holidays.

    Deliberately carries the leave TYPE and status but not the reason — a
    manager planning cover needs to know who is out, not why.
    """
    require(user, "view_team_leave")
    today = dt.date.today()
    year = year or today.year
    month = month or today.month
    first = dt.date(year, month, 1)
    last = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)

    reports = _reports_of(session, user)
    ids = [e.id for e in reports] or [-1]
    rows = session.scalars(
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id.in_(ids),
            LeaveRequest.status.in_(("approved", "pending")),
            LeaveRequest.start_date <= last,
            LeaveRequest.end_date >= first,
        )
        .order_by(LeaveRequest.start_date)
    ).all()

    by_employee: dict[int, list] = {}
    for r in rows:
        by_employee.setdefault(r.employee_id, []).append({
            "id": r.id,
            "leave_type_id": r.leave_type_id,
            "status": r.status,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "duration_days": r.duration_days,
        })

    # Conflicts: days on which more than one team member is away.
    day_counts: dict[dt.date, list[str]] = {}
    names = {e.id: e.name for e in reports}
    for r in rows:
        cursor = max(r.start_date, first)
        while cursor <= min(r.end_date, last):
            day_counts.setdefault(cursor, []).append(names.get(r.employee_id, "?"))
            cursor += dt.timedelta(days=1)

    return jsonable({
        "year": year,
        "month": month,
        "first_day": first,
        "last_day": last,
        "employees": [
            {"id": e.id, "name": e.name, "region": e.region,
             "entries": by_employee.get(e.id, [])}
            for e in reports
        ],
        "holidays": [
            {"date": d, "name": n}
            for d, n in holidays_in_range(first, last, user.region, session)
        ],
        "conflicts": [
            {"date": d, "people": people}
            for d, people in sorted(day_counts.items()) if len(people) > 1
        ],
    })


@app.get("/api/team/balances")
def team_balances(user: UserDep, session: SessionDep, as_of: dt.date | None = None):
    require(user, "view_team_leave")
    as_of = as_of or dt.date.today()
    out = []
    for employee in _reports_of(session, user):
        types = session.scalars(
            select(OrgPolicy.leave_type_id)
            .where(OrgPolicy.region == employee.region)
            .distinct()
        ).all()
        balances = {
            t: get_live_balance(session, employee.id, t, as_of) for t in sorted(types)
        }
        pending = session.scalar(
            select(func.count()).select_from(LeaveRequest).where(
                LeaveRequest.employee_id == employee.id,
                LeaveRequest.status == "pending",
            )
        )
        out.append({
            "id": employee.id,
            "name": employee.name,
            "region": employee.region,
            "department": employee.department,
            "balances": balances,
            "pending_requests": pending,
        })
    return jsonable(out)


# ===========================================================================
# APPROVALS
# ===========================================================================
def _approval_queue(session: Session, user: Employee) -> list[ApprovalStep]:
    """Steps this person can act on: assigned to them, or delegated to them."""
    from app.models import ApprovalDelegation

    today = dt.date.today()
    delegated_ids = list(session.scalars(
        select(ApprovalDelegation.delegator_id).where(
            ApprovalDelegation.delegate_id == user.id,
            ApprovalDelegation.is_active.is_(True),
            ApprovalDelegation.from_date <= today,
            ApprovalDelegation.to_date >= today,
        )
    ))
    owner_ids = [user.id] + delegated_ids

    return list(session.scalars(
        select(ApprovalStep)
        .where(
            ApprovalStep.status == "active",
            ApprovalStep.assigned_approver_id.in_(owner_ids),
        )
        .order_by(ApprovalStep.due_at.nulls_last(), ApprovalStep.id)
    ))


@app.get("/api/approvals")
def approvals(user: UserDep, session: SessionDep):
    require(user, "approve_team_leave")
    out = []
    for step in _approval_queue(session, user):
        request = session.get(LeaveRequest, step.request_id)
        if request is None or request.status != "pending":
            continue
        employee = session.get(Employee, request.employee_id)
        balance = get_live_balance(session, employee.id, request.leave_type_id)
        paid = Decimal(request.paid_days or 0)

        out.append(jsonable({
            "step_id": step.id,
            "tier": step.tier,
            "role": step.role,
            "routing_reason": step.routing_reason,
            "due_at": step.due_at,
            "is_overdue": bool(step.due_at and step.due_at < dt.datetime.now(dt.timezone.utc)),
            "on_behalf_of": (
                session.get(Employee, step.assigned_approver_id).name
                if step.assigned_approver_id and step.assigned_approver_id != user.id
                else None
            ),
            "request": _request_payload(session, request, viewer=user),
            "balance_before": balance,
            "balance_after": balance - paid,
            "options": decision_options(session, step),
            "advice": advise(session, request),
            "forward_options": forward_options(session, step),
            "recommended": recommended_reviewers(session, request),
            "days": explain_days(
                request.start_date, request.end_date, employee.region, session,
                start_half_day=request.start_half_day,
                end_half_day=request.end_half_day,
            ),
        }))
    return out


@app.post("/api/approvals/{step_id}/decide")
def decide(step_id: int, body: DecisionBody, user: UserDep, session: SessionDep):
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'.")
    require(user, "approve_team_leave" if body.decision == "approved" else "reject_team_leave")

    step = session.get(ApprovalStep, step_id)
    if step is None:
        raise HTTPException(404, "No such approval step.")

    # A rejection without a reason is unappealable and unexplainable.
    if body.decision == "rejected" and not (body.comment or "").strip():
        raise HTTPException(400, "A reason is required when rejecting a request.")

    record_approval_decision(
        session, step.request_id, step.tier, body.decision,
        actor=user, decision_reason=body.comment,
    )
    session.commit()

    request = session.get(LeaveRequest, step.request_id)
    payload = _request_payload(session, request, viewer=user)
    nxt = next(
        (c for c in payload["chain"] if c["status"] == "active"), None
    )
    payload["forwarded_to"] = nxt["role"] if nxt else None
    return jsonable(payload)


@app.post("/api/approvals/{step_id}/forward")
def forward(step_id: int, body: ForwardBody, user: UserDep, session: SessionDep):
    """Hand the decision to the next approver instead of making it.

    Deliberately a separate endpoint from `/decide`. Forwarding is not a kind
    of approval — the approver is explicitly declining to grant the leave and
    passing it up — and collapsing the two into one call is what let the old
    UI imply a manager could approve *and* still have HR decide.
    """
    require(user, "approve_team_leave")
    step = session.get(ApprovalStep, step_id)
    if step is None:
        raise HTTPException(404, "No such approval step.")

    new_step = forward_request(
        session, step.request_id, step.tier, body.to_role,
        actor=user, note=body.note,
    )
    session.commit()

    request = session.get(LeaveRequest, step.request_id)
    payload = _request_payload(session, request, viewer=user)
    recipient = (
        session.get(Employee, new_step.assigned_approver_id)
        if new_step.assigned_approver_id else None
    )
    payload["forwarded_to"] = new_step.role
    payload["forwarded_to_name"] = recipient.name if recipient else None
    return jsonable(payload)


# ===========================================================================
# HR — administration
# ===========================================================================
@app.get("/api/hr/overview")
def hr_overview(user: UserDep, session: SessionDep, as_of: dt.date | None = None):
    require(user, "view_all_leave")
    as_of = as_of or dt.date.today()
    month_start = as_of.replace(day=1)

    employees = session.scalar(
        select(func.count()).select_from(Employee).where(Employee.status == "active")
    )
    on_leave = session.scalar(
        select(func.count()).select_from(LeaveRequest).where(
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= as_of,
            LeaveRequest.end_date >= as_of,
        )
    )
    pending = session.scalar(
        select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "pending")
    )
    this_month = session.scalar(
        select(func.count()).select_from(LeaveRequest).where(
            LeaveRequest.submitted_at >= dt.datetime.combine(month_start, dt.time.min)
        )
    )
    consumed = session.scalar(
        select(func.coalesce(func.sum(-LeaveLedger.amount), 0)).where(
            LeaveLedger.amount < 0, LeaveLedger.reason.like("leave taken%")
        )
    )
    exceptions = session.scalar(select(func.count()).select_from(EmployeeException))

    # --- alerts -----------------------------------------------------------
    alerts = []
    expiring_soon = session.execute(
        select(Employee.name, LeaveLedger.leave_type_id, LeaveLedger.expires_on,
               func.sum(LeaveLedger.amount))
        .join(Employee, Employee.id == LeaveLedger.employee_id)
        .where(
            LeaveLedger.expires_on.is_not(None),
            LeaveLedger.expires_on >= as_of,
            LeaveLedger.expires_on <= as_of + dt.timedelta(days=60),
        )
        .group_by(Employee.name, LeaveLedger.leave_type_id, LeaveLedger.expires_on)
    ).all()
    if expiring_soon:
        alerts.append({
            "level": "warning",
            "message": f"{len(expiring_soon)} employees approaching carry-over expiry",
            "detail": [
                {"employee": n, "leave_type": t, "expires_on": d.isoformat(), "days": str(v)}
                for n, t, d, v in expiring_soon
            ],
        })

    overdue = session.scalars(
        select(ApprovalStep).where(
            ApprovalStep.status == "active",
            ApprovalStep.due_at.is_not(None),
            ApprovalStep.due_at < dt.datetime.now(dt.timezone.utc),
        )
    ).all()
    if overdue:
        alerts.append({
            "level": "warning",
            "message": f"{len(overdue)} approval requests overdue",
            "detail": [{"request_id": s.request_id, "tier": s.tier, "role": s.role}
                       for s in overdue],
        })

    negative = session.execute(
        select(Employee.name, LeaveLedger.leave_type_id, func.sum(LeaveLedger.amount))
        .join(Employee, Employee.id == LeaveLedger.employee_id)
        .group_by(Employee.name, LeaveLedger.leave_type_id)
        .having(func.sum(LeaveLedger.amount) < 0)
    ).all()
    if negative:
        alerts.append({
            "level": "danger",
            "message": f"{len(negative)} employees with negative balances",
            "detail": [{"employee": n, "leave_type": t, "balance": str(v)}
                       for n, t, v in negative],
        })

    if exceptions:
        alerts.append({
            "level": "info",
            "message": f"{exceptions} policy exceptions in force",
            "detail": [],
        })

    return jsonable({
        "employees": employees,
        "on_leave_today": on_leave,
        "pending_approvals": pending,
        "requests_this_month": this_month,
        "leave_consumed_days": consumed,
        "policy_exceptions": exceptions,
        "alerts": alerts,
    })


@app.get("/api/hr/employees")
def hr_employees(
    user: UserDep,
    session: SessionDep,
    q: str | None = None,
    region: str | None = None,
    department: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
):
    require(user, "manage_employees")
    stmt = select(Employee).order_by(Employee.name)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(Employee.name).like(like), func.lower(Employee.email).like(like)
        ))
    if region:
        stmt = stmt.where(Employee.region == region)
    if department:
        stmt = stmt.where(Employee.department == department)
    if status_filter:
        stmt = stmt.where(Employee.status == status_filter)

    rows = session.scalars(stmt).all()
    managers = {e.id: e.name for e in session.scalars(select(Employee))}
    return jsonable([
        {
            "id": e.id, "name": e.name, "email": e.email, "region": e.region,
            "department": e.department, "role": e.role,
            "manager": managers.get(e.manager_id),
            "join_date": e.join_date, "exit_date": e.exit_date,
            "employment_fraction": e.employment_fraction, "status": e.status,
        }
        for e in rows
    ])


@app.get("/api/hr/employees/{employee_id}")
def hr_employee(employee_id: int, user: UserDep, session: SessionDep):
    """One employee's record.

    Gated on `view_team_leave` rather than `view_all_leave` so a manager can
    open a member of their own team — the two-layer rule at work. The
    capability check says "managers may look at people"; `can_view_employee`
    then decides WHICH people, and a manager asking about someone outside
    their reporting line gets a 403 from that second check.

    The HR-only parts of the payload (entitlement exceptions) are withheld
    from managers even for their own reports.
    """
    require(user, "view_team_leave")
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee.")
    if not can_view_employee(session, user, employee):
        raise HTTPException(
            403,
            f"{employee.name} is not in your reporting line. Managers see their "
            "own team; HR and directors see everyone.",
        )

    is_admin = user.role in ("hr_admin", "director")
    dash = get_dashboard(session, employee, viewer=user)
    requests = session.scalars(
        select(LeaveRequest)
        .where(LeaveRequest.employee_id == employee_id)
        .order_by(LeaveRequest.start_date.desc())
        .limit(25)
    ).all()
    exceptions = session.scalars(
        select(EmployeeException).where(EmployeeException.employee_id == employee_id)
    ).all() if is_admin else []
    manager = session.get(Employee, employee.manager_id) if employee.manager_id else None

    return jsonable({
        "employee": {
            "id": employee.id, "name": employee.name, "email": employee.email,
            "region": employee.region, "department": employee.department,
            "role": employee.role, "manager": manager.name if manager else None,
            "join_date": employee.join_date, "exit_date": employee.exit_date,
            "employment_fraction": employee.employment_fraction,
            "status": employee.status, "last_login_at": employee.last_login_at,
        },
        "dashboard": dash.to_dict(),
        "requests": [_request_payload(session, r, viewer=user) for r in requests],
        "exceptions": [
            {"leave_type_id": x.leave_type_id,
             "entitlement_days_per_year": x.entitlement_days_per_year,
             "reason": x.reason, "effective_from": x.effective_from,
             "effective_to": x.effective_to}
            for x in exceptions
        ],
    })


@app.get("/api/hr/employees/{employee_id}/relocation-preview")
def hr_relocation_preview(
    employee_id: int, to_region: str, user: UserDep, session: SessionDep,
    on: dt.date | None = None,
):
    """What relocating this person would do, before anyone commits to it."""
    require(user, "manage_employees")
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee.")
    if to_region == employee.region:
        raise HTTPException(400, f"{employee.name} is already in {to_region}.")

    on = on or dt.date.today()
    old_region = employee.region
    # Resolve against a detached copy so the preview cannot leave the real row
    # pointing at a region nobody agreed to.
    employee.region = to_region
    try:
        planned = preview_region_transfer(session, employee, old_region, to_region, on)
    finally:
        employee.region = old_region
        session.expunge_all()

    return jsonable({
        "employee": {"id": employee_id, "name": employee.name},
        "from_region": old_region,
        "to_region": to_region,
        "transfer_date": on,
        "adjustments": [
            {
                "leave_type_id": a.leave_type_id,
                "old_entitlement": a.old_entitlement,
                "new_entitlement": a.new_entitlement,
                "adjustment": a.adjustment,
                "reason": a.reason,
                "basis": a.basis,
            }
            for a in planned
        ],
    })


@app.post("/api/hr/employees/{employee_id}/relocate")
def hr_relocate_employee(
    employee_id: int, body: TransferBody, user: UserDep, session: SessionDep
):
    """Move an employee to another region and reconcile their entitlement.

    Everything region-dependent follows: the entitlement bracket, the leave
    year (and therefore which accruals are in scope), the carry-over cap, the
    public-holiday calendar and the approval routing. The one thing that does
    NOT follow is days already earned — see `app/region_transfer.py`.
    """
    require(user, "manage_employees")
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee.")

    try:
        result = relocate_employee(
            session, employee, body.new_region, body.transfer_date, commit=False,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    moved = [a for a in result["adjustments"] if Decimal(a["adjustment"]) != 0]
    notify(
        session, employee.id, KIND_BALANCE,
        f"You have been relocated to {body.new_region}",
        (
            "; ".join(f"{a['leave_type_id']} {a['adjustment']}" for a in moved)
            + ". Open My leave for the full working."
        ) if moved else (
            "Your entitlement has been re-resolved for the new region. No days "
            "were added or removed — see My leave."
        ),
        "/",
    )
    session.commit()
    return jsonable(result)


@app.get("/api/hr/requests")
def hr_requests(
    user: UserDep,
    session: SessionDep,
    employee_id: int | None = None,
    region: str | None = None,
    leave_type: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = 100,
):
    require(user, "view_all_leave")
    stmt = select(LeaveRequest).join(Employee, Employee.id == LeaveRequest.employee_id)
    if employee_id:
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if region:
        stmt = stmt.where(Employee.region == region)
    if leave_type:
        stmt = stmt.where(LeaveRequest.leave_type_id == leave_type)
    if status_filter:
        stmt = stmt.where(LeaveRequest.status == status_filter)
    if date_from:
        stmt = stmt.where(LeaveRequest.end_date >= date_from)
    if date_to:
        stmt = stmt.where(LeaveRequest.start_date <= date_to)

    rows = session.scalars(
        stmt.order_by(LeaveRequest.start_date.desc()).limit(limit)
    ).all()
    return jsonable([_request_payload(session, r, viewer=user) for r in rows])


@app.get("/api/hr/requests/{request_id}/audit")
def hr_audit(request_id: int, user: UserDep, session: SessionDep):
    """Request → classification → policy → approval chain → ledger.

    The whole point of the append-only ledger and the policy snapshot: one
    screen that answers "who did what, when, and why".
    """
    require(user, "view_audit")
    request = session.get(LeaveRequest, request_id)
    if request is None:
        raise HTTPException(404, "No such request.")

    employee = session.get(Employee, request.employee_id)
    policy = resolve_policy(session, employee, request.leave_type_id, request.start_date)
    steps = session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.request_id == request_id)
        .order_by(ApprovalStep.tier)
    ).all()
    names = {e.id: e.name for e in session.scalars(select(Employee))}

    timeline = [{
        "at": request.submitted_at,
        "actor": employee.name,
        "event": f"Submitted {request.leave_type_id} request, "
                 f"{request.start_date} to {request.end_date}",
    }]
    if request.classified_at:
        timeline.append({
            "at": request.classified_at,
            "actor": "System",
            "event": f"Classified {request.paid_days} days paid, "
                     f"{request.unpaid_days} unpaid",
        })
    for step in steps:
        if step.acted_at:
            timeline.append({
                "at": step.acted_at,
                "actor": names.get(step.acted_by, "?"),
                "event": f"Tier {step.tier} ({step.role}) {step.status}"
                         + (f" — {step.decision_reason}" if step.decision_reason else ""),
            })
        if step.escalated_at:
            timeline.append({
                "at": step.escalated_at, "actor": "System",
                "event": f"Tier {step.tier} escalated after SLA lapsed",
            })

    ledger = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == request.employee_id,
            LeaveLedger.reason.like(f"%request {request_id}%"),
        )
    ).all()
    for row in ledger:
        timeline.append({
            "at": row.created_at, "actor": "System",
            "event": f"Ledger {row.amount} {row.leave_type_id} — {row.reason}",
        })

    timeline.sort(key=lambda e: (e["at"] is None, e["at"]))

    return jsonable({
        "request": _request_payload(session, request, viewer=user),
        "policy": {
            "id": policy.policy_snapshot_id,
            "explain": policy.explain(),
            "entitlement_days_per_year": policy.entitlement_days_per_year,
            "compliance_note": policy.compliance_note,
        } if policy else None,
        "timeline": timeline,
        "ledger": [
            {"id": r.id, "amount": r.amount, "leave_type_id": r.leave_type_id,
             "reason": r.reason, "effective_date": r.effective_date}
            for r in ledger
        ],
    })


@app.get("/api/hr/policies")
def hr_policies(user: UserDep, session: SessionDep, region: str | None = None):
    require(user, "manage_policies")
    stmt = select(OrgPolicy).order_by(
        OrgPolicy.region, OrgPolicy.leave_type_id, OrgPolicy.tenure_min_years,
        OrgPolicy.effective_from,
    )
    if region:
        stmt = stmt.where(OrgPolicy.region == region)
    rows = session.scalars(stmt).all()
    today = dt.date.today()
    return jsonable([
        {
            "id": p.id, "region": p.region, "leave_type_id": p.leave_type_id,
            "tenure_min_years": p.tenure_min_years, "tenure_max_years": p.tenure_max_years,
            "entitlement_days_per_year": p.entitlement_days_per_year,
            "is_paid": p.is_paid, "accrual_method": p.accrual_method,
            "carryover_max_days": p.carryover_max_days,
            "carryover_expiry": p.carryover_expiry,
            "max_consecutive_days": p.max_consecutive_days,
            "min_notice_days": p.min_notice_days,
            "rounding_dp": p.rounding_dp, "proration_method": p.proration_method,
            "leave_year_end": p.leave_year_end, "enforcement": p.enforcement,
            "allow_backdated": p.allow_backdated,
            "allow_negative_balance": p.allow_negative_balance,
            "effective_from": p.effective_from, "effective_to": p.effective_to,
            "policy_year": p.policy_year, "supersedes_id": p.supersedes_id,
            "change_reason": p.change_reason,
            "compliance_note": p.compliance_note,
            "is_current": p.effective_from <= today and (
                p.effective_to is None or p.effective_to >= today
            ),
        }
        for p in rows
    ])


@app.post("/api/hr/policies/preview-roll")
def hr_preview_roll(body: RollForwardBody, user: UserDep, session: SessionDep):
    require(user, "manage_policies")
    plan = preview_roll_forward(
        session, body.region, body.from_year, body.changes,
        default_leave_year_end=body.leave_year_end,
    )
    return jsonable({
        "region": plan.region, "from_year": plan.from_year, "to_year": plan.to_year,
        "old_year_end": plan.old_year_end,
        "new_year_start": plan.new_year_start, "new_year_end": plan.new_year_end,
        "changed_count": plan.changed_count,
        "warnings": plan.warnings,
        "describe": plan.describe(),
        "changes": [
            {"policy_id": c.policy_id, "leave_type_id": c.leave_type_id,
             "tenure_label": c.tenure_label, "is_unchanged": c.is_unchanged,
             "describe": c.describe(),
             "changed": {k: [str(v[0]), str(v[1])] for k, v in c.changed.items()}}
            for c in plan.changes
        ],
    })


@app.post("/api/hr/policies/roll-forward")
def hr_roll_forward(body: RollForwardBody, user: UserDep, session: SessionDep):
    require(user, "manage_policies")
    created = roll_forward_year(
        session, body.region, body.from_year, body.changes,
        actor=user, change_reason=body.change_reason,
        default_leave_year_end=body.leave_year_end, commit=True,
    )
    return jsonable({
        "created": len(created),
        "policy_year": created[0].policy_year if created else None,
        "effective_from": created[0].effective_from if created else None,
    })


@app.get("/api/hr/holidays")
def hr_holidays(
    user: UserDep,
    session: SessionDep,
    region: str = Query(...),
    year: int = Query(default=None),
):
    require(user, "manage_holidays")
    year = year or dt.date.today().year
    statutory = holidays_in_range(
        dt.date(year, 1, 1), dt.date(year, 12, 31), region, session
    )
    overrides = session.scalars(
        select(HolidayOverride)
        .where(
            HolidayOverride.region == region,
            HolidayOverride.holiday_date >= dt.date(year, 1, 1),
            HolidayOverride.holiday_date <= dt.date(year, 12, 31),
        )
        .order_by(HolidayOverride.holiday_date)
    ).all()
    override_dates = {o.holiday_date for o in overrides}

    return jsonable({
        "region": region,
        "year": year,
        "source": f"holidays package — {REGION_CALENDARS.get(region, ('?', '?'))}",
        "holidays": [
            {"date": d, "name": n,
             "kind": "company" if d in override_dates else "public"}
            for d, n in statutory
        ],
        "overrides": [
            {"id": o.id, "date": o.holiday_date, "name": o.name,
             "is_working_day": o.is_working_day}
            for o in overrides
        ],
    })


@app.post("/api/hr/holidays", status_code=201)
def hr_add_holiday(body: HolidayBody, user: UserDep, session: SessionDep):
    require(user, "manage_holidays")
    existing = session.scalars(
        select(HolidayOverride).where(
            HolidayOverride.region == body.region,
            HolidayOverride.holiday_date == body.holiday_date,
        )
    ).first()
    if existing:
        existing.name = body.name
        existing.is_working_day = body.is_working_day
    else:
        session.add(HolidayOverride(
            region=body.region, holiday_date=body.holiday_date,
            name=body.name, is_working_day=body.is_working_day,
        ))
    session.commit()
    return {"ok": True}


@app.post("/api/hr/escalate")
def hr_escalate(user: UserDep, session: SessionDep):
    """Run the overdue-approval sweep on demand."""
    require(user, "view_all_leave")
    steps = escalate_overdue_steps(session, commit=True)
    return {"escalated": len(steps)}


# ===========================================================================
# Notifications — the bell
# ===========================================================================
@app.get("/api/notifications")
def list_notifications(user: UserDep, session: SessionDep, limit: int = 40):
    rows = inbox(session, user.id, limit=limit)
    return {
        "unread": unread_count(session, user.id),
        "items": jsonable([
            {
                "id": n.id, "kind": n.kind, "title": n.title, "body": n.body,
                "link": n.link, "created_at": n.created_at, "read": n.read_at is not None,
            }
            for n in rows
        ]),
    }


@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: int, user: UserDep, session: SessionDep):
    mark_read(session, user.id, notification_id)
    session.commit()
    return {"unread": unread_count(session, user.id)}


@app.post("/api/notifications/read-all")
def read_all_notifications(user: UserDep, session: SessionDep):
    count = mark_all_read(session, user.id)
    session.commit()
    return {"marked": count, "unread": 0}


# ===========================================================================
# HR — leave types
# ===========================================================================
@app.get("/api/hr/leave-types")
def hr_leave_types(user: UserDep, session: SessionDep, include_inactive: bool = True):
    require(user, "view_all_leave")
    return jsonable([
        leave_type_dict(session, t)
        for t in list_leave_types(session, include_inactive=include_inactive)
    ])


@app.post("/api/hr/leave-types", status_code=201)
def hr_create_leave_type(body: LeaveTypeBody, user: UserDep, session: SessionDep):
    require(user, "manage_policies")
    row = create_leave_type(
        session, body.code, body.name, actor=user,
        description=body.description, color_token=body.color_token,
    )
    session.commit()
    return jsonable(leave_type_dict(session, row))


@app.patch("/api/hr/leave-types/{code}")
def hr_update_leave_type(
    code: str, body: LeaveTypePatch, user: UserDep, session: SessionDep
):
    require(user, "manage_policies")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "is_active" in changes:
        active = changes.pop("is_active")
        (reactivate_leave_type if active else retire_leave_type)(
            session, code, actor=user
        )
    if changes:
        update_leave_type(session, code, actor=user, **changes)
    session.commit()
    row = next(
        t for t in list_leave_types(session, include_inactive=True)
        if t.code == code.strip().upper() or t.code == code
    )
    return jsonable(leave_type_dict(session, row))


# ===========================================================================
# HR — the yearly policy cycle
# ===========================================================================
@app.get("/api/hr/policy-status")
def hr_policy_status(user: UserDep, session: SessionDep):
    require(user, "view_all_leave")
    return jsonable([s.to_dict() for s in all_region_status(session)])


@app.post("/api/hr/policies/preview-publish")
def hr_preview_publish(body: PublishBody, user: UserDep, session: SessionDep):
    require(user, "manage_policies")
    return jsonable(preview_publish(
        session, body.region, body.from_year, body.changes,
        default_leave_year_end=body.leave_year_end,
    ))


@app.post("/api/hr/policies/publish", status_code=201)
def hr_publish(body: PublishBody, user: UserDep, session: SessionDep):
    """Open next year's policy, give it a one-year term, and announce it."""
    require(user, "manage_policies")
    created = publish_policy_year(
        session, body.region, body.from_year, body.changes,
        actor=user, change_reason=body.change_reason,
        default_leave_year_end=body.leave_year_end, commit=True,
    )
    return jsonable({
        "created": len(created),
        "policy_year": created[0].policy_year,
        "term_end": created[0].effective_to,
        "status": policy_year_status(session, body.region).to_dict(),
    })


@app.post("/api/hr/policies/remind")
def hr_policy_remind(user: UserDep, session: SessionDep):
    require(user, "manage_policies")
    sent = remind_expiring_policies(session, commit=True)
    return {"sent": len(sent), "markers": sent}


# ===========================================================================
# Simulation — showing that the engine is dynamic
# ===========================================================================
@app.get("/api/simulate/options")
def simulate_options(user: UserDep, session: SessionDep):
    """The catalogue of simulations, and who they can be run against."""
    require(user, "view_all_leave")
    people = session.scalars(
        select(Employee).where(Employee.status == "active").order_by(Employee.name)
    ).all()
    return jsonable({
        "actions": ACTIONS,
        "employees": [
            {"id": p.id, "name": p.name, "region": p.region, "role": p.role}
            for p in people
        ],
    })


@app.get("/api/simulate/{employee_id}")
def simulate_state(employee_id: int, user: UserDep, session: SessionDep):
    require(user, "view_all_leave")
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee.")
    return jsonable({
        "snapshot": snapshot(session, employee),
        "timeline": timeline(session, employee),
    })


@app.post("/api/simulate/{employee_id}/{action}")
def simulate_run(
    employee_id: int, action: str, user: UserDep, session: SessionDep
):
    """Run one simulation for real and report what changed.

    Every action calls the same function a scheduled job or an HR screen
    would; the only thing added here is a snapshot either side, so the change
    has somewhere to be seen. It writes real rows, so it is HR-only.
    """
    require(user, "manage_policies")
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee.")
    result = run_action(session, action, employee, actor=user)
    result["timeline"] = timeline(session, employee)
    return jsonable(result)


# ===========================================================================
# Static frontend (production build)
# ===========================================================================
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Serve the SPA, letting client-side routing own every non-API path."""
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        return FileResponse(_DIST / "index.html")

