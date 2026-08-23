"""SQLAlchemy models for Module 1 — the Leave Engine data layer.

Scope note: this module is schema + seed + insert-time validation ONLY.
No policy resolution, accrual, balance or approval-chain logic lives here
(that is Module 2+). See README.md.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Vocabularies. Kept as plain strings + CHECK constraints so that adding a new
# leave type stays a data change, not a schema migration.
# ---------------------------------------------------------------------------
ACCRUAL_METHODS = ("monthly", "annual_lump", "none")
EMPLOYMENT_STATUSES = ("active", "terminated")
# How a partial year of service is converted into a partial entitlement.
#   "daily"   — eligible calendar days / total days in the cycle (most precise)
#   "monthly" — calendar months with any eligible service / 12 (common HR shorthand)
#   "none"    — no pro-ration; a partial year earns the full entitlement
PRORATION_METHODS = ("daily", "monthly", "none")
APPROVAL_ROLES = ("manager", "hr_admin", "director")
APPROVAL_STEP_STATUSES = ("pending", "active", "approved", "rejected", "forwarded")
LEAVE_REQUEST_STATUSES = ("pending", "approved", "rejected", "cancelled")
CONDITION_OPERATORS = (">", ">=", "<", "<=", "==", "!=")
# Whether a violated policy limit blocks the request or merely warns.
# "block" is the default: notice periods, maximum consecutive days and
# backdating are real rules, not advice. "warn" is the escape hatch for a
# region that genuinely wants them advisory, set in data, not code.
ENFORCEMENT_LEVELS = ("block", "warn")
# What a ledger row represents. Carry-over days are tracked separately from
# the current year's so they can expire on their own schedule.
LEDGER_BUCKETS = ("current", "carryover")
# Directory role, used to decide who may approve which tier and who may view
# whose dashboard.
EMPLOYEE_ROLES = ("employee", "manager", "hr_admin", "director")

# Sentinels used instead of NULL where a range is open-ended, so that Postgres
# range/exclusion logic and ORDER BY stay simple. `tenure_max_years = NULL`
# still means "and above" per the design doc; effective_to = NULL means "open".
FAR_FUTURE = dt.date(9999, 12, 31)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    inner = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({inner})"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 1. org_policies — the single source of truth the whole engine reads from
# ---------------------------------------------------------------------------
class OrgPolicy(Base):
    __tablename__ = "org_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    region: Mapped[str] = mapped_column(String(64), nullable=False)
    legal_entity: Mapped[str] = mapped_column(String(128), nullable=False)
    leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)

    # Tenure bracket, in years. max = NULL means "and above".
    tenure_min_years: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    tenure_max_years: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    entitlement_days_per_year: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accrual_method: Mapped[str] = mapped_column(String(16), nullable=False)

    carryover_max_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    # Stored as "MM-DD" — a recurring calendar day, not a specific year.
    carryover_expiry: Mapped[str | None] = mapped_column(String(5), nullable=True)

    max_consecutive_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_notice_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # --- rounding and pro-ration, as POLICY rather than code ---------------
    # Decimal places every derived amount is rounded to (ROUND_HALF_UP).
    # 3 gives 10/12 = 0.833; the year-end true-up closes the residual so a
    # full cycle still sums to the entitlement exactly. Stored per policy row
    # so a region whose payroll insists on 2dp can say so in data.
    rounding_dp: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # How a partial year of service is pro-rated. See PRORATION_METHODS.
    proration_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily"
    )
    # Last day of the ORGANISATION's leave year for this region, as "MM-DD"
    # (e.g. "03-31" in India, "12-31" in the US). This is what makes
    # "joined partway through the year" a meaningful statement at all.
    #
    # NULL means the cycle is anniversary-aligned instead — each employee's
    # own leave year starts on their join date, so they are never a mid-year
    # joiner and the annual entitlement is never partial at the front end.
    # Monthly-accrual types are normally left NULL, because partial service is
    # already handled month by month; annual-lump types normally set it.
    leave_year_end: Mapped[str | None] = mapped_column(String(5), nullable=True)

    # --- request-time enforcement -----------------------------------------
    # "block" (default) rejects a request that breaks min_notice_days or
    # max_consecutive_days; "warn" records it and lets it through. Per policy
    # row, so one region can be strict while another is advisory.
    enforcement: Mapped[str] = mapped_column(String(8), nullable=False, default="block")
    # Backdated leave (start date in the past) is rejected unless this is set.
    allow_backdated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # May a balance go below zero? Default false: a request that would
    # overdraw falls to unpaid rather than borrowing against the future.
    allow_negative_balance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- annual policy lifecycle ------------------------------------------
    # Which leave year this row is the policy FOR. Two rows for the same
    # region/type/bracket in different years are versions of one another, not
    # a conflict — their effective windows do not overlap.
    policy_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The row this one replaced, when it was created by a year roll-forward.
    # Chains backwards so "what did this look like in 2024?" is one join.
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_policies.id", ondelete="SET NULL"), nullable=True
    )
    # Who authored it, and why it changed. Required by the roll-forward API
    # so a policy set can never silently change hands.
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # REQUIRED, non-empty. The only compliance control in the system: it forces
    # a human to state which law/minimum this number satisfies at write time.
    compliance_note: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(btrim(compliance_note)) > 0", name="ck_org_policies_compliance_note"),
        CheckConstraint(
            "tenure_max_years IS NULL OR tenure_max_years > tenure_min_years",
            name="ck_org_policies_tenure_range",
        ),
        CheckConstraint("tenure_min_years >= 0", name="ck_org_policies_tenure_min_nonneg"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_org_policies_effective_range",
        ),
        CheckConstraint("entitlement_days_per_year >= 0", name="ck_org_policies_entitlement_nonneg"),
        CheckConstraint("carryover_max_days >= 0", name="ck_org_policies_carryover_nonneg"),
        CheckConstraint("min_notice_days >= 0", name="ck_org_policies_notice_nonneg"),
        CheckConstraint(
            "carryover_expiry IS NULL OR carryover_expiry ~ '^[0-1][0-9]-[0-3][0-9]$'",
            name="ck_org_policies_carryover_expiry_format",
        ),
        CheckConstraint(_in_list("accrual_method", ACCRUAL_METHODS), name="ck_org_policies_accrual_method"),
        CheckConstraint(
            _in_list("proration_method", PRORATION_METHODS), name="ck_org_policies_proration_method"
        ),
        CheckConstraint("rounding_dp BETWEEN 0 AND 3", name="ck_org_policies_rounding_dp"),
        CheckConstraint(
            _in_list("enforcement", ENFORCEMENT_LEVELS), name="ck_org_policies_enforcement"
        ),
        CheckConstraint(
            "leave_year_end IS NULL OR leave_year_end ~ '^[0-1][0-9]-[0-3][0-9]$'",
            name="ck_org_policies_leave_year_end_format",
        ),
        Index("ix_org_policies_lookup", "region", "leave_type_id", "tenure_min_years"),
        Index("ix_org_policies_year", "region", "policy_year"),
        # NOTE: the overlapping-tenure-range EXCLUDE constraint
        # (ex_org_policies_no_overlap) is added in the migration, because it
        # needs btree_gist + range expressions that SQLAlchemy core cannot
        # express declaratively.
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        hi = "+" if self.tenure_max_years is None else f"-{self.tenure_max_years}"
        return (
            f"<OrgPolicy {self.region} {self.leave_type_id} "
            f"{self.tenure_min_years}{hi}yr -> {self.entitlement_days_per_year}d/yr>"
        )


# ---------------------------------------------------------------------------
# 2. employee — needed by everything downstream (region + join_date = tenure)
# ---------------------------------------------------------------------------
class Employee(Base):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    join_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )

    # --- employment shape --------------------------------------------------
    # 1.000 = full-time, 0.500 = a half-time schedule. Entitlement is scaled by
    # this at resolve time, so one policy row serves every working pattern in a
    # region and HR never authors a parallel set of part-time rows.
    employment_fraction: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("1.000")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Last day of service. Accrual stops here and the final cycle is pro-rated.
    exit_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Directory role. Drives approver authorization (who may sign off which
    # tier) and dashboard visibility (who may look at whose balance).
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="employee")

    # --- login -------------------------------------------------------------
    # PBKDF2-SHA256, salted per user. NULL means the account cannot log in,
    # which is the correct state for a record that exists for accrual purposes
    # but has no system access yet.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    manager: Mapped["Employee | None"] = relationship(remote_side=[id])

    __table_args__ = (
        CheckConstraint("manager_id IS NULL OR manager_id <> id", name="ck_employee_not_own_manager"),
        CheckConstraint(
            "employment_fraction > 0 AND employment_fraction <= 1",
            name="ck_employee_employment_fraction",
        ),
        CheckConstraint(_in_list("status", EMPLOYMENT_STATUSES), name="ck_employee_status"),
        CheckConstraint(_in_list("role", EMPLOYEE_ROLES), name="ck_employee_role"),
        CheckConstraint(
            "exit_date IS NULL OR exit_date >= join_date", name="ck_employee_exit_after_join"
        ),
        # A terminated employee without a leaving date would accrue forever.
        CheckConstraint(
            "status <> 'terminated' OR exit_date IS NOT NULL",
            name="ck_employee_terminated_needs_exit_date",
        ),
        Index("ix_employee_region", "region"),
        Index("ix_employee_status", "status"),
        Index("ix_employee_department", "department"),
    )

    @property
    def is_part_time(self) -> bool:
        return self.employment_fraction < Decimal("1")

    def is_employed_on(self, on_date: dt.date) -> bool:
        """In service on this date — the eligibility test accrual depends on."""
        if on_date < self.join_date:
            return False
        if self.exit_date is not None and on_date > self.exit_date:
            return False
        return True

    def __repr__(self) -> str:  # pragma: no cover
        shape = "" if self.employment_fraction == 1 else f" @{self.employment_fraction}FTE"
        exit_note = f" exited={self.exit_date}" if self.exit_date else ""
        return f"<Employee {self.name} {self.region} joined={self.join_date}{shape}{exit_note}>"


# ---------------------------------------------------------------------------
# 3. employee_exceptions — optional per-employee override, checked first by
#    resolvePolicy() in Module 2.
# ---------------------------------------------------------------------------
class EmployeeException(Base):
    __tablename__ = "employee_exceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)
    entitlement_days_per_year: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    employee: Mapped[Employee] = relationship(foreign_keys=[employee_id])

    __table_args__ = (
        CheckConstraint("length(btrim(reason)) > 0", name="ck_employee_exceptions_reason"),
        CheckConstraint("entitlement_days_per_year >= 0", name="ck_employee_exceptions_entitlement"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employee_exceptions_effective_range",
        ),
        Index("ix_employee_exceptions_lookup", "employee_id", "leave_type_id"),
    )


# ---------------------------------------------------------------------------
# 4. leave_request
# ---------------------------------------------------------------------------
class LeaveRequest(Base):
    __tablename__ = "leave_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    duration_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- half-day support -------------------------------------------------
    # A request may start and/or end on a half day, so 4.5 days is expressible.
    # Both flags on a single-day request means a half day, not a whole one.
    start_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    end_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # When the employee actually submitted. Notice periods are measured from
    # THIS, not from the current system clock, so re-evaluating an old request
    # later cannot retroactively turn it into a short-notice violation.
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- classification snapshot -----------------------------------------
    # The paid/unpaid split as computed at submission. Kept so the figures the
    # employee saw are auditable, and so the re-classification at approval
    # time can be compared against them.
    paid_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    unpaid_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    classified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # JSON: [{"leave_type": "EL", "days": "3"}, {"leave_type": "CL", "days": "3"}]
    # Settlement writes one ledger row per source, so the breakdown has to
    # survive from submission through to approval.
    classification_sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- three DISTINCT reasons, deliberately three columns ---------------
    # These used to share one field, which meant a private note could make the
    # system believe a policy override had been justified, and cancellation
    # text overwrote both. They answer different questions and have different
    # audiences, so they are stored — and permissioned — separately.
    #
    #   employee_reason     "why I want this leave"    employee + HR only
    #   override_reason     "why a policy limit was    approvers + HR
    #                        deliberately breached"
    #   cancellation_reason "why this was withdrawn"   everyone on the request
    employee_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    employee: Mapped[Employee] = relationship()
    steps: Mapped[list["ApprovalStep"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_request_date_order"),
        CheckConstraint("duration_days > 0", name="ck_leave_request_duration_positive"),
        CheckConstraint(_in_list("status", LEAVE_REQUEST_STATUSES), name="ck_leave_request_status"),
        CheckConstraint(
            "paid_days IS NULL OR unpaid_days IS NULL "
            "OR paid_days + unpaid_days = duration_days",
            name="ck_leave_request_split_conserves",
        ),
        Index("ix_leave_request_employee", "employee_id", "start_date"),
        Index("ix_leave_request_open", "employee_id", "status"),
        # NOTE: the no-overlapping-requests EXCLUDE constraint
        # (ex_leave_request_no_overlap) is added in migration 0003 — it needs
        # a daterange expression SQLAlchemy cannot express declaratively.
    )


# ---------------------------------------------------------------------------
# 5. leave_ledger — append-only source of truth for balances
# ---------------------------------------------------------------------------
class LeaveLedger(Base):
    __tablename__ = "leave_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)
    # Signed: positive = accrual/grant, negative = deduction.
    amount: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    # --- carry-over tracking ---------------------------------------------
    # "current" days belong to the leave year they were accrued in;
    # "carryover" days were brought forward and die on `expires_on`.
    # Keeping them in separate buckets is what lets the balance show
    # "12 days, of which 3 expire on 31 March" instead of one opaque number.
    bucket: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    # Last day this credit can be spent. NULL = never expires.
    expires_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # Which exact org_policies row produced this entry — makes every historical
    # accrual explainable after the fact.
    policy_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_policies.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    employee: Mapped[Employee] = relationship()
    policy_snapshot: Mapped[OrgPolicy | None] = relationship()

    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_leave_ledger_amount_nonzero"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_leave_ledger_reason"),
        CheckConstraint(_in_list("bucket", LEDGER_BUCKETS), name="ck_leave_ledger_bucket"),
        Index("ix_leave_ledger_balance", "employee_id", "leave_type_id", "effective_date"),
        # Covers the live-balance sum including the expiry filter, so the
        # dashboard stays an index-only scan as the ledger grows.
        Index(
            "ix_leave_ledger_balance_expiry",
            "employee_id", "leave_type_id", "effective_date", "expires_on",
        ),
    )


# ---------------------------------------------------------------------------
# 6. approval_rules — routing as rows, not code
# ---------------------------------------------------------------------------
class ApprovalRule(Base):
    __tablename__ = "approval_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    condition_field: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(4), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    adds_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    tier_order: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- SLA and escalation, configured rather than coded -----------------
    # Hours this tier has to act before the step is considered overdue.
    # NULL = no deadline (the old behaviour: pending forever).
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Role to escalate to when the SLA lapses. NULL = flag it, escalate to
    # nobody. Escalation never auto-approves — that would be a policy
    # decision the system is not entitled to make on HR's behalf.
    escalate_to_role: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint(_in_list("adds_tier", APPROVAL_ROLES), name="ck_approval_rules_adds_tier"),
        CheckConstraint("sla_hours IS NULL OR sla_hours > 0", name="ck_approval_rules_sla_hours"),
        CheckConstraint(
            "escalate_to_role IS NULL OR escalate_to_role IN "
            "('manager', 'hr_admin', 'director')",
            name="ck_approval_rules_escalate_to_role",
        ),
        CheckConstraint(_in_list("operator", CONDITION_OPERATORS), name="ck_approval_rules_operator"),
        CheckConstraint("tier_order >= 1", name="ck_approval_rules_tier_order"),
        UniqueConstraint(
            "condition_field", "operator", "value", "adds_tier", name="uq_approval_rules_condition"
        ),
    )


# ---------------------------------------------------------------------------
# 7. approval_steps — the materialised chain for one request
# ---------------------------------------------------------------------------
class ApprovalStep(Base):
    __tablename__ = "approval_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("leave_request.id", ondelete="CASCADE"), nullable=False
    )
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    acted_by: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    acted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- who owns this step ----------------------------------------------
    # The specific person expected to act, resolved when the chain is created.
    # Without this a tier is only a role, and nobody knows whose queue it is.
    assigned_approver_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    # Free text captured with the decision — why it was approved or rejected.
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- SLA / escalation --------------------------------------------------
    # When this step became active, and when it is considered overdue.
    activated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when someone other than the assigned approver acted under a
    # delegation, so the audit trail shows both people.
    acted_on_behalf_of: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )

    # --- explicit forwarding ----------------------------------------------
    # Routing is now the approver's decision, not the system's: a manager
    # chooses to send a request to HR. These record that choice.
    forwarded_to_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    forwarded_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )

    request: Mapped[LeaveRequest] = relationship(back_populates="steps")
    assigned_approver: Mapped["Employee | None"] = relationship(
        foreign_keys=[assigned_approver_id]
    )

    __table_args__ = (
        CheckConstraint(_in_list("role", APPROVAL_ROLES), name="ck_approval_steps_role"),
        CheckConstraint(_in_list("status", APPROVAL_STEP_STATUSES), name="ck_approval_steps_status"),
        CheckConstraint("tier >= 1", name="ck_approval_steps_tier"),
        UniqueConstraint("request_id", "tier", name="uq_approval_steps_request_tier"),
    )


# ---------------------------------------------------------------------------
# 8. holiday_overrides — company-specific additions to the statutory calendar
# ---------------------------------------------------------------------------
class HolidayOverride(Base):
    """Company holidays, and suppressions of statutory ones.

    The statutory calendar itself comes from the `holidays` PyPI package,
    which already knows that Pongal is a Tamil Nadu holiday and Thanksgiving
    a US one — maintaining that by hand would be a standing source of bugs.
    This table is only for what a library cannot know: an office shutdown
    between Christmas and New Year, a founding-day holiday, or a statutory
    day this employer does not in fact observe (`is_working_day = True`).
    """

    __tablename__ = "holiday_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    holiday_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # False (default) = this IS a holiday. True = force it back to a working
    # day, overriding the library.
    is_working_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("region", "holiday_date", name="uq_holiday_overrides_region_date"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_holiday_overrides_name"),
        Index("ix_holiday_overrides_lookup", "region", "holiday_date"),
    )


# ---------------------------------------------------------------------------
# 9. substitution_rules — the fallback order, as data
# ---------------------------------------------------------------------------
class SubstitutionRule(Base):
    """Which leave types to draw from, in order, when the requested one runs out.

    Previously hardcoded as a Python list. As rows, a region can have its own
    order — Texas has no Casual Leave, so its chain genuinely differs — and HR
    can reorder without a deploy.

    `region = NULL` is the default chain, used when no region-specific rule
    exists for that leave type.
    """

    __tablename__ = "substitution_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 0 is the requested type itself; higher numbers are fallbacks in order.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    fallback_leave_type_id: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "leave_type_id", "region", "position", name="uq_substitution_rules_position"
        ),
        CheckConstraint("position >= 0", name="ck_substitution_rules_position"),
        Index("ix_substitution_rules_lookup", "leave_type_id", "region", "position"),
    )


# ---------------------------------------------------------------------------
# 10. approval_delegations — cover for an absent approver
# ---------------------------------------------------------------------------
class ApprovalDelegation(Base):
    """`delegator` hands their approval authority to `delegate` for a window.

    Without this, an approver going on leave stalls every request in their
    queue — which is exactly the situation a leave system should handle well.
    """

    __tablename__ = "approval_delegations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delegator_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    delegate_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    from_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    to_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    delegator: Mapped[Employee] = relationship(foreign_keys=[delegator_id])
    delegate: Mapped[Employee] = relationship(foreign_keys=[delegate_id])

    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="ck_approval_delegations_dates"),
        CheckConstraint("delegator_id <> delegate_id", name="ck_approval_delegations_distinct"),
        Index("ix_approval_delegations_lookup", "delegator_id", "from_date", "to_date"),
    )


# ---------------------------------------------------------------------------
# 11. outbox — transactional event publication
# ---------------------------------------------------------------------------
class OutboxEvent(Base):
    """Events written in the SAME transaction as the state change they describe.

    Replaces the in-memory callback list the approval engine used to fire on
    final approval. That had two defects: the callback ran *before* the
    transaction committed (so a listener could act on an approval that then
    rolled back), and it was process-local (so nothing outside that one Python
    process ever saw it).

    Writing a row here instead means the event is committed atomically with
    the approval, and a separate relay can publish it at-least-once to
    whatever the deployment actually uses. This is the standard transactional
    outbox pattern; the relay is deliberately not implemented here, because
    the target queue is a deployment decision.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    # The entity this event is about, for idempotent consumers.
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The relay's claim query: unpublished events, oldest first.
        Index("ix_outbox_unpublished", "published_at", "id"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )


