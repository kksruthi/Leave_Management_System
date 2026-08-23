--
-- PostgreSQL database dump
--

\restrict 5rvOEv6CTUb9pKCaYrweIeF4exduxcSyxoDLJnuu2C48b7R1gjIJHnHTn6xaNDt

-- Dumped from database version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';


--
-- Name: leave_ledger_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.leave_ledger_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'leave_ledger is append-only: % on row id=% is not permitted. '
                'Append a reversing entry instead.',
                TG_OP, OLD.id
                USING ERRCODE = 'restrict_violation';
        END;
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: approval_delegations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_delegations (
    id integer NOT NULL,
    delegator_id integer NOT NULL,
    delegate_id integer NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    reason text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_approval_delegations_dates CHECK ((to_date >= from_date)),
    CONSTRAINT ck_approval_delegations_distinct CHECK ((delegator_id <> delegate_id))
);


--
-- Name: approval_delegations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.approval_delegations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: approval_delegations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.approval_delegations_id_seq OWNED BY public.approval_delegations.id;


--
-- Name: approval_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_rules (
    id integer NOT NULL,
    condition_field character varying(32) NOT NULL,
    operator character varying(4) NOT NULL,
    value character varying(64) NOT NULL,
    adds_tier character varying(16) NOT NULL,
    tier_order integer NOT NULL,
    description text,
    is_active boolean NOT NULL,
    sla_hours integer,
    escalate_to_role character varying(16),
    CONSTRAINT ck_approval_rules_adds_tier CHECK (((adds_tier)::text = ANY ((ARRAY['manager'::character varying, 'hr_admin'::character varying, 'director'::character varying])::text[]))),
    CONSTRAINT ck_approval_rules_escalate_to_role CHECK (((escalate_to_role IS NULL) OR ((escalate_to_role)::text = ANY ((ARRAY['manager'::character varying, 'hr_admin'::character varying, 'director'::character varying])::text[])))),
    CONSTRAINT ck_approval_rules_operator CHECK (((operator)::text = ANY ((ARRAY['>'::character varying, '>='::character varying, '<'::character varying, '<='::character varying, '=='::character varying, '!='::character varying])::text[]))),
    CONSTRAINT ck_approval_rules_sla_hours CHECK (((sla_hours IS NULL) OR (sla_hours > 0))),
    CONSTRAINT ck_approval_rules_tier_order CHECK ((tier_order >= 1))
);


--
-- Name: approval_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.approval_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: approval_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.approval_rules_id_seq OWNED BY public.approval_rules.id;


--
-- Name: approval_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_steps (
    id integer NOT NULL,
    request_id integer NOT NULL,
    tier integer NOT NULL,
    role character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    routing_reason text,
    acted_by integer,
    acted_at timestamp with time zone,
    assigned_approver_id integer,
    decision_reason text,
    activated_at timestamp with time zone,
    due_at timestamp with time zone,
    escalated_at timestamp with time zone,
    acted_on_behalf_of integer,
    CONSTRAINT ck_approval_steps_role CHECK (((role)::text = ANY ((ARRAY['manager'::character varying, 'hr_admin'::character varying, 'director'::character varying])::text[]))),
    CONSTRAINT ck_approval_steps_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'active'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_approval_steps_tier CHECK ((tier >= 1))
);


--
-- Name: approval_steps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.approval_steps_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: approval_steps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.approval_steps_id_seq OWNED BY public.approval_steps.id;


--
-- Name: employee; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    email character varying(255) NOT NULL,
    join_date date NOT NULL,
    region character varying(64) NOT NULL,
    manager_id integer,
    employment_fraction numeric(4,3) DEFAULT 1.000 NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    exit_date date,
    role character varying(16) DEFAULT 'employee'::character varying NOT NULL,
    CONSTRAINT ck_employee_employment_fraction CHECK (((employment_fraction > (0)::numeric) AND (employment_fraction <= (1)::numeric))),
    CONSTRAINT ck_employee_exit_after_join CHECK (((exit_date IS NULL) OR (exit_date >= join_date))),
    CONSTRAINT ck_employee_not_own_manager CHECK (((manager_id IS NULL) OR (manager_id <> id))),
    CONSTRAINT ck_employee_role CHECK (((role)::text = ANY ((ARRAY['employee'::character varying, 'manager'::character varying, 'hr_admin'::character varying, 'director'::character varying])::text[]))),
    CONSTRAINT ck_employee_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'terminated'::character varying])::text[]))),
    CONSTRAINT ck_employee_terminated_needs_exit_date CHECK ((((status)::text <> 'terminated'::text) OR (exit_date IS NOT NULL)))
);


