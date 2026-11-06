"""HR — the people who work here, their time, their leave and their pay.

One engine behind every HR screen (frontend HR.jsx), in the same shape as
services/finance: plain functions over the models, each raising ValueError for
something the person must correct and LookupError for something that is not
there, and the router turning those into 400 and 404.

TIME. Every day of every person is at most one HrAttendance row, whoever wrote
it: a supervisor marking the register, a biometric export imported, a device
pushing punches, or the Store's own check-in at the till. A day with no row is
worked out — the person's weekly off, a holiday, or "unmarked", which payroll
counts as present unless HR Settings says otherwise.

PAY. A month's payroll is worked out from those days, the salary structure on
the employee, approved overtime, the sales incentive schemes and any advance
being recovered. Statutory deductions follow the settings (PF on basic + DA up
to the wage ceiling, ESI below the wage limit, Professional Tax by slab, TDS
estimated from the regime's slabs). Approving a run posts a Journal into the
books; paying it posts the Payment — services/finance does the double entry,
so a salary is never keyed into the books by hand.

THE STORE. An employee's `store_username` is their login at the tills. Their
check-ins become attendance, and the bill lines they sold become their sales
incentive and the actuals of their sales KPIs.
"""
import calendar
import csv
import datetime as dt
import hashlib
import io
import math
import re
import secrets

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from . import business_day, businesses, dates, finance, pos_sales, storage

M = models

# ---------------------------------------------------------------------------
#  Fixed lists
# ---------------------------------------------------------------------------
COMPONENTS = [("basic", "Basic"), ("da", "DA"), ("hra", "HRA"), ("conveyance", "Conveyance"),
              ("special", "Special allowance"), ("other", "Other allowance")]
COMPONENT_KEYS = [k for k, _ in COMPONENTS]
EMPLOYMENT_TYPES = ["Permanent", "Probation", "Contract", "Trainee", "Daily Wage"]
EXIT_TYPES = ["Resigned", "Terminated", "Retired", "Contract end", "Absconded"]
#: The employee file's folders, and the kinds of document in each.
DOC_GROUPS = [
    ("ID proofs", ["Photo", "Aadhaar", "PAN", "Voter ID", "Passport", "Driving licence", "Address proof",
                   "Bank proof"]),
    ("Joining", ["Resume", "Offer letter", "Appointment letter", "Joining form", "NDA", "Contract",
                 "Police verification", "Medical"]),
    ("Pay letters", ["Salary revision letter", "Increment letter", "Promotion letter"]),
    ("Education & certificates", ["Education", "Certificate"]),
    ("Previous employment", ["Relieving letter", "Experience letter", "Previous employer payslips",
                             "Previous employer Form 16"]),
    ("Conduct & exit", ["Warning letter", "Resignation letter", "Exit / F&F documents"]),
    ("Other", ["Other"]),
]
DOC_TYPES = [t for _, kinds in DOC_GROUPS for t in kinds]
DOC_GROUP_OF = {t: g for g, kinds in DOC_GROUPS for t in kinds}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
CONTRACT_TYPES = ["Appointment", "Probation", "Fixed term", "Renewal", "Consultant", "Apprentice"]
HISTORY_KINDS = ["Joined", "Confirmed", "Promotion", "Transfer", "Increment", "Re-designation",
                 "Manager change", "Appraisal", "Exit", "Rejoined", "Note"]
WEEKDAYS = list(calendar.day_name)                      # Monday … Sunday
STATUSES = {"P": "Present", "HD": "Half day", "A": "Absent", "L": "Leave", "WO": "Weekly off",
            "H": "Holiday", "OD": "On duty"}
GENDERS = ["Female", "Male", "Other"]
PAY_MODES = ["Bank", "Cash", "Cheque"]
INCENTIVE_BASES = [("pct", "% of sales value"), ("per_piece", "₹ per piece"),
                   ("slab", "% by monthly sales slab")]
GOAL_METRICS = [("manual", "Entered by hand"), ("sales", "Sales value at the Store (₹)"),
                ("bills", "Bills at the Store"), ("attendance", "Attendance %")]

DEFAULTS = {
    "employee_prefix": "EMP",
    # PF — the employee's 12% and the employer's 12%, of which 8.33% (on wages up
    # to the ceiling) goes to the pension scheme.
    "pf_employee_pct": 12.0, "pf_employer_pct": 12.0, "eps_pct": 8.33,
    "pf_wage_ceiling": 15000.0, "pf_cap_at_ceiling": True,
    # ESI — on everyone whose monthly gross is within the limit.
    "esi_employee_pct": 0.75, "esi_employer_pct": 3.25, "esi_wage_limit": 21000.0,
    # Professional Tax — Tamil Nadu's half-yearly slabs as a monthly share. Each
    # row: gross up to `upto` (blank = above every row) pays `amount` a month.
    "pt_state": "Tamil Nadu",
    "pt_slabs": [{"upto": 3500, "amount": 0}, {"upto": 5000, "amount": 30},
                 {"upto": 7500, "amount": 71}, {"upto": 10000, "amount": 155},
                 {"upto": 12500, "amount": 171}, {"upto": None, "amount": 208}],
    # Overtime — paid at this multiple of the hourly rate, worked on gross or basic.
    "ot_multiplier": 2.0, "ot_on": "gross",
    # The divisor a month's pay is split by: calendar days, or a fixed 26 / 30.
    "month_days": "calendar",
    # A day nobody marked: counted present (attendance not kept) or absent.
    "unmarked_days": "present",
    # TDS — the new regime by default (FY 2025-26 slabs, rebate up to ₹12 lakh).
    "std_deduction_new": 75000.0, "std_deduction_old": 50000.0, "cess_pct": 4.0,
    "new_regime_slabs": [[400000, 0], [800000, 5], [1200000, 10], [1600000, 15],
                         [2000000, 20], [2400000, 25], [None, 30]],
    "new_regime_rebate_upto": 1200000.0,
    "old_regime_slabs": [[250000, 0], [500000, 5], [1000000, 20], [None, 30]],
    "old_regime_rebate_upto": 500000.0,
    # The key an attendance device sends with its punches (made on first read).
    "device_key": "",
}

#: The ledgers payroll posts to, made in Finance the first time they are needed.
HR_LEDGERS = {
    "salaries":       ("Salaries & Wages", "Indirect Expenses"),
    "incentive":      ("Staff Incentive & Overtime", "Indirect Expenses"),
    "employer_pf":    ("Employer PF Contribution", "Indirect Expenses"),
    "employer_esi":   ("Employer ESI Contribution", "Indirect Expenses"),
    "salary_payable": ("Salary Payable", "Current Liabilities"),
    "pf_payable":     ("PF Payable", "Current Liabilities"),
    "esi_payable":    ("ESI Payable", "Current Liabilities"),
    "pt_payable":     ("Professional Tax Payable", "Tax & Duties"),
    "staff_advances": ("Staff Advances", "Loans & Advances (Asset)"),
    "recoveries":     ("Salary Deductions Recovered", "Indirect Incomes"),
}


# ---------------------------------------------------------------------------
#  Small helpers
# ---------------------------------------------------------------------------
def r2(v):
    return round(float(v or 0), 2)


def rupee(v):
    """Pay is whole rupees, rounded half up."""
    return float(math.floor(float(v or 0) + 0.5))


def today():
    return business_day.today().isoformat()


def iso(value, required=False, what="date"):
    if value in (None, ""):
        if required:
            raise ValueError(f"Enter the {what}")
        return None
    d = _date(value)
    if d is None:
        raise ValueError(f"“{value}” is not a date")
    return d.isoformat()