# ---------------------------------------------------------------------------
# 12. leave_types — the catalogue, so HR can add a type without a migration
# ---------------------------------------------------------------------------
class LeaveType(Base):
    """A kind of leave the organisation offers.

    Leave types were previously bare string codes with no table behind them,
    so "add Bereavement Leave" was a code change. They are now rows: HR adds
    the type here, then authors a policy for it per region.

    The type says what it IS; `org_policies` says what it's WORTH — a type
    with no policy for a region simply is not offered there, which is how
    Texas already has no Casual Leave.
    """

    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which categorical slot in the validated palette this type wears.
    color_token: Mapped[str] = mapped_column(String(16), nullable=False, default="series-1")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The terminal fallback in a substitution chain: never draws on a balance.
    is_unpaid_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(btrim(code)) > 0", name="ck_leave_types_code"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_leave_types_name"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LeaveType {self.code} {self.name}>"


# ---------------------------------------------------------------------------
# 13. notifications — what reaches a person's bell
# ---------------------------------------------------------------------------
class Notification(Base):
    """An in-app message for one employee.

    Deliberately simple and in-band: the transactional outbox already handles
    reliable delivery to EXTERNAL systems. This is the internal inbox, written
    in the same transaction as the thing it describes, so a notification can
    never survive a rolled-back approval.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_notifications_inbox", "employee_id", "read_at", "id"),
    )