--
-- Name: employee_exceptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_exceptions (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    leave_type_id character varying(16) NOT NULL,
    entitlement_days_per_year numeric(6,2) NOT NULL,
    reason text NOT NULL,
    approved_by integer,
    effective_from date NOT NULL,
    effective_to date,
    CONSTRAINT ck_employee_exceptions_effective_range CHECK (((effective_to IS NULL) OR (effective_to >= effective_from))),
    CONSTRAINT ck_employee_exceptions_entitlement CHECK ((entitlement_days_per_year >= (0)::numeric)),
    CONSTRAINT ck_employee_exceptions_reason CHECK ((length(btrim(reason)) > 0))
);


--
-- Name: employee_exceptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.employee_exceptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: employee_exceptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_exceptions_id_seq OWNED BY public.employee_exceptions.id;


--
-- Name: employee_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.employee_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: employee_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_id_seq OWNED BY public.employee.id;


--
-- Name: holiday_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.holiday_overrides (
    id integer NOT NULL,
    region character varying(64) NOT NULL,
    holiday_date date NOT NULL,
    name character varying(128) NOT NULL,
    is_working_day boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_holiday_overrides_name CHECK ((length(btrim((name)::text)) > 0))
);


--
-- Name: holiday_overrides_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.holiday_overrides_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: holiday_overrides_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.holiday_overrides_id_seq OWNED BY public.holiday_overrides.id;


--
-- Name: leave_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_ledger (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    leave_type_id character varying(16) NOT NULL,
    amount numeric(8,3) NOT NULL,
    reason text NOT NULL,
    effective_date date NOT NULL,
    policy_snapshot_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bucket character varying(16) DEFAULT 'current'::character varying NOT NULL,
    expires_on date,
    CONSTRAINT ck_leave_ledger_amount_nonzero CHECK ((amount <> (0)::numeric)),
    CONSTRAINT ck_leave_ledger_bucket CHECK (((bucket)::text = ANY ((ARRAY['current'::character varying, 'carryover'::character varying])::text[]))),
    CONSTRAINT ck_leave_ledger_reason CHECK ((length(btrim(reason)) > 0))
);


--
-- Name: leave_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.leave_ledger_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: leave_ledger_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.leave_ledger_id_seq OWNED BY public.leave_ledger.id;


