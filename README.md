# Dynamic PTO & Leave Management System

A leave management system that helps employees and HR teams manage leave requests, leave balances, policies, approvals, and accruals.

The system is designed to handle different leave policies based on region, employee tenure, employment type, and other conditions.

## Features

* Employee leave balance dashboard
* Leave request and submission
* Automatic leave accrual
* Region-based leave policies
* Tenure-based entitlement changes
* Paid and unpaid leave classification
* Leave substitution when the requested leave balance is insufficient
* Multi-level approval workflow
* Manager delegation and authorization
* Employee-specific leave exceptions
* Part-time employee support
* Leave calculation for new joiners and employees leaving the organization
* Carry-over and year-end adjustments
* JWT-based authentication
* Role-based access for employees, managers, HR, and directors
* Audit-friendly leave ledger

## Tech Stack

**Backend**

* Python
* FastAPI
* SQLAlchemy
* PostgreSQL
* Alembic

**Frontend**

* React
* JavaScript
* CSS

**Testing**

* Pytest

## Project Structure

```text
Dynamic-PTO/
│
├── app/
│   ├── policy_engine.py
│   ├── accrual.py
│   ├── dashboard.py
│   ├── classification.py
│   ├── proration.py
│   └── ...
│
├── web/
│   └── React frontend
│
├── alembic/
│   └── Database migrations
│
├── tests/
│   └── Test cases
│
├── requirements.txt
├── seed_users.py
├── seed_demo.py
└── run_api.py
```

## How It Works

### 1. Leave Policies

Leave policies are stored in the database instead of being hardcoded in the application.

Policies can depend on:

* Employee region
* Leave type
* Years of service
* Effective dates
* Employment percentage
* Accrual method

For example, an employee's annual leave entitlement can automatically change when they reach a new tenure level.

### 2. Leave Accrual

The system calculates and adds leave to the employee's leave ledger based on their policy.

It supports:

* Monthly accrual
* Annual leave grants
* Pro-rated leave
* Part-time employees
* Joiners and leavers
* Rounding and year-end adjustments

The accrual process is designed to be idempotent, so running the same job again does not duplicate leave credits.

### 3. Leave Requests

Employees can submit leave requests by selecting the leave type and dates.

The system calculates the number of working days and checks the employee's available leave.

If the requested leave balance is not enough, the system can use the configured substitution order before marking the remaining days as unpaid.

### 4. Approval Workflow

Leave requests can go through multiple approval levels depending on the request.

The system supports:

* Manager approval
* Multi-level approval
* Delegation
* Authorization checks
* Approval SLAs
* Approval history

### 5. Dashboard

Employees can view their current leave balances and leave history.

The dashboard also shows information such as:

* Annual entitlement
* Next expected accrual
* Leave request history
* Pending requests
* Policy information

Balances are calculated from the leave ledger rather than stored as a separate value.

## Database

The project uses PostgreSQL with tables for:

* Employees
* Organization policies
* Employee exceptions
* Leave requests
* Approval steps
* Approval rules
* Leave ledger

The leave ledger stores both accruals and deductions, which makes it possible to track how an employee's balance changed over time.

## Running the Project

### Backend

Install the dependencies:

```bash
pip install -r requirements.txt
```

Create and configure the PostgreSQL database, then set the `DATABASE_URL` in `.env`.

Run the migrations:

```bash
alembic upgrade head
```

Seed the database:

```bash
python -m app.seed
```

Start the API:

```bash
python run_api.py
```

The API will run on:

```text
http://127.0.0.1:8000
```

### Frontend

```bash
cd web
npm install
npm run dev
```

## Testing

The project uses Pytest for testing.

Run:

```bash
pytest
```

The tests cover areas such as:

* Leave policy resolution
* Accrual calculations
* Leave balances
* Pro-ration
* Part-time employees
* Leave requests
* Approval workflows
* Policy changes
* Database validations
* Edge cases

## Example

An employee may start with:

```text
15 days/year
```

After reaching the next tenure level, the policy can automatically change to:

```text
18 days/year
```

No change to the application code is required because the entitlement comes from the policy stored in the database.

## Future Improvements

* Payroll integration
* Holiday calendar support
* Final leave settlement
* Additional HR system integrations
* More configurable leave policies

## Project Status

The core leave management modules are implemented. Integration with payroll and some external systems is still planned.

---

**Built as a project to explore backend systems, database design, policy-based logic, and HR workflow automation.**