def _date(value):
    """A date from ISO or the day-first forms people type. Not services/dates:
    that one refuses years before 1990, which is right for an invoice and wrong
    for a date of birth."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        return d if 1900 <= d.year <= 2100 else None
    return dates.parse(value)


def day(s):
    return dt.date.fromisoformat(str(s)[:10]) if s else None


def month_of(value=None):
    """'YYYY-MM' for a month string, a date, or today."""
    if not value:
        return today()[:7]
    s = str(value)
    if re.match(r"^\d{4}-\d{2}$", s):
        return s
    return iso(s)[:7]


def month_days(month):
    y, m = int(month[:4]), int(month[5:7])
    n = calendar.monthrange(y, m)[1]
    return [dt.date(y, m, d) for d in range(1, n + 1)]


def month_label(month):
    y, m = int(month[:4]), int(month[5:7])
    return f"{calendar.month_name[m]} {y}"


def _minutes(hhmm):
    if not hhmm:
        return None
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", str(hhmm))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _hhmm(mins):
    if mins is None:
        return None
    mins = int(mins) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _user(user):
    return (user or {}).get("username") or (user or {}).get("full_name") or ""


def _next_no(db, model, field, prefix, width=5):
    col = getattr(model, field)
    n = (db.query(func.count(model.id)).scalar() or 0) + 1
    while True:
        no = f"{prefix}{n:0{width}d}"
        if not db.query(model.id).filter(col == no).first():
            return no
        n += 1


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------
def settings(db: Session) -> dict:
    out = dict(DEFAULTS)
    for row in db.query(M.HrSetting).all():
        out[row.key] = row.value
    if not out.get("device_key"):
        out["device_key"] = secrets.token_urlsafe(24)
        _put_setting(db, "device_key", out["device_key"])
    return out


def _put_setting(db, key, value):
    row = db.get(M.HrSetting, key)
    if row is None:
        db.add(M.HrSetting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def save_settings(db, data: dict):
    for k, v in (data or {}).items():
        if k not in DEFAULTS or k == "device_key":
            continue
        if isinstance(DEFAULTS[k], float) and v not in (None, ""):
            v = float(v)
        _put_setting(db, k, v)
    return settings(db)


def new_device_key(db):
    key = secrets.token_urlsafe(24)
    _put_setting(db, "device_key", key)
    return key


# ---------------------------------------------------------------------------
#  Masters: departments, designations, shifts, holidays, leave types
# ---------------------------------------------------------------------------
def ensure_defaults(db: Session):
    """A first-run set to start from — every row editable, none required."""
    if not db.query(M.HrDepartment.id).first():
        for n, c in (("Sales", "SAL"), ("Billing", "BIL"), ("Warehouse", "WH"), ("Accounts", "ACC"),
                     ("Alteration", "ALT"), ("Administration", "ADM")):
            db.add(M.HrDepartment(name=n, code=c))
        db.flush()
        dep = {d.name: d.id for d in db.query(M.HrDepartment).all()}
        for n, d, g, lv in (("Store Manager", "Sales", "M1", 2), ("Floor Supervisor", "Sales", "S1", 3),
                            ("Sales Executive", "Sales", "E1", 4), ("Cashier", "Billing", "E1", 4),
                            ("Warehouse Supervisor", "Warehouse", "S1", 3), ("Loader", "Warehouse", "E2", 5),
                            ("Accountant", "Accounts", "E1", 4), ("Tailor", "Alteration", "E1", 4),
                            ("General Manager", "Administration", "M2", 1)):
            db.add(M.HrDesignation(name=n, department_id=dep[d], grade=g, level=lv))
    if not db.query(M.HrShift.id).first():
        db.add(M.HrShift(name="General", code="GEN", start_time="10:00", end_time="21:00",
                         break_minutes=60, full_day_hours=10, half_day_hours=5))
        db.add(M.HrShift(name="Morning", code="MOR", start_time="09:00", end_time="17:00",
                         break_minutes=30, full_day_hours=7.5, half_day_hours=4, color="#1B5E96"))
        db.add(M.HrShift(name="Evening", code="EVE", start_time="13:00", end_time="21:30",
                         break_minutes=30, full_day_hours=8, half_day_hours=4, color="#B45309"))
    if not db.query(M.HrLeaveType.id).first():
        for code, name, q, acc, cf, paid, neg, col in (
                ("CL", "Casual Leave", 12, "monthly", 0, True, False, "#2F8F68"),
                ("SL", "Sick Leave", 12, "yearly", 6, True, False, "#1B5E96"),
                ("EL", "Earned Leave", 15, "monthly", 30, True, False, "#7C3AED"),
                ("CO", "Compensatory Off", 0, "none", 0, True, False, "#B45309"),
                ("LOP", "Loss of Pay", 0, "none", 0, False, True, "#B91C1C")):
            db.add(M.HrLeaveType(code=code, name=name, annual_quota=q, accrual=acc,
                                 carry_forward_max=cf, paid=paid, allow_negative=neg, color=col))
    db.flush()


def _master_out(x):
    d = {c.name: getattr(x, c.name) for c in x.__table__.columns}
    return d


MASTERS = {"departments": M.HrDepartment, "designations": M.HrDesignation, "shifts": M.HrShift,
           "holidays": M.HrHoliday, "leave_types": M.HrLeaveType}


def list_master(db, kind):
    model = MASTERS[kind]
    q = db.query(model)
    if kind == "holidays":
        rows = q.order_by(model.date).all()
    else:
        rows = q.order_by(model.id).all()
    out = [_master_out(r) for r in rows]
    if kind == "departments":
        counts = dict(db.query(M.Employee.department_id, func.count(M.Employee.id))
                        .filter(M.Employee.status != "exited").group_by(M.Employee.department_id).all())
        heads = {e.id: e.name for e in db.query(M.Employee).filter(
            M.Employee.id.in_([r["head_id"] for r in out if r["head_id"]] or [0])).all()}
        for r in out:
            r["employees"] = counts.get(r["id"], 0)
            r["head"] = heads.get(r["head_id"])
    if kind == "designations":
        deps = {d.id: d.name for d in db.query(M.HrDepartment).all()}
        counts = dict(db.query(M.Employee.designation_id, func.count(M.Employee.id))
                        .filter(M.Employee.status != "exited").group_by(M.Employee.designation_id).all())
        for r in out:
            r["department"] = deps.get(r["department_id"])
            r["employees"] = counts.get(r["id"], 0)
    if kind == "shifts":
        counts = dict(db.query(M.Employee.shift_id, func.count(M.Employee.id))
                        .filter(M.Employee.status != "exited").group_by(M.Employee.shift_id).all())
        for r in out:
            r["employees"] = counts.get(r["id"], 0)
    return out


def save_master(db, kind, data, mid=None):
    model = MASTERS[kind]
    x = db.get(model, mid) if mid else model()
    if mid and x is None:
        raise LookupError("not found")
    cols = {c.name: c for c in model.__table__.columns if c.name != "id"}
    for k, v in (data or {}).items():
        if k not in cols:
            continue
        if v == "":
            v = None
        if k in ("date",) and v:
            v = iso(v)
        if k in ("start_time", "end_time") and v and _minutes(v) is None:
            raise ValueError(f"Enter {k.replace('_', ' ')} as HH:MM")
        setattr(x, k, v)
    if not getattr(x, "name", None):
        raise ValueError("Enter a name")
    if kind == "holidays" and not x.date:
        raise ValueError("Enter the holiday's date")
    if kind == "leave_types" and not x.code:
        raise ValueError("Enter a short code, like CL")
    if kind == "departments":
        clash = db.query(M.HrDepartment).filter(func.lower(M.HrDepartment.name) == x.name.strip().lower(),
                                                M.HrDepartment.id != (mid or 0)).first()
        if clash:
            raise ValueError(f"There is already a department called {clash.name}")
    if kind == "designations":
        same = db.query(M.HrDesignation).filter(func.lower(M.HrDesignation.name) == x.name.strip().lower(),
                                                M.HrDesignation.id != (mid or 0))
        same = (same.filter(M.HrDesignation.department_id == x.department_id) if x.department_id
                else same.filter(M.HrDesignation.department_id.is_(None)))
        clash = same.first()
        if clash:
            dep = db.get(M.HrDepartment, x.department_id) if x.department_id else None
            raise ValueError(f"There is already a designation called {clash.name}" + (f" in {dep.name}" if dep else ""))
    if kind == "shifts":
        clash = db.query(M.HrShift).filter(func.lower(M.HrShift.name) == x.name.strip().lower(),
                                           M.HrShift.id != (mid or 0)).first()
        if clash:
            raise ValueError(f"There is already a shift called {clash.name}")
    x.name = x.name.strip()
    if not mid:
        db.add(x)
    db.flush()
    return _master_out(x)


def delete_master(db, kind, mid):
    model = MASTERS[kind]
    x = db.get(model, mid)
    if x is None:
        raise LookupError("not found")
    used = {
        "departments": lambda: db.query(M.Employee.id).filter(M.Employee.department_id == mid).first(),
        "designations": lambda: db.query(M.Employee.id).filter(M.Employee.designation_id == mid).first(),
        "shifts": lambda: db.query(M.Employee.id).filter(M.Employee.shift_id == mid).first()
        or db.query(M.HrRoster.id).filter(M.HrRoster.shift_id == mid).first(),
        "leave_types": lambda: db.query(M.HrLeaveLedger.id).filter(M.HrLeaveLedger.leave_type_id == mid).first()
        or db.query(M.HrLeaveRequest.id).filter(M.HrLeaveRequest.leave_type_id == mid).first(),
        "holidays": lambda: None,
    }[kind]()
    if used:
        if hasattr(x, "active"):
            x.active = False
            return {"deactivated": True}
        raise ValueError("It is in use")
    db.delete(x)
    return {"deleted": True}


# ---------------------------------------------------------------------------
#  Employees
# ---------------------------------------------------------------------------
EMP_FIELDS = ["name", "father_name", "gender", "dob", "marital_status", "anniversary", "blood_group",
              "phone", "alt_phone", "email", "address", "permanent_address", "emergency_name",
              "emergency_phone", "aadhaar", "pan", "uan", "pf_no", "esi_no", "bank_name",
              "bank_account", "ifsc", "department_id", "designation_id", "grade", "location",
              "reporting_to_id", "employment_type", "joined_on", "confirmed_on", "probation_months",
              "shift_id", "weekly_off", "pf_applicable", "esi_applicable", "pt_applicable",
              "tax_regime", "tds_monthly", "payment_mode", "store_username"]
DATE_FIELDS = {"dob", "anniversary", "joined_on", "confirmed_on", "resigned_on", "last_working_day"}
INT_FIELDS = {"department_id", "designation_id", "reporting_to_id", "shift_id", "probation_months"}


def gross_of(salary):
    return r2(sum(float((salary or {}).get(k) or 0) for k in COMPONENT_KEYS))


def emp_brief(e):
    return {"id": e.id, "code": e.code, "name": e.name,
            "designation": e.designation.name if e.designation else None,
            "department": e.department.name if e.department else None,
            "location": e.location, "status": e.status}


def emp_out(e, full=False):
    d = {c.name: getattr(e, c.name) for c in e.__table__.columns}
    d.pop("created_at", None)
    d.pop("updated_at", None)
    d["salary"] = {k: float((e.salary or {}).get(k) or 0) for k in COMPONENT_KEYS}
    d["gross"] = gross_of(e.salary)
    d["department"] = e.department.name if e.department else None
    d["designation"] = e.designation.name if e.designation else None
    d["shift"] = e.shift.name if e.shift else None
    d["manager"] = e.manager.name if e.manager else None
    d["photo"] = photo_version(e)
    if full:
        d["service"] = service_length(e)
    return d


def service_length(e, on=None):
    j = day(e.joined_on)
    if not j:
        return ""
    end = day(e.last_working_day) if e.status == "exited" and e.last_working_day else (on or business_day.today())
    months = (end.year - j.year) * 12 + end.month - j.month - (1 if end.day < j.day else 0)
    months = max(months, 0)
    y, m = divmod(months, 12)
    return (f"{y} yr " if y else "") + f"{m} mo"


def list_employees(db, q=None, department_id=None, designation_id=None, location=None, status=None,
                   employment_type=None):
    qry = db.query(M.Employee)
    if status == "current":
        qry = qry.filter(M.Employee.status != "exited")
    elif status:
        qry = qry.filter(M.Employee.status == status)
    if department_id:
        qry = qry.filter(M.Employee.department_id == int(department_id))
    if designation_id:
        qry = qry.filter(M.Employee.designation_id == int(designation_id))
    if location:
        qry = qry.filter(M.Employee.location == location)
    if employment_type:
        qry = qry.filter(M.Employee.employment_type == employment_type)
    if q:
        like = f"%{q.strip().lower()}%"
        qry = qry.filter(func.lower(M.Employee.name).like(like) | func.lower(M.Employee.code).like(like)
                         | M.Employee.phone.like(like) | func.lower(M.Employee.store_username).like(like))
    return [emp_out(e) for e in qry.order_by(M.Employee.code).all()]


def get_employee(db, eid):
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    d = emp_out(e, full=True)
    d["contracts"] = [_master_out(c) for c in db.query(M.EmployeeContract)
                      .filter(M.EmployeeContract.employee_id == eid).order_by(M.EmployeeContract.start_date.desc()).all()]
    d["documents"] = [doc_out(x) for x in db.query(M.EmployeeDocument)
                      .filter(M.EmployeeDocument.employee_id == eid).order_by(M.EmployeeDocument.id.desc()).all()]
    d["history"] = [_master_out(h) for h in db.query(M.EmployeeHistory)
                    .filter(M.EmployeeHistory.employee_id == eid)
                    .order_by(M.EmployeeHistory.effective_on.desc(), M.EmployeeHistory.id.desc()).all()]
    d["reports"] = [emp_brief(x) for x in db.query(M.Employee)
                    .filter(M.Employee.reporting_to_id == eid, M.Employee.status != "exited").all()]
    d["leave"] = leave_balances(db, eid)
    d["advances"] = [advance_out(db, a) for a in db.query(M.HrAdvance)
                     .filter(M.HrAdvance.employee_id == eid).order_by(M.HrAdvance.id.desc()).all()]
    return d


def _label(db, model, xid):
    x = db.get(model, xid) if xid else None
    return x.name if x else None


def save_employee(db, data: dict, user=None, eid=None):
    data = dict(data or {})
    e = db.get(M.Employee, eid) if eid else None
    if eid and e is None:
        raise LookupError("employee not found")
    new = e is None
    before = None if new else {"department": e.department.name if e.department else None,
                               "designation": e.designation.name if e.designation else None,
                               "location": e.location, "gross": gross_of(e.salary),
                               "manager": e.manager.name if e.manager else None,
                               "type": e.employment_type, "confirmed_on": e.confirmed_on}
    if new:
        e = M.Employee(status="active")
    for k in EMP_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, str):
            v = v.strip()
        if v == "":
            v = None
        if k in DATE_FIELDS and v:
            v = iso(v)
        if k in INT_FIELDS and v is not None:
            v = int(v)
        if k == "tds_monthly" and v is not None:
            v = float(v)
        if k in ("pan", "ifsc") and v:
            v = v.upper()
        setattr(e, k, v)
    if "salary" in data:
        e.salary = {k: r2((data.get("salary") or {}).get(k)) for k in COMPONENT_KEYS}
    if not (e.name or "").strip():
        raise ValueError("Enter the employee's name")
    if not e.joined_on:
        raise ValueError("Enter the date they joined")
    if e.phone and not re.match(r"^\+?\d{10,13}$", e.phone):
        raise ValueError("Enter the mobile number as 10 digits")
    if e.pan and not re.match(r"^[A-Z]{5}\d{4}[A-Z]$", e.pan):
        raise ValueError("A PAN is five letters, four digits and a letter")
    if e.ifsc and not re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", e.ifsc):
        raise ValueError("An IFSC is four letters, a zero and six characters")
    if e.weekly_off and e.weekly_off not in WEEKDAYS:
        raise ValueError("Pick the weekly off from the days of the week")
    # a manager may not report — however far up — to someone who reports to them
    if e.reporting_to_id:
        seen, cur = {e.id}, db.get(M.Employee, e.reporting_to_id)
        while cur is not None:
            if cur.id in seen:
                raise ValueError("That would make them report to someone who reports to them")
            seen.add(cur.id)
            cur = cur.manager
    if e.store_username:
        clash = db.query(M.Employee).filter(func.lower(M.Employee.store_username) == e.store_username.lower(),
                                            M.Employee.id != (e.id or 0)).first()
        if clash:
            raise ValueError(f"{clash.name} is already linked to the Store login “{e.store_username}”")
    code = (data.get("code") or "").strip().upper()
    if code and code != e.code:
        if db.query(M.Employee.id).filter(M.Employee.code == code, M.Employee.id != (e.id or 0)).first():
            raise ValueError(f"Employee code {code} is taken")
        e.code = code
    if not e.code:
        e.code = _next_no(db, M.Employee, "code", settings(db)["employee_prefix"], 4)
    if new:
        db.add(e)
    db.flush()
    db.refresh(e)
    who = _user(user)
    after = {"department": e.department.name if e.department else None,
             "designation": e.designation.name if e.designation else None,
             "location": e.location, "gross": gross_of(e.salary),
             "manager": e.manager.name if e.manager else None,
             "type": e.employment_type, "confirmed_on": e.confirmed_on}
    effective = iso(data.get("effective_on")) or today()

    def hist(kind, detail, when=effective):
        db.add(M.EmployeeHistory(employee_id=e.id, effective_on=when, kind=kind, detail=detail,
                                 department=after["department"], designation=after["designation"],
                                 location=after["location"], gross=after["gross"], created_by=who))
    if new:
        hist("Joined", f"Joined as {after['designation'] or 'staff'}"
             + (f", {after['department']}" if after["department"] else "")
             + (f" at {after['location']}" if after["location"] else ""), e.joined_on)
    else:
        if before["designation"] != after["designation"]:
            hist("Re-designation", f"{before['designation'] or '—'} → {after['designation'] or '—'}")
        if before["department"] != after["department"]:
            hist("Transfer", f"Department {before['department'] or '—'} → {after['department'] or '—'}")
        if before["location"] != after["location"]:
            hist("Transfer", f"{before['location'] or '—'} → {after['location'] or '—'}")
        if r2(before["gross"]) != r2(after["gross"]):
            change = after["gross"] - before["gross"]
            pct = f" ({change / before['gross'] * 100:+.1f}%)" if before["gross"] else ""
            hist("Increment" if change > 0 else "Note",
                 f"Monthly gross ₹{before['gross']:,.0f} → ₹{after['gross']:,.0f}{pct}")
        if before["manager"] != after["manager"]:
            hist("Manager change", f"Reports to {after['manager'] or 'nobody'} (was {before['manager'] or 'nobody'})")
        if after["confirmed_on"] and not before["confirmed_on"]:
            hist("Confirmed", "Confirmed in service", after["confirmed_on"])
    db.flush()
    return e


def exit_employee(db, eid, data, user=None):
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    lwd = iso(data.get("last_working_day"), True, "last working day")
    e.resigned_on = iso(data.get("resigned_on")) or e.resigned_on or today()
    e.last_working_day = lwd
    e.exit_type = data.get("exit_type") or "Resigned"
    e.exit_reason = data.get("exit_reason")
    e.fnf_status = data.get("fnf_status") or "pending"
    e.fnf_amount = float(data["fnf_amount"]) if data.get("fnf_amount") not in (None, "") else e.fnf_amount
    e.status = "exited" if lwd <= today() else "notice"
    db.add(M.EmployeeHistory(employee_id=e.id, effective_on=lwd, kind="Exit",
                             detail=f"{e.exit_type}" + (f" — {e.exit_reason}" if e.exit_reason else ""),
                             department=e.department.name if e.department else None,
                             designation=e.designation.name if e.designation else None,
                             location=e.location, gross=gross_of(e.salary), created_by=_user(user)))
    # anyone who reported to them now reports to their manager
    for r in db.query(M.Employee).filter(M.Employee.reporting_to_id == e.id).all():
        r.reporting_to_id = e.reporting_to_id
    db.flush()
    return e


def rejoin_employee(db, eid, data, user=None):
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    if e.status == "active":
        raise ValueError(f"{e.name} has not left")
    when = iso(data.get("joined_on")) or today()
    e.status, e.resigned_on, e.last_working_day, e.exit_type, e.exit_reason = "active", None, None, None, None
    db.add(M.EmployeeHistory(employee_id=e.id, effective_on=when, kind="Rejoined",
                             detail="Rejoined", location=e.location, gross=gross_of(e.salary),
                             department=e.department.name if e.department else None,
                             designation=e.designation.name if e.designation else None,
                             created_by=_user(user)))
    db.flush()
    return e


def settle_exits(db):
    """Notice periods that have run out become exits."""
    n = 0
    for e in db.query(M.Employee).filter(M.Employee.status == "notice",
                                         M.Employee.last_working_day < today()).all():
        e.status = "exited"
        n += 1
    return n


def add_history(db, eid, data, user=None):
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    h = M.EmployeeHistory(employee_id=eid, effective_on=iso(data.get("effective_on")) or today(),
                          kind=data.get("kind") or "Note", detail=data.get("detail"),
                          department=e.department.name if e.department else None,
                          designation=e.designation.name if e.designation else None,
                          location=e.location, gross=gross_of(e.salary), created_by=_user(user))
    db.add(h)
    db.flush()
    return _master_out(h)


# ---- contracts and documents -------------------------------------------------
def save_contract(db, eid, data, cid=None):
    if db.get(M.Employee, eid) is None:
        raise LookupError("employee not found")
    c = db.get(M.EmployeeContract, cid) if cid else M.EmployeeContract(employee_id=eid)
    if cid and (c is None or c.employee_id != eid):
        raise LookupError("contract not found")
    c.contract_type = data.get("contract_type") or "Appointment"
    c.start_date = iso(data.get("start_date"), True, "start date")
    c.end_date = iso(data.get("end_date"))
    if c.end_date and c.end_date < c.start_date:
        raise ValueError("The contract ends before it starts")
    c.ctc_annual = float(data["ctc_annual"]) if data.get("ctc_annual") not in (None, "") else None
    c.notice_days = int(data["notice_days"]) if data.get("notice_days") not in (None, "") else 30
    c.terms = data.get("terms")
    c.document_id = int(data["document_id"]) if data.get("document_id") else None
    c.status = data.get("status") or "active"
    if not cid:
        db.add(c)
    db.flush()
    return _master_out(c)


def delete_contract(db, eid, cid):
    c = db.get(M.EmployeeContract, cid)
    if c is None or c.employee_id != eid:
        raise LookupError("contract not found")
    db.delete(c)


def doc_out(x):
    d = _master_out(x)
    d.pop("file_ref", None)
    d["has_file"] = bool(x.file_ref)
    d["group"] = DOC_GROUP_OF.get(x.doc_type, "Other")
    d["is_image"] = bool(x.file_ref and x.file_ref.lower().endswith(IMAGE_EXTS))
    d["expired"] = bool(x.expires_on and x.expires_on < today())
    d["expiring"] = bool(x.expires_on and today() <= x.expires_on <= (business_day.today() + dt.timedelta(days=30)).isoformat())
    return d


def save_document(db, eid, data, raw=None, filename=None, user=None, did=None):
    if db.get(M.Employee, eid) is None:
        raise LookupError("employee not found")
    x = db.get(M.EmployeeDocument, did) if did else M.EmployeeDocument(employee_id=eid)
    if did and (x is None or x.employee_id != eid):
        raise LookupError("document not found")
    for k in ("doc_type", "title", "number"):
        if k in data:
            setattr(x, k, (data.get(k) or "").strip() or None)
    for k in ("issued_on", "expires_on"):
        if k in data:
            setattr(x, k, iso(data.get(k)))
    if raw:
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("A document can be at most 10 MB")
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if filename and "." in filename else ""
        if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx", ""):
            raise ValueError("Attach a PDF, an image or a Word file")
        if (data.get("doc_type") or x.doc_type) == "Photo" and ext not in IMAGE_EXTS:
            raise ValueError("A photo must be a JPG, PNG or WebP picture")
        h = hashlib.sha256(raw).hexdigest()
        x.file_ref = storage.save(raw, f"hr{eid}_{h[:16]}{ext}")
        x.filename = filename
        x.mime = storage.mime_for(x.file_ref)
    if not x.doc_type:
        raise ValueError("Choose what kind of document it is")
    x.uploaded_by = _user(user)
    if not did:
        db.add(x)
    db.flush()
    if x.doc_type == "Photo" and x.file_ref:
        db.get(M.Employee, eid).photo_ref = x.file_ref
    return doc_out(x)


def document_file(db, eid, did):
    x = db.get(M.EmployeeDocument, did)
    if x is None or x.employee_id != eid or not x.file_ref:
        raise LookupError("no file")
    raw = storage.read(x.file_ref)
    if raw is None:
        raise LookupError("the file is missing from storage")
    return raw, x.mime or storage.mime_for(x.file_ref), x.filename or f"document-{did}"


def photo_version(e):
    """Changes whenever the photo does, so a browser never shows the old one."""
    return hashlib.sha1(e.photo_ref.encode()).hexdigest()[:10] if e.photo_ref else None


def remove_photo(db, eid):
    """Takes the picture off the record; the Photo document itself stays on file."""
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    e.photo_ref = None
    db.flush()


def photo_file(db, eid):
    e = db.get(M.Employee, eid)
    if e is None or not e.photo_ref:
        raise LookupError("no photo")
    raw = storage.read(e.photo_ref)
    if raw is None:
        raise LookupError("no photo")
    return raw, storage.mime_for(e.photo_ref)


def delete_document(db, eid, did):
    x = db.get(M.EmployeeDocument, did)
    if x is None or x.employee_id != eid:
        raise LookupError("document not found")
    e = db.get(M.Employee, eid)
    if e.photo_ref and e.photo_ref == x.file_ref:
        e.photo_ref = None
    db.delete(x)


# ---- the Store's staff ------------------------------------------------------------
def store_staff(db):
    """The Store's logins, and which employee each is linked to."""
    if not pos_sales.available():
        return {"available": False, "rows": []}
    linked = {(e.store_username or "").lower(): e for e in db.query(M.Employee)
              .filter(M.Employee.store_username.isnot(None)).all()}
    rows = []
    for uid, username, name, phone, role, salary, active in pos_sales._rows(
            f"SELECT id, username, full_name, phone, role, salary, active FROM {pos_sales.q('users')} "
            "ORDER BY full_name", []):
        e = linked.get((username or "").lower())
        rows.append({"store_id": uid, "username": username, "name": name or username, "phone": phone,
                     "role": role, "salary": r2(salary), "active": bool(active) if active is not None else True,
                     "employee_id": e.id if e else None, "employee": e.name if e else None})
    return {"available": True, "rows": rows}


def import_store_staff(db, usernames, user=None):
    """Make an employee of each Store login named, linked to it."""
    staff = {r["username"]: r for r in store_staff(db)["rows"]}
    made = []
    desig = {d.name: d for d in db.query(M.HrDesignation).all()}
    gen = db.query(M.HrShift).filter(M.HrShift.active.isnot(False)).order_by(M.HrShift.id).first()
    for u in usernames or []:
        r = staff.get(u)
        if r is None or r["employee_id"]:
            continue
        d = desig.get("Cashier" if r["role"] == "cashier" else "Store Manager" if r["role"] in ("admin", "manager")
                      else "Sales Executive")
        phone = re.sub(r"\D", "", r["phone"] or "")[-10:] or None
        e = save_employee(db, {"name": r["name"], "phone": phone if phone and len(phone) == 10 else None,
                               "joined_on": today(), "store_username": r["username"],
                               "designation_id": d.id if d else None,
                               "department_id": d.department_id if d else None,
                               "shift_id": gen.id if gen else None,
                               "salary": {"basic": r["salary"] or 0}}, user)
        made.append(emp_brief(e))
    return made