--
-- Name: leave_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_request (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    leave_type_id character varying(16) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    duration_days numeric(6,2) NOT NULL,
    status character varying(16) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    start_half_day boolean DEFAULT false NOT NULL,
    end_half_day boolean DEFAULT false NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    paid_days numeric(6,2),
    unpaid_days numeric(6,2),
    classified_at timestamp with time zone,
    override_reason text,
    CONSTRAINT ck_leave_request_date_order CHECK ((end_date >= start_date)),
    CONSTRAINT ck_leave_request_duration_positive CHECK ((duration_days > (0)::numeric)),
    CONSTRAINT ck_leave_request_split_conserves CHECK (((paid_days IS NULL) OR (unpaid_days IS NULL) OR ((paid_days + unpaid_days) = duration_days))),
    CONSTRAINT ck_leave_request_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: leave_request_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.leave_request_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: leave_request_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.leave_request_id_seq OWNED BY public.leave_request.id;


--
-- Name: org_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_policies (
    id integer NOT NULL,
    region character varying(64) NOT NULL,
    legal_entity character varying(128) NOT NULL,
    leave_type_id character varying(16) NOT NULL,
    tenure_min_years numeric(5,2) NOT NULL,
    tenure_max_years numeric(5,2),
    entitlement_days_per_year numeric(6,2) NOT NULL,
    is_paid boolean NOT NULL,
    accrual_method character varying(16) NOT NULL,
    carryover_max_days numeric(6,2) NOT NULL,
    carryover_expiry character varying(5),
    max_consecutive_days integer,
    min_notice_days integer NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    compliance_note text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    rounding_dp integer DEFAULT 3 NOT NULL,
    proration_method character varying(16) DEFAULT 'daily'::character varying NOT NULL,
    leave_year_end character varying(5),
    enforcement character varying(8) DEFAULT 'block'::character varying NOT NULL,
    allow_backdated boolean DEFAULT false NOT NULL,
    allow_negative_balance boolean DEFAULT false NOT NULL,
    policy_year integer,
    supersedes_id integer,
    created_by_id integer,
    change_reason text,
    CONSTRAINT ck_org_policies_accrual_method CHECK (((accrual_method)::text = ANY ((ARRAY['monthly'::character varying, 'annual_lump'::character varying, 'none'::character varying])::text[]))),
    CONSTRAINT ck_org_policies_carryover_expiry_format CHECK (((carryover_expiry IS NULL) OR ((carryover_expiry)::text ~ '^[0-1][0-9]-[0-3][0-9]$'::text))),
    CONSTRAINT ck_org_policies_carryover_nonneg CHECK ((carryover_max_days >= (0)::numeric)),
    CONSTRAINT ck_org_policies_compliance_note CHECK ((length(btrim(compliance_note)) > 0)),
    CONSTRAINT ck_org_policies_effective_range CHECK (((effective_to IS NULL) OR (effective_to >= effective_from))),
    CONSTRAINT ck_org_policies_enforcement CHECK (((enforcement)::text = ANY ((ARRAY['block'::character varying, 'warn'::character varying])::text[]))),
    CONSTRAINT ck_org_policies_entitlement_nonneg CHECK ((entitlement_days_per_year >= (0)::numeric)),
    CONSTRAINT ck_org_policies_leave_year_end_format CHECK (((leave_year_end IS NULL) OR ((leave_year_end)::text ~ '^[0-1][0-9]-[0-3][0-9]$'::text))),
    CONSTRAINT ck_org_policies_notice_nonneg CHECK ((min_notice_days >= 0)),
    CONSTRAINT ck_org_policies_proration_method CHECK (((proration_method)::text = ANY ((ARRAY['daily'::character varying, 'monthly'::character varying, 'none'::character varying])::text[]))),
    CONSTRAINT ck_org_policies_rounding_dp CHECK (((rounding_dp >= 0) AND (rounding_dp <= 3))),
    CONSTRAINT ck_org_policies_tenure_min_nonneg CHECK ((tenure_min_years >= (0)::numeric)),
    CONSTRAINT ck_org_policies_tenure_range CHECK (((tenure_max_years IS NULL) OR (tenure_max_years > tenure_min_years)))
);


--
-- Name: org_policies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.org_policies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: org_policies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.org_policies_id_seq OWNED BY public.org_policies.id;


--
-- Name: outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbox (
    id integer NOT NULL,
    topic character varying(64) NOT NULL,
    aggregate_type character varying(32) NOT NULL,
    aggregate_id integer NOT NULL,
    payload text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text
);


--
-- Name: outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.outbox_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.outbox_id_seq OWNED BY public.outbox.id;


--
-- Name: substitution_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.substitution_rules (
    id integer NOT NULL,
    leave_type_id character varying(16) NOT NULL,
    region character varying(64),
    "position" integer NOT NULL,
    fallback_leave_type_id character varying(16) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT ck_substitution_rules_position CHECK (("position" >= 0))
);


--
-- Name: substitution_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.substitution_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: substitution_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.substitution_rules_id_seq OWNED BY public.substitution_rules.id;


--
-- Name: approval_delegations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_delegations ALTER COLUMN id SET DEFAULT nextval('public.approval_delegations_id_seq'::regclass);


--
-- Name: approval_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_rules ALTER COLUMN id SET DEFAULT nextval('public.approval_rules_id_seq'::regclass);


--
-- Name: approval_steps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps ALTER COLUMN id SET DEFAULT nextval('public.approval_steps_id_seq'::regclass);


--
-- Name: employee id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee ALTER COLUMN id SET DEFAULT nextval('public.employee_id_seq'::regclass);


--
-- Name: employee_exceptions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exceptions ALTER COLUMN id SET DEFAULT nextval('public.employee_exceptions_id_seq'::regclass);


--
-- Name: holiday_overrides id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday_overrides ALTER COLUMN id SET DEFAULT nextval('public.holiday_overrides_id_seq'::regclass);


--
-- Name: leave_ledger id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger ALTER COLUMN id SET DEFAULT nextval('public.leave_ledger_id_seq'::regclass);


--
-- Name: leave_request id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request ALTER COLUMN id SET DEFAULT nextval('public.leave_request_id_seq'::regclass);


--
-- Name: org_policies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_policies ALTER COLUMN id SET DEFAULT nextval('public.org_policies_id_seq'::regclass);


--
-- Name: outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox ALTER COLUMN id SET DEFAULT nextval('public.outbox_id_seq'::regclass);


--
-- Name: substitution_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.substitution_rules ALTER COLUMN id SET DEFAULT nextval('public.substitution_rules_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: approval_delegations approval_delegations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_delegations
    ADD CONSTRAINT approval_delegations_pkey PRIMARY KEY (id);


--
-- Name: approval_rules approval_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_rules
    ADD CONSTRAINT approval_rules_pkey PRIMARY KEY (id);


--
-- Name: approval_steps approval_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT approval_steps_pkey PRIMARY KEY (id);


--
-- Name: employee employee_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_email_key UNIQUE (email);


--
-- Name: employee_exceptions employee_exceptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exceptions
    ADD CONSTRAINT employee_exceptions_pkey PRIMARY KEY (id);


--
-- Name: employee employee_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_pkey PRIMARY KEY (id);


--
-- Name: leave_request ex_leave_request_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT ex_leave_request_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(start_date, end_date, '[]'::text) WITH &&) WHERE (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying])::text[])));