# ---------------------------------------------------------------------------
#  The organisation chart
# ---------------------------------------------------------------------------
def org_chart(db, department_id=None, location=None):
    q = db.query(M.Employee).filter(M.Employee.status != "exited")
    rows = q.all()
    keep = rows
    if department_id or location:
        keep = [e for e in rows if (not department_id or e.department_id == int(department_id))
                and (not location or e.location == location)]
    ids = {e.id for e in keep}
    reports = {}
    for e in rows:
        if e.reporting_to_id:
            reports[e.reporting_to_id] = reports.get(e.reporting_to_id, 0) + 1
    nodes = [{"id": e.id, "code": e.code, "name": e.name, "designation": e.designation.name if e.designation else None,
              "level": e.designation.level if e.designation else 9,
              "department": e.department.name if e.department else None, "location": e.location,
              "manager_id": e.reporting_to_id if e.reporting_to_id in ids else None,
              "reports": reports.get(e.id, 0), "photo": photo_version(e), "status": e.status}
             for e in keep]
    deps = []
    for d in db.query(M.HrDepartment).filter(M.HrDepartment.active.isnot(False)).all():
        members = [n for n in nodes if n["department"] == d.name]
        head = next((n for n in nodes if n["id"] == d.head_id), None)
        deps.append({"id": d.id, "name": d.name, "head": head["name"] if head else None,
                     "count": len(members)})
    no_manager = sum(1 for n in nodes if not n["manager_id"])
    return {"nodes": nodes, "departments": deps, "roots": no_manager}


# ---------------------------------------------------------------------------
#  Time: shifts, roster, attendance
# ---------------------------------------------------------------------------
def _holidays(db, d0, d1):
    return {h.date: h for h in db.query(M.HrHoliday).filter(M.HrHoliday.date >= d0, M.HrHoliday.date <= d1,
                                                           M.HrHoliday.optional.isnot(True)).all()}


def _roster(db, ids, d0, d1):
    out = {}
    if not ids:
        return out
    for r in db.query(M.HrRoster).filter(M.HrRoster.employee_id.in_(ids), M.HrRoster.date >= d0,
                                         M.HrRoster.date <= d1).all():
        out[(r.employee_id, r.date)] = r
    return out


def _employed_on(e, d):
    s = d.isoformat()
    if e.joined_on and s < e.joined_on:
        return False
    if e.last_working_day and e.status in ("exited", "notice") and s > e.last_working_day:
        return False
    return True


def score_day(in_time, out_time, shift):
    """(worked, late, ot) minutes for one day's first-in and last-out."""
    i, o = _minutes(in_time), _minutes(out_time)
    if i is None:
        return 0, 0, 0
    start = _minutes(shift.start_time) if shift else None
    late = max(0, i - start) if start is not None else 0
    grace = (shift.grace_minutes or 0) if shift else 0
    late = late if late > grace else 0
    if o is None:
        return 0, late, 0
    if o < i:                                     # past midnight
        o += 24 * 60
    span = o - i
    brk = (shift.break_minutes or 0) if shift else 0
    worked = span - brk if shift and span > (shift.half_day_hours or 0) * 60 else span
    worked = max(worked, 0)
    full = (shift.full_day_hours or 9) * 60 if shift else 9 * 60
    over = worked - full
    after = (shift.ot_after_minutes or 0) if shift else 30
    ot = int(over) if over >= after else 0
    return int(worked), int(late), ot


def status_for(worked, shift, has_out):
    if not has_out:
        return "P"
    full = (shift.full_day_hours if shift else 9) * 60
    half = (shift.half_day_hours if shift else 4.5) * 60
    if worked >= full - 60:
        return "P"
    if worked >= half:
        return "HD"
    return "HD" if worked > 0 else "A"


def _shift_for(db, e, date_s, roster=None):
    r = (roster or {}).get((e.id, date_s))
    if r is not None and r.shift_id:
        return db.get(M.HrShift, r.shift_id)
    return e.shift


def mark(db, eid, date_s, data, user=None, source="manual"):
    """Write one person's day. A day the leave screen wrote is not overwritten by
    a punch — cancel the leave first."""
    e = db.get(M.Employee, eid)
    if e is None:
        raise LookupError("employee not found")
    date_s = iso(date_s, True)
    a = db.query(M.HrAttendance).filter(M.HrAttendance.employee_id == eid, M.HrAttendance.date == date_s).first()
    if a is not None and a.source == "leave" and source in ("device", "import", "store"):
        return a
    if a is None:
        a = M.HrAttendance(employee_id=eid, date=date_s)
        db.add(a)
    if "in_time" in data:
        a.in_time = _hhmm(_minutes(data.get("in_time"))) if data.get("in_time") else None
    if "out_time" in data:
        a.out_time = _hhmm(_minutes(data.get("out_time"))) if data.get("out_time") else None
    shift = _shift_for(db, e, date_s, _roster(db, [eid], date_s, date_s))
    worked, late, ot = score_day(a.in_time, a.out_time, shift)
    a.worked_minutes, a.late_minutes = worked, late
    if ot != (a.ot_minutes or 0):
        a.ot_minutes = ot
        a.ot_status = "pending" if ot else None
        a.ot_approved_minutes = 0
    st = data.get("status")
    if st:
        if st not in STATUSES:
            raise ValueError(f"Unknown attendance status {st}")
        a.status = st
    elif a.in_time:
        a.status = status_for(worked, shift, bool(a.out_time))
    if a.status != "L" and source != "leave" and a.source == "leave":
        a.leave_type_id = None
    if "leave_type_id" in data:
        a.leave_type_id = data.get("leave_type_id")
    if "remarks" in data:
        a.remarks = data.get("remarks")
    a.source = source
    a.updated_by = _user(user)
    db.flush()
    return a


def clear_day(db, eid, date_s):
    a = db.query(M.HrAttendance).filter(M.HrAttendance.employee_id == eid, M.HrAttendance.date == iso(date_s)).first()
    if a is not None:
        if a.source == "leave":
            raise ValueError("This day is approved leave — cancel the leave instead")
        db.delete(a)


def _active_in(db, d0, d1, department_id=None, location=None, ids=None):
    q = db.query(M.Employee).filter(M.Employee.joined_on <= d1)
    q = q.filter((M.Employee.status != "exited") | (M.Employee.last_working_day >= d0))
    if department_id:
        q = q.filter(M.Employee.department_id == int(department_id))
    if location:
        q = q.filter(M.Employee.location == location)
    if ids:
        q = q.filter(M.Employee.id.in_(ids))
    return q.order_by(M.Employee.code).all()


def day_view(db, e, d, rec, hol, roster):
    """What a day is for one person: their record, or what it would be without one."""
    s = d.isoformat()
    if not _employed_on(e, d):
        return {"s": "", "out": True}
    if rec is not None:
        return {"s": rec.status, "in": rec.in_time, "out_t": rec.out_time, "late": rec.late_minutes or 0,
                "ot": rec.ot_minutes or 0, "ot_ok": rec.ot_approved_minutes or 0, "src": rec.source,
                "lt": rec.leave_type_id, "rm": rec.remarks}
    r = roster.get((e.id, s))
    if (r is not None and r.off) or (r is None and e.weekly_off and WEEKDAYS[d.weekday()] == e.weekly_off):
        return {"s": "WO", "derived": True}
    if s in hol:
        return {"s": "H", "derived": True, "rm": hol[s].name}
    return {"s": "", "derived": True}


def counts(db, e, days, recs, hol, roster, leave_paid, st):
    """The month's tally for one person, as payroll pays it."""
    c = {"present": 0.0, "paid_leave": 0.0, "lop": 0.0, "absent": 0.0, "wo": 0.0, "holiday": 0.0,
         "unmarked": 0, "payable": 0, "late_days": 0, "ot_minutes": 0, "ot_approved": 0}
    for d in days:
        if not _employed_on(e, d):
            continue
        c["payable"] += 1
        v = day_view(db, e, d, recs.get(d.isoformat()), hol, roster)
        s = v["s"]
        if v.get("late"):
            c["late_days"] += 1
        c["ot_minutes"] += v.get("ot", 0) or 0
        c["ot_approved"] += v.get("ot_ok", 0) or 0
        if s in ("P", "OD"):
            c["present"] += 1
        elif s == "HD":
            c["present"] += 0.5
            if v.get("lt"):
                if leave_paid.get(v["lt"], True):
                    c["paid_leave"] += 0.5
                else:
                    c["lop"] += 0.5
            else:
                c["lop"] += 0.5
        elif s == "L":
            if leave_paid.get(v.get("lt"), True):
                c["paid_leave"] += 1
            else:
                c["lop"] += 1
        elif s == "A":
            c["absent"] += 1
            c["lop"] += 1
        elif s == "WO":
            c["wo"] += 1
        elif s == "H":
            c["holiday"] += 1
        else:
            c["unmarked"] += 1
            if st.get("unmarked_days") == "absent":
                c["lop"] += 1
            else:
                c["present"] += 1
    c["paid_days"] = c["payable"] - c["lop"]
    return c


def month_register(db, month, department_id=None, location=None):
    month = month_of(month)
    days = month_days(month)
    d0, d1 = days[0].isoformat(), days[-1].isoformat()
    emps = _active_in(db, d0, d1, department_id, location)
    ids = [e.id for e in emps]
    recs = {}
    for a in db.query(M.HrAttendance).filter(M.HrAttendance.employee_id.in_(ids or [0]),
                                             M.HrAttendance.date >= d0, M.HrAttendance.date <= d1).all():
        recs.setdefault(a.employee_id, {})[a.date] = a
    hol = _holidays(db, d0, d1)
    roster = _roster(db, ids, d0, d1)
    paid = {t.id: bool(t.paid) for t in db.query(M.HrLeaveType).all()}
    st = settings(db)
    rows = []
    for e in emps:
        mine = recs.get(e.id, {})
        cells = {d.isoformat(): day_view(db, e, d, mine.get(d.isoformat()), hol, roster) for d in days}
        rows.append({**emp_brief(e), "shift": e.shift.name if e.shift else None, "weekly_off": e.weekly_off,
                     "days": cells, "totals": counts(db, e, days, mine, hol, roster, paid, st)})
    tday = today()
    return {"month": month, "label": month_label(month), "days": [
        {"date": d.isoformat(), "d": d.day, "dow": WEEKDAYS[d.weekday()][:2], "holiday": hol[d.isoformat()].name
         if d.isoformat() in hol else None, "today": d.isoformat() == tday} for d in days], "rows": rows}


def bulk_mark(db, data, user=None):
    """The register's quick fill: one status for many people on one or more days."""
    ids = [int(x) for x in data.get("employee_ids") or []]
    dates = data.get("dates") or [data.get("date")]
    st = data.get("status")
    if not ids or not dates or st not in STATUSES:
        raise ValueError("Pick the people, the day and the status")
    n = 0
    for eid in ids:
        e = db.get(M.Employee, eid)
        for ds in dates:
            if e is None or not _employed_on(e, day(iso(ds))):
                continue
            mark(db, eid, ds, {"status": st, "remarks": data.get("remarks")}, user)
            n += 1
    return {"marked": n}


# ---- punches: devices, files, the Store -----------------------------------------
def _find_emp(db, key, cache):
    k = str(key or "").strip()
    if not k:
        return None
    if k.lower() in cache:
        return cache[k.lower()]
    e = (db.query(M.Employee).filter(func.upper(M.Employee.code) == k.upper()).first()
         or db.query(M.Employee).filter(func.lower(M.Employee.store_username) == k.lower()).first())
    if e is None and re.match(r"^\d+$", k):
        e = db.query(M.Employee).filter(M.Employee.code.like(f"%{int(k):04d}")).first() \
            or db.query(M.Employee).filter(M.Employee.phone == k).first()
    cache[k.lower()] = e
    return e


def _parse_stamp(v):
    s = str(v or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %I:%M %p"):
        try:
            return dt.datetime.strptime(s[:len(dt.datetime.now().strftime(fmt))] if "%p" not in fmt else s, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def apply_punches(db, punches, source, user=None):
    """[(employee key, datetime)] → each person's day: the first punch is the in,
    the last the out. Returns what was written and what was not recognised."""
    cache, days, unknown = {}, {}, {}
    for key, when in punches:
        e = _find_emp(db, key, cache)
        if e is None:
            unknown[str(key)] = unknown.get(str(key), 0) + 1
            continue
        k = (e.id, when.date().isoformat())
        days.setdefault(k, []).append(when.hour * 60 + when.minute)
    n = 0
    for (eid, ds), mins in days.items():
        mins.sort()
        a = db.query(M.HrAttendance).filter(M.HrAttendance.employee_id == eid, M.HrAttendance.date == ds).first()
        # punches from several sources (or several files) widen the day, never narrow it
        if a is not None and a.in_time:
            mins.append(_minutes(a.in_time))
        if a is not None and a.out_time:
            mins.append(_minutes(a.out_time))
        mins.sort()
        first, last = mins[0], mins[-1]
        mark(db, eid, ds, {"in_time": _hhmm(first), "out_time": _hhmm(last) if last - first >= 5 else None},
             user, source)
        n += 1
    return {"days": n, "unknown": [{"key": k, "punches": v} for k, v in unknown.items()]}


def import_attendance(db, raw: bytes, filename, user=None):
    """Any attendance system's export, as CSV: a row per day (code, date, in,
    out, [status]) or a row per punch (code, date-time). The columns are found by
    their headings, so the device's own export is read as it comes."""
    text = raw.decode("utf-8-sig", errors="replace")
    if filename and filename.lower().endswith((".xlsx", ".xls")):
        raise ValueError("Save the sheet as CSV first (File → Save as → CSV)")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("The file has no rows")
    head = [re.sub(r"[^a-z]", "", h.lower()) for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n or (len(n) > 3 and h.startswith(n)):
                    return i
        return None
    c_key = col("employeecode", "empcode", "code", "employeeid", "empid", "userid", "username", "enrollno",
                "cardno", "employee", "staff", "id")
    c_date = col("date", "attendancedate", "day")
    c_in = col("intime", "in", "checkin", "firstin", "punchin", "timein")
    c_out = col("outtime", "out", "checkout", "lastout", "punchout", "timeout")
    c_stamp = col("datetime", "punchtime", "timestamp", "logtime", "time")
    c_status = col("status", "attendance")
    if c_key is None:
        raise ValueError("No column says who — name one Employee Code (or Username / Emp ID)")
    punches, days, unknown, bad = [], [], {}, 0
    cache = {}
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        get = (lambda i: r[i].strip() if i is not None and i < len(r) else "")
        key = get(c_key)
        if c_date is not None and (c_in is not None or c_status is not None):
            ds = _date(get(c_date))
            if ds is None:
                bad += 1
                continue
            days.append((key, ds.isoformat(), get(c_in), get(c_out), get(c_status).upper()))
        elif c_stamp is not None:
            stamp = get(c_stamp)
            if c_date is not None and len(stamp) <= 8:
                stamp = f"{get(c_date)} {stamp}"
            when = _parse_stamp(stamp)
            if when is None:
                bad += 1
                continue
            punches.append((key, when))
        else:
            raise ValueError("Name a Date column with In / Out, or a Date-Time column of punches")
    out = apply_punches(db, punches, "import", user) if punches else {"days": 0, "unknown": []}
    n = out["days"]
    for key, ds, tin, tout, st in days:
        e = _find_emp(db, key, cache)
        if e is None:
            unknown[key] = unknown.get(key, 0) + 1
            continue
        data = {}
        if tin:
            data["in_time"] = tin
        if tout:
            data["out_time"] = tout
        st = {"PRESENT": "P", "ABSENT": "A", "HALFDAY": "HD", "HALF DAY": "HD", "LEAVE": "L", "WEEKOFF": "WO",
              "WEEKLY OFF": "WO", "HOLIDAY": "H", "OFF": "WO"}.get(st, st)
        if st in STATUSES:
            data["status"] = st
        if data:
            mark(db, e.id, ds, data, user, "import")
            n += 1
    for u in out["unknown"]:
        unknown[u["key"]] = unknown.get(u["key"], 0) + u["punches"]
    return {"days": n, "skipped": bad, "unknown": [{"key": k, "rows": v} for k, v in unknown.items()]}


def sync_store_attendance(db, date_from, date_to, user=None):
    """The Store's check-ins (made at sign-in at the till) as attendance."""
    if not pos_sales.available():
        raise ValueError("The Store's database cannot be read from here")
    d0, d1 = iso(date_from, True), iso(date_to, True)
    rows = pos_sales._rows(
        f"SELECT u.username, a.check_in, a.check_out FROM {pos_sales.q('attendance')} a "
        f"JOIN {pos_sales.q('users')} u ON u.id = a.user_id WHERE a.check_in >= ? AND a.check_in <= ?",
        [d0, d1 + " 23:59:59"])
    punches = []
    for username, cin, cout in rows:
        for ts in (cin, cout):
            loc = business_day.local(ts) if ts else None
            if loc is not None:
                punches.append((username, loc))
    return apply_punches(db, punches, "store", user)


def device_punches(db, body):
    """What an attendance device (or its middleware) pushes: {"punches": [{"id":
    "EMP0001", "time": "2026-11-04 09:58"}]}."""
    pairs = []
    for p in (body or {}).get("punches") or []:
        when = _parse_stamp(p.get("time") or p.get("timestamp"))
        key = p.get("id") or p.get("code") or p.get("employee") or p.get("user")
        if when and key:
            pairs.append((key, when))
    if not pairs:
        raise ValueError("No punches — send {\"punches\": [{\"id\": \"EMP0001\", \"time\": \"2026-11-04 09:58\"}]}")
    return apply_punches(db, pairs, "device", {"username": "device"})


# ---- roster and overtime -------------------------------------------------------------
def roster_week(db, start, department_id=None, location=None):
    d0 = day(iso(start) or today())
    d0 = d0 - dt.timedelta(days=d0.weekday())
    dates = [d0 + dt.timedelta(days=i) for i in range(7)]
    a, b = dates[0].isoformat(), dates[-1].isoformat()
    emps = _active_in(db, a, b, department_id, location)
    ros = _roster(db, [e.id for e in emps], a, b)
    hol = _holidays(db, a, b)
    rows = []
    for e in emps:
        cells = {}
        for d in dates:
            s = d.isoformat()
            r = ros.get((e.id, s))
            if not _employed_on(e, d):
                cells[s] = {"out": True}
            elif r is not None:
                cells[s] = {"shift_id": r.shift_id, "off": bool(r.off), "set": True}
            else:
                off = bool(e.weekly_off and WEEKDAYS[d.weekday()] == e.weekly_off)
                cells[s] = {"shift_id": None if off else e.shift_id, "off": off, "set": False}
        rows.append({**emp_brief(e), "shift_id": e.shift_id, "weekly_off": e.weekly_off, "days": cells})
    return {"start": a, "dates": [{"date": d.isoformat(), "dow": WEEKDAYS[d.weekday()][:3], "d": d.day,
                                   "holiday": hol[d.isoformat()].name if d.isoformat() in hol else None}
                                  for d in dates], "rows": rows}


def assign_roster(db, data):
    ids = [int(x) for x in data.get("employee_ids") or []]
    d0, d1 = iso(data.get("date_from"), True, "first day"), iso(data.get("date_to") or data.get("date_from"))
    shift_id = data.get("shift_id")
    off = bool(data.get("off"))
    clear = bool(data.get("clear"))
    if not ids:
        raise ValueError("Pick the people")
    if not clear and not off and not shift_id:
        raise ValueError("Pick a shift, or mark the days off")
    n = 0
    d = day(d0)
    while d <= day(d1):
        for eid in ids:
            r = db.query(M.HrRoster).filter(M.HrRoster.employee_id == eid, M.HrRoster.date == d.isoformat()).first()
            if clear:
                if r is not None:
                    db.delete(r)
                    n += 1
                continue
            if r is None:
                r = M.HrRoster(employee_id=eid, date=d.isoformat())
                db.add(r)
            r.shift_id, r.off = (None, True) if off else (int(shift_id), False)
            n += 1
        d += dt.timedelta(days=1)
    db.flush()
    return {"changed": n}


def overtime(db, date_from, date_to, status=None):
    q = db.query(M.HrAttendance).filter(M.HrAttendance.date >= iso(date_from, True),
                                        M.HrAttendance.date <= iso(date_to, True),
                                        M.HrAttendance.ot_minutes > 0)
    if status:
        q = q.filter(M.HrAttendance.ot_status == status)
    emps = {e.id: e for e in db.query(M.Employee).all()}
    st = settings(db)
    out = []
    for a in q.order_by(M.HrAttendance.date.desc()).all():
        e = emps.get(a.employee_id)
        if e is None:
            continue
        rate = ot_rate(e, st)
        hrs = (a.ot_approved_minutes if a.ot_status == "approved" else a.ot_minutes) / 60
        out.append({"id": a.id, "employee_id": e.id, "code": e.code, "name": e.name, "date": a.date,
                    "in_time": a.in_time, "out_time": a.out_time, "worked": a.worked_minutes,
                    "ot_minutes": a.ot_minutes, "approved_minutes": a.ot_approved_minutes or 0,
                    "status": a.ot_status or "pending", "rate": r2(rate), "amount": rupee(rate * hrs)})
    return out


def ot_rate(e, st):
    sal = e.salary or {}
    base = gross_of(sal) if st.get("ot_on") != "basic" else r2(float(sal.get("basic") or 0) + float(sal.get("da") or 0))
    hours = (e.shift.full_day_hours if e.shift else 9) or 9
    divisor = 26 if st.get("month_days") == "26" else 30
    return base / divisor / hours * float(st.get("ot_multiplier") or 2)


def decide_overtime(db, ids, decision, minutes=None, user=None):
    if decision not in ("approved", "rejected", "pending"):
        raise ValueError("Approve or reject")
    n = 0
    for aid in ids or []:
        a = db.get(M.HrAttendance, int(aid))
        if a is None or not a.ot_minutes:
            continue
        a.ot_status = decision
        a.ot_approved_minutes = (int(minutes) if minutes not in (None, "") and len(ids) == 1 else a.ot_minutes) \
            if decision == "approved" else 0
        a.updated_by = _user(user)
        n += 1
    return {"changed": n}


# ---------------------------------------------------------------------------
#  Leave
# ---------------------------------------------------------------------------
def leave_balances(db, eid, as_on=None):
    as_on = iso(as_on) or "9999-12-31"
    sums = dict(db.query(M.HrLeaveLedger.leave_type_id, func.sum(M.HrLeaveLedger.days))
                  .filter(M.HrLeaveLedger.employee_id == eid, M.HrLeaveLedger.date <= as_on)
                  .group_by(M.HrLeaveLedger.leave_type_id).all())
    taken = dict(db.query(M.HrLeaveLedger.leave_type_id, func.sum(M.HrLeaveLedger.days))
                   .filter(M.HrLeaveLedger.employee_id == eid, M.HrLeaveLedger.kind == "availed",
                           M.HrLeaveLedger.date <= as_on, M.HrLeaveLedger.date >= as_on[:4] + "-01-01")
                   .group_by(M.HrLeaveLedger.leave_type_id).all())
    pending = dict(db.query(M.HrLeaveRequest.leave_type_id, func.sum(M.HrLeaveRequest.days))
                     .filter(M.HrLeaveRequest.employee_id == eid, M.HrLeaveRequest.status == "pending")
                     .group_by(M.HrLeaveRequest.leave_type_id).all())
    out = []
    for t in db.query(M.HrLeaveType).filter(M.HrLeaveType.active.isnot(False)).order_by(M.HrLeaveType.id).all():
        out.append({"leave_type_id": t.id, "code": t.code, "name": t.name, "color": t.color, "paid": t.paid,
                    "quota": t.annual_quota, "balance": r2(sums.get(t.id) or 0),
                    "taken_this_year": r2(-(taken.get(t.id) or 0)), "pending": r2(pending.get(t.id) or 0)})
    return out


def leave_ledger(db, eid, leave_type_id=None):
    q = db.query(M.HrLeaveLedger).filter(M.HrLeaveLedger.employee_id == eid)
    if leave_type_id:
        q = q.filter(M.HrLeaveLedger.leave_type_id == int(leave_type_id))
    types = {t.id: t.code for t in db.query(M.HrLeaveType).all()}
    rows, bal = [], {}
    for x in q.order_by(M.HrLeaveLedger.date, M.HrLeaveLedger.id).all():
        bal[x.leave_type_id] = r2(bal.get(x.leave_type_id, 0) + (x.days or 0))
        rows.append({**_master_out(x), "type": types.get(x.leave_type_id), "balance": bal[x.leave_type_id]})
    return list(reversed(rows))


def _leave_days(db, e, d0, d1, half):
    """Working days the leave covers — their weekly offs and holidays are not leave."""
    hol = _holidays(db, d0, d1)
    ros = _roster(db, [e.id], d0, d1)
    out = []
    d = day(d0)
    while d <= day(d1):
        s = d.isoformat()
        r = ros.get((e.id, s))
        off = (r is not None and r.off) or (r is None and e.weekly_off and WEEKDAYS[d.weekday()] == e.weekly_off)
        if not off and s not in hol:
            out.append(s)
        d += dt.timedelta(days=1)
    return out, (0.5 if half else float(len(out)))


def apply_leave(db, data, user=None):
    e = db.get(M.Employee, int(data.get("employee_id") or 0))
    if e is None:
        raise ValueError("Pick the employee")
    t = db.get(M.HrLeaveType, int(data.get("leave_type_id") or 0))
    if t is None:
        raise ValueError("Pick the kind of leave")
    d0 = iso(data.get("from_date"), True, "first day")
    d1 = iso(data.get("to_date")) or d0
    if d1 < d0:
        raise ValueError("The leave ends before it starts")
    half = bool(data.get("half_day"))
    if half and d0 != d1:
        raise ValueError("A half day is one day — make the dates the same")
    dates, days = _leave_days(db, e, d0, d1, half)
    if not dates:
        raise ValueError("Those days are all weekly offs or holidays")
    clash = db.query(M.HrLeaveRequest).filter(
        M.HrLeaveRequest.employee_id == e.id, M.HrLeaveRequest.status.in_(("pending", "approved")),
        M.HrLeaveRequest.from_date <= d1, M.HrLeaveRequest.to_date >= d0).first()
    if clash:
        raise ValueError(f"{e.name} already has {clash.number} ({clash.status}) on those days")
    if t.paid and not t.allow_negative:
        bal = next((b for b in leave_balances(db, e.id) if b["leave_type_id"] == t.id), None)
        free = (bal["balance"] - bal["pending"]) if bal else 0
        if days > free + 1e-9:
            raise ValueError(f"{e.name} has {free:g} day(s) of {t.code} left — this asks for {days:g}. "
                             "Use Loss of Pay for the rest.")
    r = M.HrLeaveRequest(number=_next_no(db, M.HrLeaveRequest, "number", "LV-"), employee_id=e.id,
                         leave_type_id=t.id, from_date=d0, to_date=d1, half_day=half, days=days,
                         reason=data.get("reason"), applied_by=_user(user))
    db.add(r)
    db.flush()
    if data.get("approve"):
        decide_leave(db, r.id, "approved", data.get("note"), user)
    return r


def decide_leave(db, rid, decision, note=None, user=None):
    r = db.get(M.HrLeaveRequest, rid)
    if r is None:
        raise LookupError("leave request not found")
    if decision not in ("approved", "rejected", "cancelled"):
        raise ValueError("Approve, reject or cancel")
    if decision in ("approved", "rejected") and r.status != "pending":
        raise ValueError(f"{r.number} is already {r.status}")
    if decision == "cancelled" and r.status not in ("pending", "approved"):
        raise ValueError(f"{r.number} is {r.status}")
    if decision == "cancelled" and r.status == "approved":
        _locked_month(db, r.from_date, r.to_date)
        db.query(M.HrLeaveLedger).filter(M.HrLeaveLedger.ref == f"LV:{r.id}").delete()
        for a in db.query(M.HrAttendance).filter(M.HrAttendance.employee_id == r.employee_id,
                                                 M.HrAttendance.date >= r.from_date,
                                                 M.HrAttendance.date <= r.to_date,
                                                 M.HrAttendance.source == "leave").all():
            db.delete(a)
    if decision == "approved":
        _locked_month(db, r.from_date, r.to_date)
        e = r.employee
        dates, _ = _leave_days(db, e, r.from_date, r.to_date, r.half_day)
        db.add(M.HrLeaveLedger(employee_id=r.employee_id, leave_type_id=r.leave_type_id, date=r.from_date,
                               kind="availed", days=-r.days, ref=f"LV:{r.id}", created_by=_user(user),
                               remarks=f"{r.number} {r.from_date}" + (f" to {r.to_date}" if r.to_date != r.from_date else "")))
        for ds in dates:
            a = db.query(M.HrAttendance).filter(M.HrAttendance.employee_id == r.employee_id,
                                                M.HrAttendance.date == ds).first()
            if a is None:
                a = M.HrAttendance(employee_id=r.employee_id, date=ds)
                db.add(a)
            a.status = "HD" if r.half_day else "L"
            a.leave_type_id = r.leave_type_id
            a.source = "leave"
            a.remarks = f"{r.leave_type.code} · {r.number}"
            a.updated_by = _user(user)
    r.status = decision
    r.decided_by = _user(user)
    r.decided_at = dt.datetime.utcnow()
    r.decision_note = note
    db.flush()
    return r


def _locked_month(db, d0, d1):
    for m in {d0[:7], (d1 or d0)[:7]}:
        run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == m).first()
        if run is not None and run.status != "draft":
            raise ValueError(f"{month_label(m)}'s payroll is {run.status} — reopen it before changing leave in it")


def leave_out(r):
    return {"id": r.id, "number": r.number, "employee_id": r.employee_id,
            "employee": r.employee.name if r.employee else None, "code": r.employee.code if r.employee else None,
            "leave_type_id": r.leave_type_id, "type": r.leave_type.code if r.leave_type else None,
            "type_name": r.leave_type.name if r.leave_type else None,
            "color": r.leave_type.color if r.leave_type else None,
            "from_date": r.from_date, "to_date": r.to_date, "half_day": r.half_day, "days": r.days,
            "reason": r.reason, "status": r.status, "applied_by": r.applied_by,
            "applied_at": r.applied_at.isoformat() if r.applied_at else None, "decided_by": r.decided_by,
            "decision_note": r.decision_note}


def list_leave(db, status=None, employee_id=None, date_from=None, date_to=None):
    q = db.query(M.HrLeaveRequest)
    if status:
        q = q.filter(M.HrLeaveRequest.status == status)
    if employee_id:
        q = q.filter(M.HrLeaveRequest.employee_id == int(employee_id))
    if date_from:
        q = q.filter(M.HrLeaveRequest.to_date >= iso(date_from))
    if date_to:
        q = q.filter(M.HrLeaveRequest.from_date <= iso(date_to))
    return [leave_out(r) for r in q.order_by(M.HrLeaveRequest.id.desc()).limit(500).all()]


def adjust_leave(db, data, user=None):
    eid, tid = int(data.get("employee_id") or 0), int(data.get("leave_type_id") or 0)
    if not db.get(M.Employee, eid) or not db.get(M.HrLeaveType, tid):
        raise ValueError("Pick the employee and the kind of leave")
    days = float(data.get("days") or 0)
    if not days:
        raise ValueError("Enter the days to add (or a minus to take away)")
    kind = data.get("kind") or "adjust"
    x = M.HrLeaveLedger(employee_id=eid, leave_type_id=tid, date=iso(data.get("date")) or today(),
                        kind=kind, days=days, ref=f"ADJ:{secrets.token_hex(4)}",
                        remarks=data.get("remarks"), created_by=_user(user))
    db.add(x)
    db.flush()
    return _master_out(x)


def run_accrual(db, month, user=None):
    """Credit a month's leave: a twelfth of each monthly type's quota to everyone
    employed that month, and in January (or on the first run of a year) the
    whole quota of each yearly type, pro-rated for someone joining mid-year.
    Safe to run twice — each credit is keyed and written once."""
    month = month_of(month)
    days = month_days(month)
    d0, d1 = days[0].isoformat(), days[-1].isoformat()
    year = month[:4]
    types = db.query(M.HrLeaveType).filter(M.HrLeaveType.active.isnot(False)).all()
    made = 0
    for e in _active_in(db, d0, d1):
        for t in types:
            if t.accrual == "monthly" and t.annual_quota:
                ref = f"ACC:{month}"
                share = r2(t.annual_quota / 12)
                if e.joined_on and e.joined_on > d0:          # joined in the month: by days
                    share = r2(share * (len(days) - day(e.joined_on).day + 1) / len(days))
                if share and not _has_ref(db, e.id, t.id, ref):
                    db.add(M.HrLeaveLedger(employee_id=e.id, leave_type_id=t.id, date=d1, kind="accrual",
                                           days=share, ref=ref, remarks=f"{month_label(month)}",
                                           created_by=_user(user)))
                    made += 1
            elif t.accrual == "yearly" and t.annual_quota:
                ref = f"GRANT:{year}"
                if _has_ref(db, e.id, t.id, ref):
                    continue
                months_left = 12
                if e.joined_on and e.joined_on[:4] == year:
                    months_left = 12 - int(e.joined_on[5:7]) + 1
                grant = r2(t.annual_quota * months_left / 12)
                if grant:
                    db.add(M.HrLeaveLedger(employee_id=e.id, leave_type_id=t.id,
                                           date=max(f"{year}-01-01", e.joined_on or ""), kind="grant",
                                           days=grant, ref=ref, remarks=f"{year} quota", created_by=_user(user)))
                    made += 1
    db.flush()
    return {"credited": made, "month": month}


def year_end(db, year, user=None):
    """Carry what each type allows into next year; the rest lapses."""
    year = str(year)
    end = f"{year}-12-31"
    made = 0
    for t in db.query(M.HrLeaveType).all():
        rows = (db.query(M.HrLeaveLedger.employee_id, func.sum(M.HrLeaveLedger.days))
                  .filter(M.HrLeaveLedger.leave_type_id == t.id, M.HrLeaveLedger.date <= end)
                  .group_by(M.HrLeaveLedger.employee_id).all())
        for eid, bal in rows:
            bal = r2(bal)
            lapse = r2(bal - min(bal, float(t.carry_forward_max or 0)))
            ref = f"LAPSE:{year}"
            if lapse > 0 and not _has_ref(db, eid, t.id, ref):
                db.add(M.HrLeaveLedger(employee_id=eid, leave_type_id=t.id, date=end, kind="lapse", days=-lapse,
                                       ref=ref, remarks=f"Not carried into {int(year) + 1}", created_by=_user(user)))
                made += 1
    db.flush()
    return {"lapsed": made}


def _has_ref(db, eid, tid, ref):
    return db.query(M.HrLeaveLedger.id).filter(M.HrLeaveLedger.employee_id == eid,
                                               M.HrLeaveLedger.leave_type_id == tid,
                                               M.HrLeaveLedger.ref == ref).first() is not None


def leave_calendar(db, month):
    month = month_of(month)
    days = month_days(month)
    d0, d1 = days[0].isoformat(), days[-1].isoformat()
    reqs = db.query(M.HrLeaveRequest).filter(M.HrLeaveRequest.status.in_(("approved", "pending")),
                                             M.HrLeaveRequest.from_date <= d1, M.HrLeaveRequest.to_date >= d0).all()
    return {"month": month, "label": month_label(month), "requests": [leave_out(r) for r in reqs],
            "holidays": [_master_out(h) for h in db.query(M.HrHoliday).filter(M.HrHoliday.date >= d0,
                                                                            M.HrHoliday.date <= d1).all()]}


# ---------------------------------------------------------------------------
#  Advances and loans
# ---------------------------------------------------------------------------
def advance_out(db, a):
    rec = db.query(func.coalesce(func.sum(M.HrAdvanceRecovery.amount), 0)).filter(
        M.HrAdvanceRecovery.advance_id == a.id).scalar() or 0
    return {"id": a.id, "number": a.number, "employee_id": a.employee_id,
            "employee": a.employee.name if a.employee else None, "code": a.employee.code if a.employee else None,
            "kind": a.kind, "amount": r2(a.amount), "issued_on": a.issued_on, "emi": r2(a.emi),
            "recover_from": a.recover_from, "mode": a.mode, "reason": a.reason, "status": a.status,
            "recovered": r2(rec), "balance": r2((a.amount or 0) - rec), "voucher_id": a.voucher_id,
            "voucher_no": None}


def hr_ledger(db, key):
    name, group = HR_LEDGERS[key]
    L = db.query(M.Ledger).filter(M.Ledger.source == "hr", M.Ledger.alias == key).first()
    if L is None:
        L = M.Ledger(name=name, group=group, alias=key, source="hr", tags=[])
        db.add(L)
        db.flush()
    return L


def _money_ledger(db, mode, ledger_id=None):
    if ledger_id:
        L = db.get(M.Ledger, int(ledger_id))
        if L is None:
            raise ValueError("That bank or cash ledger does not exist")
        return L
    return finance.system_ledger(db, "cash" if (mode or "Cash") == "Cash" else "bank")


def list_advances(db, status=None, employee_id=None):
    q = db.query(M.HrAdvance)
    if status:
        q = q.filter(M.HrAdvance.status == status)
    if employee_id:
        q = q.filter(M.HrAdvance.employee_id == int(employee_id))
    vno = {v.id: v.voucher_no for v in db.query(M.Voucher).filter(
        M.Voucher.id.in_([a.voucher_id for a in q.all() if a.voucher_id] or [0])).all()}
    out = []
    for a in q.order_by(M.HrAdvance.id.desc()).all():
        d = advance_out(db, a)
        d["voucher_no"] = vno.get(a.voucher_id)
        out.append(d)
    return out


def give_advance(db, data, user=None):
    e = db.get(M.Employee, int(data.get("employee_id") or 0))
    if e is None:
        raise ValueError("Pick the employee")
    amount = r2(data.get("amount"))
    if amount <= 0:
        raise ValueError("Enter the amount")
    emi = r2(data.get("emi")) or amount
    if emi > amount:
        emi = amount
    issued = iso(data.get("issued_on")) or today()
    a = M.HrAdvance(number=_next_no(db, M.HrAdvance, "number", "ADV-"), employee_id=e.id,
                    kind=data.get("kind") or "Advance", amount=amount, issued_on=issued, emi=emi,
                    recover_from=month_of(data.get("recover_from") or issued), mode=data.get("mode") or "Cash",
                    reason=data.get("reason"), created_by=_user(user))
    db.add(a)
    db.flush()
    if data.get("post", True):
        money = _money_ledger(db, a.mode, data.get("ledger_id"))
        v = finance.save_voucher(db, {
            "vtype": "Payment", "date": issued, "mode": a.mode, "location": e.location,
            "narration": f"{a.kind} {a.number} to {e.name} ({e.code})" + (f" — {a.reason}" if a.reason else ""),
            "lines": [{"ledger_id": hr_ledger(db, "staff_advances").id, "debit": amount, "narration": e.name,
                       "bill_ref": f"ADV:{a.id}"},
                      {"ledger_id": money.id, "credit": amount}]}, user, source="hr_advance", source_id=a.id)
        a.voucher_id = v.id
    return a


def repay_advance(db, aid, data, user=None):
    a = db.get(M.HrAdvance, aid)
    if a is None:
        raise LookupError("advance not found")
    bal = advance_out(db, a)["balance"]
    amount = r2(data.get("amount"))
    if amount <= 0 or amount > bal + 0.01:
        raise ValueError(f"Enter an amount up to the balance of ₹{bal:,.2f}")
    when = iso(data.get("date")) or today()
    rec = M.HrAdvanceRecovery(advance_id=a.id, month=when[:7], date=when, amount=amount, source="manual")
    db.add(rec)
    db.flush()
    if data.get("post", True):
        money = _money_ledger(db, data.get("mode") or "Cash", data.get("ledger_id"))
        v = finance.save_voucher(db, {
            "vtype": "Receipt", "date": when, "mode": data.get("mode") or "Cash",
            "narration": f"Repayment of {a.number} by {a.employee.name}",
            "lines": [{"ledger_id": money.id, "debit": amount},
                      {"ledger_id": hr_ledger(db, "staff_advances").id, "credit": amount, "bill_ref": f"ADV:{a.id}"}]},
            user, source="hr_advance_repay", source_id=rec.id)
        rec.voucher_id = v.id
    if bal - amount <= 0.01:
        a.status = "closed"
    return advance_out(db, a)


# ---------------------------------------------------------------------------
#  Sales incentive
# ---------------------------------------------------------------------------
def scheme_out(s):
    d = _master_out(s)
    d["filters"] = s.filters or {}
    d["applies_to"] = s.applies_to or {}
    d["slabs"] = s.slabs or []
    return d


def save_scheme(db, data, sid=None):
    s = db.get(M.HrIncentiveScheme, sid) if sid else M.HrIncentiveScheme()
    if sid and s is None:
        raise LookupError("scheme not found")
    s.name = (data.get("name") or "").strip()
    if not s.name:
        raise ValueError("Name the scheme")
    s.basis = data.get("basis") or "pct"
    if s.basis not in dict(INCENTIVE_BASES):
        raise ValueError("Pick how it pays")
    s.rate = r2(data.get("rate"))
    s.slabs = [{"from": r2(x.get("from")), "to": r2(x.get("to")) if x.get("to") not in (None, "") else None,
                "pct": r2(x.get("pct"))} for x in (data.get("slabs") or []) if x.get("pct") not in (None, "")]
    if s.basis == "slab" and not s.slabs:
        raise ValueError("Add at least one slab")
    if s.basis != "slab" and not s.rate:
        raise ValueError("Enter the rate")
    f = data.get("filters") or {}

    def lst(v):
        if isinstance(v, str):
            v = re.split(r"[,\n]", v)
        return [str(x).strip() for x in (v or []) if str(x).strip()]
    s.filters = {"categories": lst(f.get("categories")), "brands": lst(f.get("brands")),
                 "barcodes": lst(f.get("barcodes")), "locations": lst(f.get("locations")),
                 "age_min": int(f["age_min"]) if f.get("age_min") not in (None, "") else None,
                 "age_max": int(f["age_max"]) if f.get("age_max") not in (None, "") else None,
                 "price_min": r2(f["price_min"]) if f.get("price_min") not in (None, "") else None,
                 "price_max": r2(f["price_max"]) if f.get("price_max") not in (None, "") else None}
    ap = data.get("applies_to") or {}
    s.applies_to = {"employees": [int(x) for x in ap.get("employees") or []],
                    "designations": [int(x) for x in ap.get("designations") or []]}
    s.valid_from = iso(data.get("valid_from"))
    s.valid_to = iso(data.get("valid_to"))
    s.active = bool(data.get("active", True))
    s.notes = data.get("notes")
    if not sid:
        db.add(s)
    db.flush()
    return scheme_out(s)


def _till_lines(date_from, date_to):
    q = pos_sales.q
    where, params = pos_sales._window(date_from, date_to, "i.invoice_date")
    return pos_sales._rows(
        "SELECT u.username, i.id, i.invoice_date, ii.quantity, ii.unit_price, ii.line_total, "
        "       p.sku, p.barcode, p.brand, cat.name, COALESCE(p.purchase_date, p.created_at), l.name "
        f"FROM {q('invoice_items')} ii JOIN {q('invoices')} i ON i.id = ii.invoice_id "
        f"JOIN {q('users')} u ON u.id = COALESCE(ii.staff_id, i.staff_id) "
        f"JOIN {q('products')} p ON p.id = ii.product_id "
        f"LEFT JOIN {q('categories')} cat ON cat.id = p.category_id "
        f"LEFT JOIN {q('locations')} l ON l.id = i.location_id "
        "WHERE 1=1" + pos_sales.LIVE_BILL + where, params)


def _age_days(sold, since):
    a = str(sold or "")[:10]
    b = str(since or "")[:10]
    try:
        return (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days
    except ValueError:
        return None


def _line_matches(s, ln):
    f = s.filters or {}
    sold_day = str(ln["date"] or "")[:10]
    if s.valid_from and sold_day < s.valid_from:
        return False
    if s.valid_to and sold_day > s.valid_to:
        return False
    low = lambda xs: {x.lower() for x in xs or []}       # noqa: E731
    if f.get("categories") and (ln["category"] or "").lower() not in low(f["categories"]):
        return False
    if f.get("brands") and (ln["brand"] or "").lower() not in low(f["brands"]):
        return False
    if f.get("barcodes"):
        codes = low(f["barcodes"])
        if (ln["sku"] or "").lower() not in codes and (ln["barcode"] or "").lower() not in codes:
            return False
    if f.get("locations") and (ln["location"] or "").lower() not in low(f["locations"]):
        return False
    age = ln["age"]
    if f.get("age_min") is not None and (age is None or age < f["age_min"]):
        return False
    if f.get("age_max") is not None and (age is None or age > f["age_max"]):
        return False
    if f.get("price_min") is not None and ln["price"] < f["price_min"]:
        return False
    if f.get("price_max") is not None and ln["price"] > f["price_max"]:
        return False
    return True


def _applies(s, e):
    ap = s.applies_to or {}
    if not ap.get("employees") and not ap.get("designations"):
        return True
    return e.id in (ap.get("employees") or []) or (e.designation_id in (ap.get("designations") or []))


def incentives(db, date_from, date_to, scheme_ids=None):
    """Each person's incentive under each scheme, from the bill lines they sold."""
    d0, d1 = iso(date_from, True), iso(date_to, True)
    schemes = db.query(M.HrIncentiveScheme).filter(M.HrIncentiveScheme.active.isnot(False))
    if scheme_ids:
        schemes = schemes.filter(M.HrIncentiveScheme.id.in_(scheme_ids))
    schemes = schemes.all()
    if not pos_sales.available():
        return {"available": False, "rows": [], "schemes": [scheme_out(s) for s in schemes], "unlinked": []}
    by_user = {(e.store_username or "").lower(): e for e in db.query(M.Employee)
               .filter(M.Employee.store_username.isnot(None)).all()}
    per, unlinked = {}, {}
    for (username, inv_id, date, qty, price, value, sku, barcode, brand, cat, since, loc) in _till_lines(d0, d1):
        e = by_user.get((username or "").lower())
        if e is None:
            u = unlinked.setdefault(username, {"username": username, "sales": 0.0, "lines": 0})
            u["sales"] = r2(u["sales"] + float(value or 0))
            u["lines"] += 1
            continue
        ln = {"date": date, "qty": float(qty or 0), "price": float(price or 0), "value": float(value or 0),
              "sku": sku, "barcode": barcode, "brand": brand, "category": cat, "location": loc,
              "age": _age_days(date, since)}
        p = per.setdefault(e.id, {"employee": e, "bills": set(), "qty": 0.0, "sales": 0.0, "by": {}, "eligible": {}})
        p["bills"].add(inv_id)
        p["qty"] += ln["qty"]
        p["sales"] += ln["value"]
        for s in schemes:
            if not _applies(s, e) or not _line_matches(s, ln):
                continue
            if s.basis == "pct":
                p["by"][s.id] = p["by"].get(s.id, 0) + ln["value"] * (s.rate or 0) / 100
            elif s.basis == "per_piece":
                p["by"][s.id] = p["by"].get(s.id, 0) + ln["qty"] * (s.rate or 0)
            p["eligible"][s.id] = p["eligible"].get(s.id, 0) + ln["value"]
    rows = []
    for eid, p in per.items():
        for s in schemes:
            if s.basis == "slab" and s.id in p["eligible"]:
                total = p["eligible"][s.id]
                pct = 0
                for sl in sorted(s.slabs or [], key=lambda x: x["from"] or 0):
                    if total >= (sl["from"] or 0) and (sl["to"] is None or total <= sl["to"]):
                        pct = sl["pct"]
                p["by"][s.id] = total * pct / 100
        e = p["employee"]
        by = {sid: rupee(v) for sid, v in p["by"].items()}
        rows.append({"employee_id": e.id, "code": e.code, "name": e.name, "location": e.location,
                     "designation": e.designation.name if e.designation else None,
                     "bills": len(p["bills"]), "qty": r2(p["qty"]), "sales": r2(p["sales"]),
                     "by_scheme": by, "eligible": {k: r2(v) for k, v in p["eligible"].items()},
                     "incentive": r2(sum(by.values()))})
    rows.sort(key=lambda r: -r["sales"])
    return {"available": True, "from": d0, "to": d1, "rows": rows,
            "schemes": [scheme_out(s) for s in schemes],
            "unlinked": sorted(unlinked.values(), key=lambda u: -u["sales"]),
            "totals": {"sales": r2(sum(r["sales"] for r in rows)), "incentive": r2(sum(r["incentive"] for r in rows))}}


def store_sales_of(db, e, d0, d1):
    """(sales value, bills) one person sold at the Store between two days."""
    if not e.store_username or not pos_sales.available():
        return 0.0, 0
    where, params = pos_sales._window(d0, d1, "i.invoice_date")
    r = pos_sales._rows(
        f"SELECT SUM(ii.line_total), COUNT(DISTINCT i.id) FROM {pos_sales.q('invoice_items')} ii "
        f"JOIN {pos_sales.q('invoices')} i ON i.id = ii.invoice_id "
        f"JOIN {pos_sales.q('users')} u ON u.id = COALESCE(ii.staff_id, i.staff_id) "
        "WHERE LOWER(u.username) = ?" + pos_sales.LIVE_BILL + where, [e.store_username.lower()] + params)
    return (r2(r[0][0]), int(r[0][1] or 0)) if r else (0.0, 0)


# ---------------------------------------------------------------------------
#  Payroll
# ---------------------------------------------------------------------------
def pt_for(gross, st):
    for s in st.get("pt_slabs") or []:
        if s.get("upto") in (None, "") or gross <= float(s["upto"]):
            return float(s.get("amount") or 0)
    return 0.0


def _tax(income, slabs):
    tax, lo = 0.0, 0.0
    for upto, pct in slabs:
        hi = float(upto) if upto not in (None, "") else float("inf")
        if income > lo:
            tax += (min(income, hi) - lo) * float(pct) / 100
        lo = hi
    return tax


def annual_tax(annual_gross, regime, st, pf_year=0.0):
    """Income tax on a year's salary, with the standard deduction, the rebate
    and cess. Old regime takes the employee's PF as 80C (up to ₹1.5 lakh)."""
    if regime == "old":
        taxable = max(0.0, annual_gross - float(st["std_deduction_old"]) - min(pf_year, 150000.0))
        tax = _tax(taxable, st["old_regime_slabs"])
        if taxable <= float(st["old_regime_rebate_upto"]):
            tax = 0.0
    else:
        taxable = max(0.0, annual_gross - float(st["std_deduction_new"]))
        tax = _tax(taxable, st["new_regime_slabs"])
        if taxable <= float(st["new_regime_rebate_upto"]):
            tax = 0.0
    return tax * (1 + float(st["cess_pct"]) / 100), taxable


def _divisor(st, cal_days):
    return {"26": 26, "30": 30}.get(str(st.get("month_days")), cal_days)


def compute_slip(db, e, month, st, att, incentive_amt, adjustments=None, advances=None):
    """One person's pay for a month: the earnings, the deductions and the net."""
    adj = dict(adjustments or {})
    days = month_days(month)
    cal = len(days)
    divisor = _divisor(st, cal)
    c = att
    if divisor == cal:
        factor = c["paid_days"] / cal if cal else 0
    else:
        unpaid = c["lop"] + (cal - c["payable"])
        factor = max(0.0, (divisor - unpaid) / divisor)
    sal = {k: float((e.salary or {}).get(k) or 0) for k in COMPONENT_KEYS}
    rate_gross = sum(sal.values())
    earn = {k: rupee(v * factor) for k, v in sal.items() if v}
    ot_hours = r2(c["ot_approved"] / 60)
    ot_pay = rupee(ot_rate(e, st) * ot_hours) if ot_hours else 0.0
    if ot_pay:
        earn["ot"] = ot_pay
    if incentive_amt:
        earn["incentive"] = rupee(incentive_amt)
    for k in ("bonus", "arrears"):
        if float(adj.get(k) or 0):
            earn[k] = rupee(adj[k])
    gross = r2(sum(earn.values()))
    ded, er = {}, {}
    pf_wages = esi_wages = 0.0
    if e.pf_applicable:
        pf_wages = earn.get("basic", 0) + earn.get("da", 0)
        ceiling = float(st["pf_wage_ceiling"])
        if st.get("pf_cap_at_ceiling"):
            pf_wages = min(pf_wages, ceiling)
        if pf_wages:
            ded["pf"] = rupee(pf_wages * float(st["pf_employee_pct"]) / 100)
            er_total = rupee(pf_wages * float(st["pf_employer_pct"]) / 100)
            eps = rupee(min(pf_wages, ceiling) * float(st["eps_pct"]) / 100)
            er["eps"] = min(eps, er_total)
            er["pf"] = er_total - er["eps"]
    if e.esi_applicable and rate_gross <= float(st["esi_wage_limit"]) and gross:
        esi_wages = gross
        ded["esi"] = float(math.ceil(esi_wages * float(st["esi_employee_pct"]) / 100))
        er["esi"] = float(math.ceil(esi_wages * float(st["esi_employer_pct"]) / 100))
    if e.pt_applicable and gross:
        pt = pt_for(gross, st)
        if pt:
            ded["pt"] = pt
    if e.tds_monthly not in (None, ""):
        tds = float(e.tds_monthly or 0)
    else:
        tax, _ = annual_tax(rate_gross * 12 + (incentive_amt or 0) * 12, e.tax_regime or "new", st,
                            (ded.get("pf") or 0) * 12)
        tds = rupee(tax / 12)
    if tds:
        ded["tds"] = tds
    adv_lines = []
    room = gross - sum(ded.values())
    for a in advances or []:
        take = min(a["emi"] or a["balance"], a["balance"], max(room, 0))
        if take > 0:
            take = rupee(take)
            adv_lines.append({"id": a["id"], "number": a["number"], "amount": take})
            room -= take
    if adv_lines:
        ded["advance"] = r2(sum(x["amount"] for x in adv_lines))
    if float(adj.get("other_deduction") or 0):
        ded["other"] = rupee(adj["other_deduction"])
    total_ded = r2(sum(ded.values()))
    return {"earnings": earn, "deductions": ded, "employer": er, "gross": gross, "total_deductions": total_ded,
            "net": r2(gross - total_ded), "pf_wages": r2(pf_wages), "esi_wages": r2(esi_wages),
            "ot_hours": ot_hours, "advances": adv_lines, "factor": factor, "rate_gross": r2(rate_gross)}


def _snapshot(e):
    return {"name": e.name, "code": e.code, "designation": e.designation.name if e.designation else None,
            "department": e.department.name if e.department else None, "location": e.location,
            "uan": e.uan, "pf_no": e.pf_no, "esi_no": e.esi_no, "pan": e.pan, "bank_name": e.bank_name,
            "bank_account": e.bank_account, "ifsc": e.ifsc, "joined_on": e.joined_on,
            "rates": {k: float((e.salary or {}).get(k) or 0) for k in COMPONENT_KEYS}}


def compute_run(db, month, user=None):
    """Work out (or work out again) a month's payroll. Only a draft can be."""
    month = month_of(month)
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month).first()
    if run is None:
        run = M.HrPayrollRun(month=month, created_by=_user(user))
        db.add(run)
        db.flush()
    if run.status != "draft":
        raise ValueError(f"{month_label(month)} is {run.status} — reopen it to work it out again")
    st = settings(db)
    days = month_days(month)
    d0, d1 = days[0].isoformat(), days[-1].isoformat()
    emps = [e for e in _active_in(db, d0, d1) if gross_of(e.salary) > 0]
    ids = [e.id for e in emps]
    recs = {}
    for a in db.query(M.HrAttendance).filter(M.HrAttendance.employee_id.in_(ids or [0]),
                                             M.HrAttendance.date >= d0, M.HrAttendance.date <= d1).all():
        recs.setdefault(a.employee_id, {})[a.date] = a
    hol = _holidays(db, d0, d1)
    roster = _roster(db, ids, d0, d1)
    paid = {t.id: bool(t.paid) for t in db.query(M.HrLeaveType).all()}
    inc = {r["employee_id"]: r["incentive"] for r in incentives(db, d0, d1)["rows"]}
    old = {p.employee_id: p for p in db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all()}
    keep = set()
    for e in emps:
        att = counts(db, e, days, recs.get(e.id, {}), hol, roster, paid, st)
        advs = [a for a in (advance_out(db, x) for x in db.query(M.HrAdvance).filter(
            M.HrAdvance.employee_id == e.id, M.HrAdvance.status == "open").order_by(M.HrAdvance.id).all())
            if a["balance"] > 0 and (a["recover_from"] or "") <= month]
        slip = old.get(e.id) or M.HrPayslip(run_id=run.id, employee_id=e.id, adjustments={})
        r = compute_slip(db, e, month, st, att, inc.get(e.id, 0), slip.adjustments, advs)
        slip.days_in_month = len(days)
        slip.payable_days = att["payable"]
        slip.paid_days = r2(att["paid_days"])
        slip.lop_days = r2(att["lop"])
        slip.present_days = r2(att["present"])
        slip.leave_days = r2(att["paid_leave"])
        slip.ot_hours = r["ot_hours"]
        slip.earnings, slip.deductions, slip.employer = r["earnings"], r["deductions"], r["employer"]
        slip.gross, slip.total_deductions, slip.net = r["gross"], r["total_deductions"], r["net"]
        slip.pf_wages, slip.esi_wages = r["pf_wages"], r["esi_wages"]
        slip.payment_mode = e.payment_mode or "Bank"
        snap = _snapshot(e)
        snap["advances"] = r["advances"]
        snap["late_days"] = att["late_days"]
        snap["weekly_offs"] = att["wo"]
        snap["holidays"] = att["holiday"]
        snap["unmarked"] = att["unmarked"]
        slip.snapshot = snap
        if slip.id is None:
            db.add(slip)
        keep.add(e.id)
    for eid, slip in old.items():
        if eid not in keep:
            db.delete(slip)
    run.computed_at = dt.datetime.utcnow()
    db.flush()
    run.totals = run_totals(db, run)
    return run


def run_totals(db, run):
    slips = db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all()

    def s(fn):
        return r2(sum(fn(p) for p in slips))
    return {"employees": len(slips), "gross": s(lambda p: p.gross), "net": s(lambda p: p.net),
            "deductions": s(lambda p: p.total_deductions),
            "pf": s(lambda p: (p.deductions or {}).get("pf", 0)),
            "esi": s(lambda p: (p.deductions or {}).get("esi", 0)),
            "pt": s(lambda p: (p.deductions or {}).get("pt", 0)),
            "tds": s(lambda p: (p.deductions or {}).get("tds", 0)),
            "advance": s(lambda p: (p.deductions or {}).get("advance", 0)),
            "other": s(lambda p: (p.deductions or {}).get("other", 0)),
            "employer_pf": s(lambda p: (p.employer or {}).get("pf", 0) + (p.employer or {}).get("eps", 0)),
            "employer_esi": s(lambda p: (p.employer or {}).get("esi", 0)),
            "ot": s(lambda p: (p.earnings or {}).get("ot", 0)),
            "incentive": s(lambda p: (p.earnings or {}).get("incentive", 0)),
            "held": sum(1 for p in slips if (p.adjustments or {}).get("hold")),
            "cost": s(lambda p: p.gross + (p.employer or {}).get("pf", 0) + (p.employer or {}).get("eps", 0)
                      + (p.employer or {}).get("esi", 0))}


def slip_out(p, full=False):
    snap = p.snapshot or {}
    d = {"id": p.id, "employee_id": p.employee_id, "code": snap.get("code"), "name": snap.get("name"),
         "designation": snap.get("designation"), "department": snap.get("department"),
         "location": snap.get("location"), "days_in_month": p.days_in_month, "payable_days": p.payable_days,
         "paid_days": p.paid_days, "lop_days": p.lop_days, "present_days": p.present_days,
         "leave_days": p.leave_days, "ot_hours": p.ot_hours, "earnings": p.earnings or {},
         "deductions": p.deductions or {}, "employer": p.employer or {}, "gross": p.gross,
         "total_deductions": p.total_deductions, "net": p.net, "adjustments": p.adjustments or {},
         "payment_mode": p.payment_mode, "pf_wages": p.pf_wages, "esi_wages": p.esi_wages}
    if full:
        d["snapshot"] = snap
    return d


def run_out(db, run, slips=True):
    d = {"id": run.id, "month": run.month, "label": month_label(run.month), "status": run.status,
         "computed_at": run.computed_at.isoformat() if run.computed_at else None,
         "approved_by": run.approved_by, "approved_at": run.approved_at.isoformat() if run.approved_at else None,
         "paid_on": run.paid_on, "totals": run.totals or {}, "notes": run.notes,
         "journal": None, "payment": None}
    for k, vid in (("journal", run.journal_voucher_id), ("payment", run.payment_voucher_id)):
        v = db.get(M.Voucher, vid) if vid else None
        if v is not None:
            d[k] = {"id": v.id, "voucher_no": v.voucher_no, "date": v.date, "amount": v.amount, "status": v.status}
    if slips:
        d["slips"] = [slip_out(p) for p in sorted(
            db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all(),
            key=lambda p: (p.snapshot or {}).get("code") or "")]
    return d


def list_runs(db):
    return [run_out(db, r, slips=False) for r in db.query(M.HrPayrollRun).order_by(M.HrPayrollRun.month.desc()).all()]


def get_run(db, month):
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month_of(month)).first()
    return run_out(db, run) if run else None


def adjust_slip(db, sid, data, user=None):
    p = db.get(M.HrPayslip, sid)
    if p is None:
        raise LookupError("payslip not found")
    if p.run.status != "draft":
        raise ValueError("The run is approved — reopen it to change a payslip")
    adj = dict(p.adjustments or {})
    for k in ("bonus", "arrears", "other_deduction"):
        if k in data:
            adj[k] = r2(data.get(k))
    for k in ("hold", "remarks"):
        if k in data:
            adj[k] = data.get(k)
    p.adjustments = adj
    db.flush()
    compute_run(db, p.run.month, user)
    return slip_out(db.get(M.HrPayslip, sid))


def approve_run(db, month, user=None):
    """Lock the month and post its salaries to the books as one Journal."""
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month_of(month)).first()
    if run is None:
        raise LookupError("Work the month out first")
    if run.status != "draft":
        raise ValueError(f"{month_label(run.month)} is already {run.status}")
    compute_run(db, run.month, user)
    slips = db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all()
    if not slips:
        raise ValueError("Nobody is on this payroll — enter salaries on the employees first")
    t = run_totals(db, run)
    special = t["ot"] + t["incentive"]
    lines = [
        {"ledger_id": hr_ledger(db, "salaries").id, "debit": t["gross"] - special, "narration": "Salaries"},
        {"ledger_id": hr_ledger(db, "incentive").id, "debit": special, "narration": "Overtime & incentive"},
        {"ledger_id": hr_ledger(db, "employer_pf").id, "debit": t["employer_pf"]},
        {"ledger_id": hr_ledger(db, "employer_esi").id, "debit": t["employer_esi"]},
        {"ledger_id": hr_ledger(db, "salary_payable").id, "credit": t["net"], "bill_ref": f"PAYROLL:{run.month}"},
        {"ledger_id": hr_ledger(db, "pf_payable").id, "credit": t["pf"] + t["employer_pf"]},
        {"ledger_id": hr_ledger(db, "esi_payable").id, "credit": t["esi"] + t["employer_esi"]},
        {"ledger_id": finance.system_ledger(db, "tds").id, "credit": t["tds"], "narration": "TDS on salaries (192)"},
        {"ledger_id": hr_ledger(db, "pt_payable").id, "credit": t["pt"]},
        {"ledger_id": hr_ledger(db, "staff_advances").id, "credit": t["advance"]},
        {"ledger_id": hr_ledger(db, "recoveries").id, "credit": t["other"]},
    ]
    lines = [ln for ln in lines if r2(ln.get("debit")) or r2(ln.get("credit"))]
    last = month_days(run.month)[-1].isoformat()
    v = finance.save_voucher(db, {"vtype": "Journal", "date": last,
                                  "narration": f"Salaries for {month_label(run.month)} — {t['employees']} employees",
                                  "lines": lines}, user, source="payroll", source_id=run.id)
    run.journal_voucher_id = v.id
    for p in slips:
        for a in (p.snapshot or {}).get("advances") or []:
            db.add(M.HrAdvanceRecovery(advance_id=a["id"], month=run.month, date=last, amount=a["amount"],
                                       source="payroll", payslip_id=p.id))
    db.flush()
    for a in db.query(M.HrAdvance).filter(M.HrAdvance.status == "open").all():
        if advance_out(db, a)["balance"] <= 0.01:
            a.status = "closed"
    run.status = "approved"
    run.approved_by = _user(user)
    run.approved_at = dt.datetime.utcnow()
    run.totals = t
    db.flush()
    return run