--
-- Name: org_policies ex_org_policies_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_policies
    ADD CONSTRAINT ex_org_policies_no_overlap EXCLUDE USING gist (region WITH =, leave_type_id WITH =, numrange(tenure_min_years, tenure_max_years, '[)'::text) WITH &&, daterange(effective_from, effective_to, '[]'::text) WITH &&);


--
-- Name: holiday_overrides holiday_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday_overrides
    ADD CONSTRAINT holiday_overrides_pkey PRIMARY KEY (id);


--
-- Name: leave_ledger leave_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_pkey PRIMARY KEY (id);


--
-- Name: leave_request leave_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_pkey PRIMARY KEY (id);


--
-- Name: org_policies org_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_policies
    ADD CONSTRAINT org_policies_pkey PRIMARY KEY (id);


--
-- Name: outbox outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox
    ADD CONSTRAINT outbox_pkey PRIMARY KEY (id);


--
-- Name: substitution_rules substitution_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.substitution_rules
    ADD CONSTRAINT substitution_rules_pkey PRIMARY KEY (id);


--
-- Name: approval_rules uq_approval_rules_condition; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_rules
    ADD CONSTRAINT uq_approval_rules_condition UNIQUE (condition_field, operator, value, adds_tier);


--
-- Name: approval_steps uq_approval_steps_request_role; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT uq_approval_steps_request_role UNIQUE (request_id, role);


--
-- Name: approval_steps uq_approval_steps_request_tier; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT uq_approval_steps_request_tier UNIQUE (request_id, tier);


--
-- Name: holiday_overrides uq_holiday_overrides_region_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.holiday_overrides
    ADD CONSTRAINT uq_holiday_overrides_region_date UNIQUE (region, holiday_date);


--
-- Name: substitution_rules uq_substitution_rules_position; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.substitution_rules
    ADD CONSTRAINT uq_substitution_rules_position UNIQUE (leave_type_id, region, "position");


--
-- Name: ix_approval_delegations_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_approval_delegations_lookup ON public.approval_delegations USING btree (delegator_id, from_date, to_date);


--
-- Name: ix_employee_exceptions_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_exceptions_lookup ON public.employee_exceptions USING btree (employee_id, leave_type_id);