def reopen_run(db, month, user=None):
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month_of(month)).first()
    if run is None:
        raise LookupError("no payroll for that month")
    if run.status == "paid":
        raise ValueError("Salaries are paid — cancel the payment voucher in Finance first, then reopen")
    if run.status != "approved":
        raise ValueError("It is still a draft")
    if run.journal_voucher_id:
        v = db.get(M.Voucher, run.journal_voucher_id)
        if v is not None:
            v.status = "cancelled"
        run.journal_voucher_id = None
    ids = [p.id for p in db.query(M.HrPayslip.id).filter(M.HrPayslip.run_id == run.id).all()]
    touched = {r.advance_id for r in db.query(M.HrAdvanceRecovery).filter(M.HrAdvanceRecovery.payslip_id.in_(ids or [0])).all()}
    db.query(M.HrAdvanceRecovery).filter(M.HrAdvanceRecovery.payslip_id.in_(ids or [0])).delete(synchronize_session=False)
    for aid in touched:
        a = db.get(M.HrAdvance, aid)
        if a is not None:
            a.status = "open"
    run.status = "draft"
    run.approved_by = run.approved_at = None
    db.flush()
    return run


def pay_run(db, month, data, user=None):
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month_of(month)).first()
    if run is None or run.status != "approved":
        raise ValueError("Approve the payroll before paying it")
    slips = [p for p in db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all()
             if not (p.adjustments or {}).get("hold") and p.net > 0]
    amount = r2(sum(p.net for p in slips))
    if not amount:
        raise ValueError("Nothing to pay")
    when = iso(data.get("date")) or today()
    mode = data.get("mode") or "NEFT"
    money = _money_ledger(db, "Cash" if mode == "Cash" else "Bank", data.get("ledger_id"))
    v = finance.save_voucher(db, {"vtype": "Payment", "date": when, "mode": mode, "ref_no": data.get("ref_no"),
                                  "narration": f"Salaries paid for {month_label(run.month)} — {len(slips)} employees",
                                  "lines": [{"ledger_id": hr_ledger(db, "salary_payable").id, "debit": amount,
                                             "bill_ref": f"PAYROLL:{run.month}"},
                                            {"ledger_id": money.id, "credit": amount}]},
                             user, source="payroll_payment", source_id=run.id)
    run.payment_voucher_id = v.id
    run.status = "paid"
    run.paid_on = when
    run.paid_by = _user(user)
    db.flush()
    return run