--
-- Name: ix_employee_region; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_region ON public.employee USING btree (region);


--
-- Name: ix_employee_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_employee_status ON public.employee USING btree (status);


--
-- Name: ix_holiday_overrides_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_holiday_overrides_lookup ON public.holiday_overrides USING btree (region, holiday_date);


--
-- Name: ix_leave_ledger_balance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_ledger_balance ON public.leave_ledger USING btree (employee_id, leave_type_id, effective_date);


--
-- Name: ix_leave_ledger_balance_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_ledger_balance_expiry ON public.leave_ledger USING btree (employee_id, leave_type_id, effective_date, expires_on);


--
-- Name: ix_leave_request_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_request_employee ON public.leave_request USING btree (employee_id, start_date);


--
-- Name: ix_leave_request_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_request_open ON public.leave_request USING btree (employee_id, status);


--
-- Name: ix_org_policies_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_policies_lookup ON public.org_policies USING btree (region, leave_type_id, tenure_min_years);


--
-- Name: ix_org_policies_year; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_policies_year ON public.org_policies USING btree (region, policy_year);


--
-- Name: ix_outbox_aggregate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbox_aggregate ON public.outbox USING btree (aggregate_type, aggregate_id);


--
-- Name: ix_outbox_unpublished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbox_unpublished ON public.outbox USING btree (published_at, id);


--
-- Name: ix_substitution_rules_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_substitution_rules_lookup ON public.substitution_rules USING btree (leave_type_id, region, "position");


--
-- Name: leave_ledger trg_leave_ledger_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_leave_ledger_append_only BEFORE DELETE OR UPDATE ON public.leave_ledger FOR EACH ROW EXECUTE FUNCTION public.leave_ledger_append_only();


--
-- Name: approval_delegations approval_delegations_delegate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_delegations
    ADD CONSTRAINT approval_delegations_delegate_id_fkey FOREIGN KEY (delegate_id) REFERENCES public.employee(id) ON DELETE CASCADE;


--
-- Name: approval_delegations approval_delegations_delegator_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_delegations
    ADD CONSTRAINT approval_delegations_delegator_id_fkey FOREIGN KEY (delegator_id) REFERENCES public.employee(id) ON DELETE CASCADE;


--
-- Name: approval_steps approval_steps_acted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT approval_steps_acted_by_fkey FOREIGN KEY (acted_by) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: approval_steps approval_steps_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT approval_steps_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.leave_request(id) ON DELETE CASCADE;


--
-- Name: employee_exceptions employee_exceptions_approved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exceptions
    ADD CONSTRAINT employee_exceptions_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: employee_exceptions employee_exceptions_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_exceptions
    ADD CONSTRAINT employee_exceptions_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id) ON DELETE CASCADE;


--
-- Name: employee employee_manager_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_manager_id_fkey FOREIGN KEY (manager_id) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: approval_steps fk_approval_steps_assigned_approver; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_assigned_approver FOREIGN KEY (assigned_approver_id) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: approval_steps fk_approval_steps_on_behalf_of; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_on_behalf_of FOREIGN KEY (acted_on_behalf_of) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: org_policies fk_org_policies_created_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_policies
    ADD CONSTRAINT fk_org_policies_created_by FOREIGN KEY (created_by_id) REFERENCES public.employee(id) ON DELETE SET NULL;


--
-- Name: org_policies fk_org_policies_supersedes; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_policies
    ADD CONSTRAINT fk_org_policies_supersedes FOREIGN KEY (supersedes_id) REFERENCES public.org_policies(id) ON DELETE SET NULL;


--
-- Name: leave_ledger leave_ledger_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id) ON DELETE CASCADE;


--
-- Name: leave_ledger leave_ledger_policy_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_ledger
    ADD CONSTRAINT leave_ledger_policy_snapshot_id_fkey FOREIGN KEY (policy_snapshot_id) REFERENCES public.org_policies(id) ON DELETE RESTRICT;


--
-- Name: leave_request leave_request_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_request
    ADD CONSTRAINT leave_request_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employee(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 5rvOEv6CTUb9pKCaYrweIeF4exduxcSyxoDLJnuu2C48b7R1gjIJHnHTn6xaNDt