def bank_file(db, month):
    run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == month_of(month)).first()
    if run is None:
        raise LookupError("no payroll for that month")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Employee Code", "Name", "Bank", "Account No", "IFSC", "Amount", "Narration"])
    for p in sorted(db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all(),
                    key=lambda p: (p.snapshot or {}).get("code") or ""):
        snap = p.snapshot or {}
        if (p.adjustments or {}).get("hold") or p.payment_mode != "Bank" or p.net <= 0:
            continue
        w.writerow([snap.get("code"), snap.get("name"), snap.get("bank_name") or "", snap.get("bank_account") or "",
                    snap.get("ifsc") or "", f"{p.net:.2f}", f"Salary {month_label(run.month)}"])
    return out.getvalue()


def payslip(db, sid):
    p = db.get(M.HrPayslip, sid)
    if p is None:
        raise LookupError("payslip not found")
    d = slip_out(p, full=True)
    d["month"] = p.run.month
    d["label"] = month_label(p.run.month)
    d["status"] = p.run.status
    d["components"] = COMPONENTS
    d["ytd"] = _ytd(db, p)
    try:
        biz = businesses.default_business(db)
    except Exception:                                   # noqa: BLE001
        biz = None
    d["company"] = {"name": getattr(biz, "name", None) or "", "address": getattr(biz, "address", None) or ""}
    return d


def _ytd(db, p):
    """Gross, TDS and PF so far in the financial year (April onwards)."""
    m = p.run.month
    fy = int(m[:4]) if int(m[5:7]) >= 4 else int(m[:4]) - 1
    start = f"{fy}-04"
    slips = (db.query(M.HrPayslip).join(M.HrPayrollRun, M.HrPayrollRun.id == M.HrPayslip.run_id)
               .filter(M.HrPayslip.employee_id == p.employee_id, M.HrPayrollRun.month >= start,
                       M.HrPayrollRun.month <= m).all())
    return {"gross": r2(sum(x.gross for x in slips)), "tds": r2(sum((x.deductions or {}).get("tds", 0) for x in slips)),
            "pf": r2(sum((x.deductions or {}).get("pf", 0) for x in slips)), "months": len(slips)}


def employee_slips(db, eid):
    rows = (db.query(M.HrPayslip, M.HrPayrollRun).join(M.HrPayrollRun, M.HrPayrollRun.id == M.HrPayslip.run_id)
              .filter(M.HrPayslip.employee_id == eid).order_by(M.HrPayrollRun.month.desc()).all())
    return [{**slip_out(p), "month": r.month, "label": month_label(r.month), "status": r.status} for p, r in rows]


# ---------------------------------------------------------------------------
#  Performance
# ---------------------------------------------------------------------------
def list_cycles(db):
    out = []
    for c in db.query(M.HrReviewCycle).order_by(M.HrReviewCycle.id.desc()).all():
        d = _master_out(c)
        d["goals"] = db.query(func.count(M.HrGoal.id)).filter(M.HrGoal.cycle_id == c.id).scalar()
        d["appraisals"] = db.query(func.count(M.HrAppraisal.id)).filter(M.HrAppraisal.cycle_id == c.id).scalar()
        out.append(d)
    return out


def save_cycle(db, data, cid=None):
    c = db.get(M.HrReviewCycle, cid) if cid else M.HrReviewCycle()
    if cid and c is None:
        raise LookupError("cycle not found")
    c.name = (data.get("name") or "").strip()
    if not c.name:
        raise ValueError("Name the review cycle, like “FY 2026-27 Annual”")
    c.period_from = iso(data.get("period_from"), True, "start of the period")
    c.period_to = iso(data.get("period_to"), True, "end of the period")
    if c.period_to < c.period_from:
        raise ValueError("The period ends before it starts")
    c.status = data.get("status") or c.status or "open"
    if not cid:
        db.add(c)
    db.flush()
    return _master_out(c)


def _actual(db, g, e, c):
    if g.metric == "sales":
        return store_sales_of(db, e, c.period_from, c.period_to)[0]
    if g.metric == "bills":
        return float(store_sales_of(db, e, c.period_from, c.period_to)[1])
    if g.metric == "attendance":
        d0, d1 = c.period_from, min(c.period_to, today())
        if d1 < d0:
            return None
        days = []
        d = day(d0)
        while d <= day(d1):
            days.append(d)
            d += dt.timedelta(days=1)
        recs = {a.date: a for a in db.query(M.HrAttendance).filter(
            M.HrAttendance.employee_id == e.id, M.HrAttendance.date >= d0, M.HrAttendance.date <= d1).all()}
        cnt = counts(db, e, days, recs, _holidays(db, d0, d1), _roster(db, [e.id], d0, d1),
                     {t.id: bool(t.paid) for t in db.query(M.HrLeaveType).all()}, settings(db))
        working = cnt["payable"] - cnt["wo"] - cnt["holiday"]
        return r2(cnt["present"] / working * 100) if working else None
    return g.actual


def achievement(g):
    if g.target in (None, 0) or g.actual is None:
        return None
    return r2(min(float(g.actual) / float(g.target) * 100, 150))


def review(db, cycle_id, employee_id=None):
    c = db.get(M.HrReviewCycle, cycle_id)
    if c is None:
        raise LookupError("cycle not found")
    emps = {e.id: e for e in db.query(M.Employee).all()}
    goals = db.query(M.HrGoal).filter(M.HrGoal.cycle_id == c.id)
    if employee_id:
        goals = goals.filter(M.HrGoal.employee_id == int(employee_id))
    by = {}
    for g in goals.order_by(M.HrGoal.id).all():
        e = emps.get(g.employee_id)
        if e is None:
            continue
        if g.metric and g.metric != "manual" and c.status == "open":
            g.actual = _actual(db, g, e, c)
        by.setdefault(g.employee_id, []).append(g)
    apps = {a.employee_id: a for a in db.query(M.HrAppraisal).filter(M.HrAppraisal.cycle_id == c.id).all()}
    rows = []
    wanted = [emps[int(employee_id)]] if employee_id and int(employee_id) in emps else \
        [e for e in emps.values() if e.id in by or e.id in apps]
    for e in sorted(wanted, key=lambda x: x.code or ""):
        gl = by.get(e.id, [])
        score = goal_score(gl)
        a = apps.get(e.id)
        rows.append({**emp_brief(e), "goals": [{**_master_out(g), "achievement": achievement(g)} for g in gl],
                     "score": score, "suggested": rating_for(score),
                     "appraisal": _master_out(a) if a else None, "gross": gross_of(e.salary)})
    db.flush()
    return {"cycle": _master_out(c), "rows": rows}


def goal_score(goals):
    w = [(g.weight or 0, achievement(g), g.manager_rating) for g in goals]
    parts = [(wt, (ach if ach is not None else (mr / 5 * 100 if mr else None))) for wt, ach, mr in w]
    parts = [(wt, v) for wt, v in parts if v is not None]
    tw = sum(wt for wt, _ in parts)
    if not parts:
        return None
    if not tw:
        return r2(sum(v for _, v in parts) / len(parts))
    return r2(sum(wt * v for wt, v in parts) / tw)


def rating_for(score):
    if score is None:
        return None
    return 5 if score >= 120 else 4 if score >= 100 else 3 if score >= 80 else 2 if score >= 60 else 1


def save_goal(db, data, gid=None):
    g = db.get(M.HrGoal, gid) if gid else M.HrGoal()
    if gid and g is None:
        raise LookupError("goal not found")
    for k in ("cycle_id", "employee_id"):
        if not gid:
            setattr(g, k, int(data.get(k) or 0))
    if not db.get(M.HrReviewCycle, g.cycle_id) or not db.get(M.Employee, g.employee_id):
        raise ValueError("Pick the review cycle and the employee")
    g.title = (data.get("title") or "").strip()
    if not g.title:
        raise ValueError("Name the goal or KPI")
    g.kind = data.get("kind") or "KPI"
    g.metric = data.get("metric") or "manual"
    for k in ("weight", "target", "actual", "self_rating", "manager_rating"):
        if k in data:
            setattr(g, k, float(data[k]) if data.get(k) not in (None, "") else None)
    g.unit = data.get("unit")
    g.comments = data.get("comments")
    if not gid:
        db.add(g)
    db.flush()
    return _master_out(g)


def copy_goals(db, cycle_id, from_employee_id, to_employee_ids):
    src = db.query(M.HrGoal).filter(M.HrGoal.cycle_id == cycle_id, M.HrGoal.employee_id == from_employee_id).all()
    n = 0
    for eid in to_employee_ids or []:
        if int(eid) == int(from_employee_id):
            continue
        for g in src:
            db.add(M.HrGoal(cycle_id=cycle_id, employee_id=int(eid), title=g.title, kind=g.kind, metric=g.metric,
                            weight=g.weight, target=g.target, unit=g.unit))
            n += 1
    db.flush()
    return {"copied": n}


def save_appraisal(db, data, user=None):
    cid, eid = int(data.get("cycle_id") or 0), int(data.get("employee_id") or 0)
    if not db.get(M.HrReviewCycle, cid) or not db.get(M.Employee, eid):
        raise ValueError("Pick the review cycle and the employee")
    a = db.query(M.HrAppraisal).filter(M.HrAppraisal.cycle_id == cid, M.HrAppraisal.employee_id == eid).first()
    if a is None:
        a = M.HrAppraisal(cycle_id=cid, employee_id=eid)
        db.add(a)
    if a.status == "applied":
        raise ValueError("This appraisal is already applied")
    for k in ("rating", "score", "increment_pct"):
        if k in data:
            setattr(a, k, float(data[k]) if data.get(k) not in (None, "") else None)
    for k in ("strengths", "improvements", "comments"):
        if k in data:
            setattr(a, k, data.get(k))
    if "new_designation_id" in data:
        a.new_designation_id = int(data["new_designation_id"]) if data.get("new_designation_id") else None
    a.effective_on = iso(data.get("effective_on")) or a.effective_on
    if data.get("status") in ("draft", "submitted", "approved"):
        a.status = data["status"]
    a.reviewed_by = _user(user)
    db.flush()
    return _master_out(a)


def apply_appraisal(db, aid, user=None):
    """Make the appraisal real: the increment on every pay component and the new
    designation, both on the role history."""
    a = db.get(M.HrAppraisal, aid)
    if a is None:
        raise LookupError("appraisal not found")
    if a.status != "approved":
        raise ValueError("Approve the appraisal first")
    e = db.get(M.Employee, a.employee_id)
    data = {"effective_on": a.effective_on or today()}
    if a.increment_pct:
        f = 1 + float(a.increment_pct) / 100
        data["salary"] = {k: rupee(float((e.salary or {}).get(k) or 0) * f) for k in COMPONENT_KEYS}
    if a.new_designation_id and a.new_designation_id != e.designation_id:
        data["designation_id"] = a.new_designation_id
        d = db.get(M.HrDesignation, a.new_designation_id)
        if d and d.department_id:
            data["department_id"] = d.department_id
    save_employee(db, data, user, e.id)
    cyc = db.get(M.HrReviewCycle, a.cycle_id)
    db.add(M.EmployeeHistory(employee_id=e.id, effective_on=data["effective_on"], kind="Appraisal",
                             detail=f"{cyc.name if cyc else 'Review'}: rated {a.rating or '—'}/5"
                             + (f", increment {a.increment_pct:g}%" if a.increment_pct else ""),
                             department=e.department.name if e.department else None,
                             designation=e.designation.name if e.designation else None,
                             location=e.location, gross=gross_of(e.salary), created_by=_user(user)))
    a.status = "applied"
    a.applied_at = dt.datetime.utcnow()
    db.flush()
    return _master_out(a)


# ---------------------------------------------------------------------------
#  The dashboard
# ---------------------------------------------------------------------------
def _next_occurrence(s, ref):
    try:
        d = dt.date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None
    try:
        nxt = d.replace(year=ref.year)
    except ValueError:                                  # 29 February
        nxt = dt.date(ref.year, 3, 1)
    if nxt < ref:
        try:
            nxt = d.replace(year=ref.year + 1)
        except ValueError:
            nxt = dt.date(ref.year + 1, 3, 1)
    return nxt


def dashboard(db):
    settle_exits(db)
    t = business_day.today()
    ts = t.isoformat()
    month = ts[:7]
    emps = db.query(M.Employee).all()
    current = [e for e in emps if e.status != "exited"]
    by_dep, by_loc, by_type = {}, {}, {}
    for e in current:
        by_dep[e.department.name if e.department else "—"] = by_dep.get(e.department.name if e.department else "—", 0) + 1
        by_loc[e.location or "—"] = by_loc.get(e.location or "—", 0) + 1
        by_type[e.employment_type or "—"] = by_type.get(e.employment_type or "—", 0) + 1
    # today
    recs = {a.employee_id: a for a in db.query(M.HrAttendance).filter(M.HrAttendance.date == ts).all()}
    hol = _holidays(db, ts, ts)
    ros = _roster(db, [e.id for e in current], ts, ts)
    today_c = {"P": 0, "HD": 0, "A": 0, "L": 0, "WO": 0, "H": 0, "OD": 0, "": 0}
    late, on_leave = [], []
    for e in current:
        if not _employed_on(e, t):
            continue
        v = day_view(db, e, t, recs.get(e.id), hol, ros)
        today_c[v["s"]] = today_c.get(v["s"], 0) + 1
        if v.get("late"):
            late.append({"name": e.name, "minutes": v["late"], "in": v.get("in")})
        if v["s"] == "L":
            on_leave.append({"name": e.name, "type": (v.get("rm") or "").split(" · ")[0]})
    # people events
    soon = t + dt.timedelta(days=30)
    events, probation_over = [], []
    for e in current:
        for kind, s in (("Birthday", e.dob), ("Work anniversary", e.joined_on), ("Wedding anniversary", e.anniversary)):
            if not s:
                continue
            nxt = _next_occurrence(s, t)
            if nxt and nxt <= soon and not (kind == "Work anniversary" and s[:4] == str(nxt.year)):
                years = nxt.year - int(s[:4])
                events.append({"date": nxt.isoformat(), "kind": kind, "name": e.name, "id": e.id,
                               "years": years if kind != "Birthday" else None})
        if e.employment_type == "Probation" and e.joined_on and not e.confirmed_on:
            j = day(e.joined_on)
            m = (e.probation_months or 6)
            end = dt.date(j.year + (j.month - 1 + m) // 12, (j.month - 1 + m) % 12 + 1, min(j.day, 28))
            if end < t:
                probation_over.append({"date": end.isoformat(), "name": e.name, "id": e.id})
            elif end <= soon:
                events.append({"date": end.isoformat(), "kind": "Probation ends", "name": e.name, "id": e.id})
    events.sort(key=lambda x: x["date"])
    docs = [{**doc_out(x), "employee": next((e.name for e in emps if e.id == x.employee_id), None)}
            for x in db.query(M.EmployeeDocument).filter(M.EmployeeDocument.expires_on.isnot(None),
                                                         M.EmployeeDocument.expires_on <= soon.isoformat()).all()]
    contracts = [{**_master_out(c), "employee": next((e.name for e in emps if e.id == c.employee_id), None)}
                 for c in db.query(M.EmployeeContract).filter(M.EmployeeContract.status == "active",
                                                              M.EmployeeContract.end_date.isnot(None),
                                                              M.EmployeeContract.end_date <= soon.isoformat()).all()]
    # movement
    m0 = month + "-01"
    joiners = [emp_brief(e) for e in emps if e.joined_on and e.joined_on >= m0]
    exits = [emp_brief(e) for e in emps if e.last_working_day and e.last_working_day >= m0
             and e.status in ("exited", "notice")]
    year_ago = (t - dt.timedelta(days=365)).isoformat()
    left_year = sum(1 for e in emps if e.status == "exited" and (e.last_working_day or "") >= year_ago)
    avg_head = (len(current) + left_year / 2) or 1
    # payroll
    runs = db.query(M.HrPayrollRun).order_by(M.HrPayrollRun.month.desc()).limit(6).all()
    trend = [{"month": r.month, "label": month_label(r.month)[:3] + " " + r.month[2:4], "status": r.status,
              "gross": (r.totals or {}).get("gross", 0), "net": (r.totals or {}).get("net", 0),
              "cost": (r.totals or {}).get("cost", 0), "employees": (r.totals or {}).get("employees", 0)}
             for r in reversed(runs)]
    this_run = next((x for x in trend if x["month"] == month), None)
    monthly_bill = r2(sum(gross_of(e.salary) for e in current))
    adv_open = [advance_out(db, a) for a in db.query(M.HrAdvance).filter(M.HrAdvance.status == "open").all()]
    pending_leave = db.query(func.count(M.HrLeaveRequest.id)).filter(M.HrLeaveRequest.status == "pending").scalar()
    pending_ot = db.query(func.count(M.HrAttendance.id)).filter(M.HrAttendance.ot_minutes > 0,
                                                                (M.HrAttendance.ot_status == "pending")
                                                                | M.HrAttendance.ot_status.is_(None)).scalar()
    return {
        "today": ts, "month": month, "month_label": month_label(month),
        "headcount": {"current": len(current), "active": sum(1 for e in current if e.status == "active"),
                      "notice": sum(1 for e in current if e.status == "notice"),
                      "exited": sum(1 for e in emps if e.status == "exited")},
        "by_department": sorted(([k, v] for k, v in by_dep.items()), key=lambda x: -x[1]),
        "by_location": sorted(([k, v] for k, v in by_loc.items()), key=lambda x: -x[1]),
        "by_type": sorted(([k, v] for k, v in by_type.items()), key=lambda x: -x[1]),
        "attendance": today_c, "late": sorted(late, key=lambda x: -x["minutes"])[:10], "on_leave": on_leave,
        "pending_leave": pending_leave, "pending_ot": pending_ot,
        "events": events[:15], "documents": docs, "contracts": contracts, "probation_over": probation_over,
        "joiners": joiners, "exits": exits, "attrition_pct": r2(left_year / avg_head * 100),
        "payroll": {"trend": trend, "this_month": this_run, "monthly_bill": monthly_bill},
        "advances": {"count": len(adv_open), "balance": r2(sum(a["balance"] for a in adv_open))},
        "unlinked_store": sum(1 for e in current if not e.store_username),
    }


# ---------------------------------------------------------------------------
#  Options for the screens' pickers
# ---------------------------------------------------------------------------
def options(db):
    ensure_defaults(db)
    locs = sorted({w.name for w in db.query(M.Warehouse).all()} | {s.name for s in db.query(M.Store).all()})
    banks = [{"id": L.id, "name": L.name, "group": L.group} for L in db.query(M.Ledger).filter(
        M.Ledger.group.in_(("Bank Account", "Cash-In-Hand")), M.Ledger.active.isnot(False)).order_by(M.Ledger.name).all()]
    return {
        "departments": list_master(db, "departments"), "designations": list_master(db, "designations"),
        "shifts": list_master(db, "shifts"), "leave_types": list_master(db, "leave_types"),
        "employees": [emp_brief(e) for e in db.query(M.Employee).filter(M.Employee.status != "exited")
                      .order_by(M.Employee.name).all()],
        "locations": locs, "banks": banks, "components": COMPONENTS, "employment_types": EMPLOYMENT_TYPES,
        "exit_types": EXIT_TYPES, "doc_types": DOC_TYPES, "doc_groups": DOC_GROUPS, "contract_types": CONTRACT_TYPES,
        "history_kinds": HISTORY_KINDS, "weekdays": WEEKDAYS, "statuses": STATUSES, "genders": GENDERS,
        "pay_modes": PAY_MODES, "incentive_bases": INCENTIVE_BASES, "goal_metrics": GOAL_METRICS,
        "store": pos_sales.available(),
    }


# ---------------------------------------------------------------------------
#  Reports
# ---------------------------------------------------------------------------
REPORTS = [
    # key, label, group, params
    ("register", "Employee Register", "People", ["status", "department_id", "location"]),
    ("joiners_exits", "Joiners & Exits", "People", ["date_from", "date_to"]),
    ("role_history", "Role History", "People", ["date_from", "date_to"]),
    ("birthdays", "Birthdays & Anniversaries", "People", ["month"]),
    ("documents_expiry", "Document Expiry", "People", ["days"]),
    ("contracts_expiry", "Contract Expiry", "People", ["days"]),
    ("attendance_summary", "Attendance Summary", "Time", ["month", "department_id", "location"]),
    ("late_report", "Late Arrivals", "Time", ["date_from", "date_to"]),
    ("overtime", "Overtime Register", "Time", ["date_from", "date_to"]),
    ("leave_balance", "Leave Balances", "Leave", ["as_on"]),
    ("leave_register", "Leave Register", "Leave", ["date_from", "date_to"]),
    ("payroll_register", "Payroll Register", "Payroll", ["month"]),
    ("pf_statement", "PF Statement (ECR)", "Payroll", ["month"]),
    ("esi_statement", "ESI Statement", "Payroll", ["month"]),
    ("pt_statement", "Professional Tax", "Payroll", ["month"]),
    ("tds_statement", "TDS on Salaries", "Payroll", ["month"]),
    ("bank_transfer", "Salary Bank Transfer", "Payroll", ["month"]),
    ("cost_by_department", "Salary Cost by Department", "Payroll", ["month"]),
    ("incentive_report", "Sales Incentive", "Payroll", ["date_from", "date_to"]),
    ("advances", "Advances Outstanding", "Payroll", []),
]
REPORT_KEYS = [k for k, *_ in REPORTS]


def report(db, key, p):
    if key not in REPORT_KEYS:
        raise LookupError("unknown report")
    p = {k: v for k, v in (p or {}).items() if v not in (None, "")}
    t = business_day.today()
    d0 = iso(p.get("date_from")) or t.replace(day=1).isoformat()
    d1 = iso(p.get("date_to")) or t.isoformat()
    month = month_of(p.get("month"))
    label = dict((k, lbl) for k, lbl, *_ in REPORTS)[key]

    def out(cols, rows, sub="", totals=None):
        return {"key": key, "title": label, "sub": sub, "columns": cols, "rows": rows, "totals": totals or {}}

    def slips_of(m):
        run = db.query(M.HrPayrollRun).filter(M.HrPayrollRun.month == m).first()
        if run is None:
            return None, []
        return run, sorted(db.query(M.HrPayslip).filter(M.HrPayslip.run_id == run.id).all(),
                           key=lambda x: (x.snapshot or {}).get("code") or "")

    if key == "register":
        rows = list_employees(db, status=p.get("status") or "current", department_id=p.get("department_id"),
                              location=p.get("location"))
        return out(["Code", "Name", "Designation", "Department", "Location", "Type", "Joined", "Mobile",
                    "Gross", "Status"],
                   [{"Code": r["code"], "Name": r["name"], "Designation": r["designation"],
                     "Department": r["department"], "Location": r["location"], "Type": r["employment_type"],
                     "Joined": r["joined_on"], "Mobile": r["phone"], "Gross": r["gross"], "Status": r["status"]}
                    for r in rows], f"{len(rows)} employees", {"Gross": r2(sum(r["gross"] for r in rows))})
    if key == "joiners_exits":
        rows = []
        for e in db.query(M.Employee).all():
            if e.joined_on and d0 <= e.joined_on <= d1:
                rows.append({"Date": e.joined_on, "Movement": "Joined", "Code": e.code, "Name": e.name,
                             "Designation": e.designation.name if e.designation else None,
                             "Location": e.location, "Reason": ""})
            if e.last_working_day and d0 <= e.last_working_day <= d1:
                rows.append({"Date": e.last_working_day, "Movement": e.exit_type or "Exit", "Code": e.code,
                             "Name": e.name, "Designation": e.designation.name if e.designation else None,
                             "Location": e.location, "Reason": e.exit_reason or ""})
        rows.sort(key=lambda r: r["Date"])
        return out(["Date", "Movement", "Code", "Name", "Designation", "Location", "Reason"], rows,
                   f"{sum(1 for r in rows if r['Movement'] == 'Joined')} joined, "
                   f"{sum(1 for r in rows if r['Movement'] != 'Joined')} left")
    if key == "role_history":
        emps = {e.id: e for e in db.query(M.Employee).all()}
        rows = [{"Date": h.effective_on, "Code": emps[h.employee_id].code if h.employee_id in emps else "",
                 "Name": emps[h.employee_id].name if h.employee_id in emps else "", "Change": h.kind,
                 "Detail": h.detail, "Designation": h.designation, "Gross": h.gross, "By": h.created_by}
                for h in db.query(M.EmployeeHistory).filter(M.EmployeeHistory.effective_on >= d0,
                                                            M.EmployeeHistory.effective_on <= d1)
                .order_by(M.EmployeeHistory.effective_on.desc()).all()]
        return out(["Date", "Code", "Name", "Change", "Detail", "Designation", "Gross", "By"], rows)
    if key == "birthdays":
        mm = month[5:7]
        rows = []
        for e in db.query(M.Employee).filter(M.Employee.status != "exited").all():
            for kind, s in (("Birthday", e.dob), ("Work anniversary", e.joined_on),
                            ("Wedding anniversary", e.anniversary)):
                if s and s[5:7] == mm and not (kind == "Work anniversary" and s[:4] == month[:4]):
                    rows.append({"Day": int(s[8:10]), "Occasion": kind, "Code": e.code, "Name": e.name,
                                 "Years": int(month[:4]) - int(s[:4]) if kind != "Birthday" else "",
                                 "Mobile": e.phone, "Location": e.location})
        rows.sort(key=lambda r: r["Day"])
        return out(["Day", "Occasion", "Code", "Name", "Years", "Mobile", "Location"], rows, month_label(month))
    if key in ("documents_expiry", "contracts_expiry"):
        n = int(p.get("days") or 60)
        lim = (t + dt.timedelta(days=n)).isoformat()
        emps = {e.id: e for e in db.query(M.Employee).all()}
        if key == "documents_expiry":
            rows = [{"Expires": x.expires_on, "Code": emps[x.employee_id].code, "Name": emps[x.employee_id].name,
                     "Document": x.doc_type, "Number": x.number, "State": "Expired" if x.expires_on < t.isoformat() else "Due"}
                    for x in db.query(M.EmployeeDocument).filter(M.EmployeeDocument.expires_on.isnot(None),
                                                                 M.EmployeeDocument.expires_on <= lim)
                    .order_by(M.EmployeeDocument.expires_on).all() if x.employee_id in emps]
            return out(["Expires", "Code", "Name", "Document", "Number", "State"], rows, f"within {n} days")
        rows = [{"Ends": c.end_date, "Code": emps[c.employee_id].code, "Name": emps[c.employee_id].name,
                 "Contract": c.contract_type, "Started": c.start_date, "CTC": c.ctc_annual, "Notice days": c.notice_days}
                for c in db.query(M.EmployeeContract).filter(M.EmployeeContract.status == "active",
                                                             M.EmployeeContract.end_date.isnot(None),
                                                             M.EmployeeContract.end_date <= lim)
                .order_by(M.EmployeeContract.end_date).all() if c.employee_id in emps]
        return out(["Ends", "Code", "Name", "Contract", "Started", "CTC", "Notice days"], rows, f"within {n} days")
    if key == "attendance_summary":
        reg = month_register(db, month, p.get("department_id"), p.get("location"))
        rows = [{"Code": r["code"], "Name": r["name"], "Department": r["department"],
                 "Present": r["totals"]["present"], "Paid leave": r["totals"]["paid_leave"],
                 "Absent": r["totals"]["absent"], "LOP": r["totals"]["lop"], "Weekly off": r["totals"]["wo"],
                 "Holidays": r["totals"]["holiday"], "Unmarked": r["totals"]["unmarked"],
                 "Late days": r["totals"]["late_days"], "OT hours": r2(r["totals"]["ot_approved"] / 60),
                 "Paid days": r["totals"]["paid_days"]} for r in reg["rows"]]
        return out(["Code", "Name", "Department", "Present", "Paid leave", "Absent", "LOP", "Weekly off",
                    "Holidays", "Unmarked", "Late days", "OT hours", "Paid days"], rows, reg["label"])
    if key == "late_report":
        emps = {e.id: e for e in db.query(M.Employee).all()}
        rows = [{"Date": a.date, "Code": emps[a.employee_id].code, "Name": emps[a.employee_id].name,
                 "Shift": emps[a.employee_id].shift.name if emps[a.employee_id].shift else "",
                 "In": a.in_time, "Late (min)": a.late_minutes}
                for a in db.query(M.HrAttendance).filter(M.HrAttendance.date >= d0, M.HrAttendance.date <= d1,
                                                         M.HrAttendance.late_minutes > 0)
                .order_by(M.HrAttendance.date.desc()).all() if a.employee_id in emps]
        return out(["Date", "Code", "Name", "Shift", "In", "Late (min)"], rows, f"{len(rows)} late arrivals")
    if key == "overtime":
        rows = [{"Date": r["date"], "Code": r["code"], "Name": r["name"], "In": r["in_time"], "Out": r["out_time"],
                 "OT (min)": r["ot_minutes"], "Approved (min)": r["approved_minutes"], "Status": r["status"],
                 "Amount": r["amount"]} for r in overtime(db, d0, d1)]
        return out(["Date", "Code", "Name", "In", "Out", "OT (min)", "Approved (min)", "Status", "Amount"], rows,
                   totals={"Amount": r2(sum(r["Amount"] for r in rows if r["Status"] == "approved"))})
    if key == "leave_balance":
        types = [t_ for t_ in db.query(M.HrLeaveType).filter(M.HrLeaveType.active.isnot(False)).all()]
        rows = []
        for e in db.query(M.Employee).filter(M.Employee.status != "exited").order_by(M.Employee.code).all():
            bal = {b["code"]: b["balance"] for b in leave_balances(db, e.id, p.get("as_on"))}
            rows.append({"Code": e.code, "Name": e.name, **{t_.code: bal.get(t_.code, 0) for t_ in types}})
        return out(["Code", "Name"] + [t_.code for t_ in types], rows, "as on " + (iso(p.get("as_on")) or "today"))
    if key == "leave_register":
        rows = [{"No": r["number"], "Code": r["code"], "Name": r["employee"], "Type": r["type"], "From": r["from_date"],
                 "To": r["to_date"], "Days": r["days"], "Status": r["status"], "Reason": r["reason"],
                 "Decided by": r["decided_by"]} for r in list_leave(db, date_from=d0, date_to=d1)]
        return out(["No", "Code", "Name", "Type", "From", "To", "Days", "Status", "Reason", "Decided by"], rows)
    if key == "incentive_report":
        r = incentives(db, d0, d1)
        names = {s["id"]: s["name"] for s in r["schemes"]}
        cols = ["Code", "Name", "Designation", "Bills", "Qty", "Sales"] + [names[k] for k in names] + ["Incentive"]
        rows = [{"Code": x["code"], "Name": x["name"], "Designation": x["designation"], "Bills": x["bills"],
                 "Qty": x["qty"], "Sales": x["sales"], **{names[k]: x["by_scheme"].get(k, 0) for k in names},
                 "Incentive": x["incentive"]} for x in r["rows"]]
        return out(cols, rows, "" if r["available"] else "The Store cannot be read from here",
                   {"Sales": r.get("totals", {}).get("sales", 0), "Incentive": r.get("totals", {}).get("incentive", 0)})
    if key == "advances":
        rows = [{"No": a["number"], "Code": a["code"], "Name": a["employee"], "Kind": a["kind"], "Given": a["issued_on"],
                 "Amount": a["amount"], "EMI": a["emi"], "Recovered": a["recovered"], "Balance": a["balance"]}
                for a in list_advances(db, status="open")]
        return out(["No", "Code", "Name", "Kind", "Given", "Amount", "EMI", "Recovered", "Balance"], rows,
                   totals={"Balance": r2(sum(r["Balance"] for r in rows))})
    run, slips = slips_of(month)
    sub = month_label(month) + (f" · {run.status}" if run else " · not worked out yet")
    if key == "payroll_register":
        comps = [c for c, _ in COMPONENTS] + ["ot", "incentive", "bonus", "arrears"]
        heads = dict(COMPONENTS, ot="OT", incentive="Incentive", bonus="Bonus", arrears="Arrears")
        used = [c for c in comps if any((s.earnings or {}).get(c) for s in slips)]
        dk = [("pf", "PF"), ("esi", "ESI"), ("pt", "PT"), ("tds", "TDS"), ("advance", "Advance"), ("other", "Other")]
        dused = [(k, h) for k, h in dk if any((s.deductions or {}).get(k) for s in slips)]
        cols = ["Code", "Name", "Paid days", "LOP"] + [heads[c] for c in used] + ["Gross"] + [h for _, h in dused] + \
            ["Deductions", "Net"]
        rows = []
        for s in slips:
            snap = s.snapshot or {}
            rows.append({"Code": snap.get("code"), "Name": snap.get("name"), "Paid days": s.paid_days, "LOP": s.lop_days,
                         **{heads[c]: (s.earnings or {}).get(c, 0) for c in used}, "Gross": s.gross,
                         **{h: (s.deductions or {}).get(k, 0) for k, h in dused},
                         "Deductions": s.total_deductions, "Net": s.net})
        tot = {c: r2(sum(r.get(c) or 0 for r in rows)) for c in cols[4:]}
        return out(cols, rows, sub, tot)
    if key == "pf_statement":
        rows = []
        for s in slips:
            if not (s.deductions or {}).get("pf"):
                continue
            snap = s.snapshot or {}
            er = s.employer or {}
            rows.append({"UAN": snap.get("uan") or "", "Name": snap.get("name"), "Gross wages": s.gross,
                         "EPF wages": s.pf_wages, "EPS wages": min(s.pf_wages, float(settings(db)["pf_wage_ceiling"])),
                         "EDLI wages": min(s.pf_wages, float(settings(db)["pf_wage_ceiling"])),
                         "EE share": s.deductions["pf"], "ER EPS": er.get("eps", 0), "ER EPF": er.get("pf", 0),
                         "NCP days": s.lop_days or 0})
        cols = ["UAN", "Name", "Gross wages", "EPF wages", "EPS wages", "EDLI wages", "EE share", "ER EPS", "ER EPF", "NCP days"]
        return out(cols, rows, sub, {c: r2(sum(r[c] for r in rows)) for c in cols[2:9]})
    if key == "esi_statement":
        rows = []
        for s in slips:
            if not (s.deductions or {}).get("esi"):
                continue
            snap = s.snapshot or {}
            rows.append({"IP number": snap.get("esi_no") or "", "Name": snap.get("name"), "Days paid": s.paid_days,
                         "Wages": s.esi_wages, "Employee": s.deductions["esi"], "Employer": (s.employer or {}).get("esi", 0)})
        cols = ["IP number", "Name", "Days paid", "Wages", "Employee", "Employer"]
        return out(cols, rows, sub, {c: r2(sum(r[c] for r in rows)) for c in cols[3:]})
    if key == "pt_statement":
        rows = [{"Code": (s.snapshot or {}).get("code"), "Name": (s.snapshot or {}).get("name"), "Gross": s.gross,
                 "PT": (s.deductions or {}).get("pt", 0)} for s in slips if (s.deductions or {}).get("pt")]
        return out(["Code", "Name", "Gross", "PT"], rows, sub + f" · {settings(db).get('pt_state')}",
                   {"Gross": r2(sum(r["Gross"] for r in rows)), "PT": r2(sum(r["PT"] for r in rows))})
    if key == "tds_statement":
        rows = [{"Code": (s.snapshot or {}).get("code"), "Name": (s.snapshot or {}).get("name"),
                 "PAN": (s.snapshot or {}).get("pan") or "", "Gross": s.gross, "TDS": (s.deductions or {}).get("tds", 0)}
                for s in slips if (s.deductions or {}).get("tds")]
        return out(["Code", "Name", "PAN", "Gross", "TDS"], rows, sub + " · section 192",
                   {"Gross": r2(sum(r["Gross"] for r in rows)), "TDS": r2(sum(r["TDS"] for r in rows))})
    if key == "bank_transfer":
        rows = [{"Code": (s.snapshot or {}).get("code"), "Name": (s.snapshot or {}).get("name"),
                 "Mode": s.payment_mode, "Bank": (s.snapshot or {}).get("bank_name") or "",
                 "Account": (s.snapshot or {}).get("bank_account") or "", "IFSC": (s.snapshot or {}).get("ifsc") or "",
                 "Net": s.net, "Held": "Yes" if (s.adjustments or {}).get("hold") else ""} for s in slips]
        return out(["Code", "Name", "Mode", "Bank", "Account", "IFSC", "Net", "Held"], rows, sub,
                   {"Net": r2(sum(r["Net"] for r in rows if not r["Held"]))})
    if key == "cost_by_department":
        agg = {}
        for s in slips:
            dname = (s.snapshot or {}).get("department") or "—"
            a = agg.setdefault(dname, {"Department": dname, "Employees": 0, "Gross": 0.0, "Employer PF": 0.0,
                                       "Employer ESI": 0.0, "Cost": 0.0})
            er = s.employer or {}
            a["Employees"] += 1
            a["Gross"] = r2(a["Gross"] + s.gross)
            a["Employer PF"] = r2(a["Employer PF"] + er.get("pf", 0) + er.get("eps", 0))
            a["Employer ESI"] = r2(a["Employer ESI"] + er.get("esi", 0))
            a["Cost"] = r2(a["Gross"] + a["Employer PF"] + a["Employer ESI"])
        rows = sorted(agg.values(), key=lambda r: -r["Cost"])
        cols = ["Department", "Employees", "Gross", "Employer PF", "Employer ESI", "Cost"]
        return out(cols, rows, sub, {c: r2(sum(r[c] for r in rows)) for c in cols[1:]})
    raise LookupError("unknown report")


# ---------------------------------------------------------------------------
#  For the Command Center's question box (services/ask_anything)
# ---------------------------------------------------------------------------
def day_summary(db, date_s=None):
    """Who was in, late, on leave, absent or not marked on one day."""
    d = day(iso(date_s) or today())
    ds = d.isoformat()
    emps = _active_in(db, ds, ds)
    recs = {a.employee_id: a for a in db.query(M.HrAttendance).filter(M.HrAttendance.date == ds).all()}
    hol = _holidays(db, ds, ds)
    ros = _roster(db, [e.id for e in emps], ds, ds)
    rows = []
    for e in emps:
        if not _employed_on(e, d):
            continue
        v = day_view(db, e, d, recs.get(e.id), hol, ros)
        rows.append({"Employee": e.name, "Code": e.code, "Department": e.department.name if e.department else "",
                     "Status": STATUSES.get(v["s"], "Not marked"), "In": v.get("in") or "", "Out": v.get("out_t") or "",
                     "Late (min)": v.get("late") or "", "_s": v["s"]})
    c = {}
    for r in rows:
        c[r["_s"]] = c.get(r["_s"], 0) + 1
    return {"date": ds, "rows": rows, "counts": c,
            "late": [r for r in rows if r["Late (min)"]], "total": len(rows)}


def find_employees(db, text):
    """Employees a name or code means — every word starting a word of the name."""
    want = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not want:
        return []
    out, exact = [], []
    for e in db.query(M.Employee).all():
        words = re.findall(r"[a-z0-9]+", f"{e.name} {e.code} {e.store_username or ''}".lower())
        if all(any(w.startswith(x) for w in words) for x in want):
            out.append(e)
            if " ".join(want) in (e.name.lower(), (e.code or "").lower()):
                exact.append(e)
    return exact or out
