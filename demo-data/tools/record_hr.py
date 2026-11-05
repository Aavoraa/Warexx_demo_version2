"""Record the HR module's answers for the demo.

    <essa>/backend/.venv/Scripts/python.exe demo-data/tools/record_hr.py --essa D:/ESSA-Next

HR came from ESSA (frontend/vendor/warexx-app/src/HR.jsx is its HR.jsx). This runs
ESSA's real HR backend IN-PROCESS on two throwaway SQLite files in a temp folder
(the warehouse's and a till's), fills them with a FICTIONAL company's people —
three months of attendance, leave, advances, incentives, payroll and a review
cycle — then asks every question the HR screens ask and writes the answers into
demo-data/recorded/manifest.json (replacing any /api/hr entries already there).

It never opens a real database: every database variable is removed before ESSA
is imported, and the run refuses to start unless its database is in the temp
folder it made. Nobody in it is a real person.
"""
import argparse
import calendar
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import tempfile
from urllib.parse import urlencode

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE.parent / "recorded" / "manifest.json"

ap = argparse.ArgumentParser()
ap.add_argument("--essa", required=True, help="the ESSA-Next folder (has backend/ and 'Textile Retail Shop/')")
ap.add_argument("--dry", action="store_true", help="seed and check, but do not write the manifest")
args = ap.parse_args()
ROOT = pathlib.Path(args.essa).resolve()
SHOP = ROOT / "Textile Retail Shop"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

tmp = tempfile.mkdtemp(prefix="warexx-hr-demo-")
WH_DB = os.path.join(tmp, "warehouse.db").replace(os.sep, "/")
SHOP_DB = os.path.join(tmp, "shop.db").replace(os.sep, "/")
for leak in ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL",
             "SHOP_DB_SCHEMA", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ESSA_SUPERBOSS_PASSWORD", "BLOB_READ_WRITE_TOKEN"):
    os.environ.pop(leak, None)
os.environ["ESSA_STATE_DIR"] = tmp
# the fictional company every slip and statement is issued by (never ESSA's own)
os.environ["ESSA_COMPANY_NAME"] = "Sample Business Private Limited"
os.environ["ESSA_COMPANY_GSTIN"] = "33AAAAA0000A1Z5"
os.environ["ESSA_POS_DB"] = SHOP_DB
os.environ["ESSA_WAREHOUSE_DB"] = os.path.join(tmp, "no-warehouse-here.db")

OFFSET = dt.timedelta(minutes=330)
TODAY = (dt.datetime.utcnow() + OFFSET).date()
random.seed(20261005)


def month_first(d, back=0):
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


def ym(d):
    return d.strftime("%Y-%m")


def month_last(first):
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


M0 = month_first(TODAY)            # this month (in progress)
M1 = month_first(TODAY, 1)         # last month — the payroll screens open on it
M2 = month_first(TODAY, 2)
M3 = month_first(TODAY, 3)
START = M3                         # three full months of history, and this one so far


def utc(d, h, m=0):
    return dt.datetime.combine(d, dt.time(h, m)) - OFFSET


# ===========================================================================
# the till: the store staff who sell, and what they sold (for incentives/KPIs)
os.environ["DATABASE_URL"] = "sqlite:///" + SHOP_DB
sys.path.insert(0, str(SHOP))
import app as shop_pkg                                              # noqa: E402
from app import db as shop_db                                       # noqa: E402
from app.models import (Attendance as SAttendance, Category, Company, Invoice,  # noqa: E402
                        InvoiceItem, Location, Product, User as SUser)

shop_app = shop_pkg.create_app()
ctx = shop_app.app_context()
ctx.push()
shop_db.create_all()
co = Company(name="SAMPLE BUSINESS", is_default=True, active=True)
shop_db.session.add(co)
SELLERS = {"anitha": "Anitha Raman", "karthik": "Karthik Sundar", "divya": "Divya Menon", "farhan": "Farhan Ali"}
susers = {}
for u, n in SELLERS.items():
    s = SUser(username=u, full_name=n, role="cashier")
    s.set_password("demo-only")
    susers[u] = s
    shop_db.session.add(s)
saree_c, kurti_c, shirt_c = Category(name="SAREE"), Category(name="KURTI"), Category(name="SHIRT")
shop_db.session.add_all([saree_c, kurti_c, shirt_c])
shop_db.session.flush()
branch = Location(name="SAMPLE STORE 1", company_id=co.id, active=True)
shop_db.session.add(branch)
shop_db.session.flush()
prods = [
    Product(sku="SMPL-SAR-01", barcode="8900100", name="SILK SAREE", category_id=saree_c.id, brand="SAMPLE SILKS",
            selling_price=2400, cost_price=1500, purchase_date=START - dt.timedelta(days=200)),
    Product(sku="SMPL-KUR-01", barcode="8900101", name="COTTON KURTI", category_id=kurti_c.id, brand="SAMPLE WEAVES",
            selling_price=950, cost_price=560, purchase_date=START),
    Product(sku="SMPL-SHI-01", barcode="8900102", name="LINEN SHIRT", category_id=shirt_c.id, brand="SAMPLE WEAVES",
            selling_price=1300, cost_price=780, purchase_date=START - dt.timedelta(days=120)),
]
shop_db.session.add_all(prods)
shop_db.session.flush()
bill = 0
d = START
while d < TODAY:
    if d.weekday() != 6:
        for u, s in susers.items():
            for _ in range(random.randint(0, 2)):
                bill += 1
                lines = [(random.choice(prods), random.randint(1, 2)) for _ in range(random.randint(1, 3))]
                total = sum(p.selling_price * q for p, q in lines)
                inv = Invoice(invoice_number="SB-%05d" % bill, cashier_id=s.id, staff_id=s.id,
                              invoice_date=utc(d, random.randint(11, 20), random.randint(0, 59)),
                              subtotal=total, total=total, company_id=co.id, location_id=branch.id,
                              payment_status="paid", payment_method="cash", fin_year="26")
                shop_db.session.add(inv)
                shop_db.session.flush()
                for p, q in lines:
                    shop_db.session.add(InvoiceItem(invoice_id=inv.id, product_id=p.id, quantity=q,
                                                    unit_price=p.selling_price, line_total=q * p.selling_price, gst_rate=5))
    d += dt.timedelta(days=1)
# a few till check-ins this month, for "Sync from Store"
for dd in range(1, TODAY.day):
    day_ = M0.replace(day=dd)
    if day_.weekday() != 6:
        shop_db.session.add(SAttendance(user_id=susers["farhan"].id, check_in=utc(day_, 9, 55), check_out=utc(day_, 21, 2)))
shop_db.session.commit()
ctx.pop()
for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.") or k == "config"]:
    del sys.modules[name]
sys.path.remove(str(SHOP))
print("till: %d bills by %d sellers" % (bill, len(SELLERS)))

# ===========================================================================
# the warehouse (where HR lives)
os.environ["DATABASE_URL"] = "sqlite:///" + WH_DB
sys.path.insert(0, str(ROOT))
from backend.app import models                                      # noqa: E402
from backend.app.database import engine, DB_URL                     # noqa: E402
from backend.app.main import app                                    # noqa: E402
from backend.app.services import hr as hr_svc                       # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

if not DB_URL.startswith("sqlite") or tmp.replace(os.sep, "/") not in DB_URL:
    sys.exit("refusing to run: the database is not the throwaway one (%s)" % DB_URL)

models.Base.metadata.create_all(bind=engine)
client = TestClient(app)
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                  json={"username": "superadmin", "password": "super@123"}).json()["token"]
H = {"Authorization": "Bearer " + tok}
problems = []


def get(path, **params):
    r = client.get("/api/hr/" + path, headers=H, params=params)
    if r.status_code != 200:
        problems.append("GET %s %s %s" % (path, r.status_code, r.text[:200]))
    return r.json()


def send(method, path, body=None, quiet=False):
    r = client.request(method, "/api/hr/" + path, headers=H, json=body)
    if r.status_code != 200 and not quiet:
        problems.append("%s %s %s %s" % (method, path, r.status_code, r.text[:200]))
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


o = get("options")
dep = {x["name"]: x["id"] for x in o["departments"]}
desig = {}
for x in o["designations"]:
    desig.setdefault(x["name"], x["id"])
shifts = {x["name"]: x["id"] for x in o["shifts"]}
print("departments:", ", ".join(dep), "| designations:", len(desig), "| shifts:", ", ".join(shifts))


def dg(*names):
    for n in names:
        if n in desig:
            return desig[n]
    return None


def dp(*names):
    for n in names:
        if n in dep:
            return dep[n]
    return next(iter(dep.values()))


# holidays
for hd, hn, opt in [(dt.date(TODAY.year, 8, 15), "Independence Day", False), (dt.date(TODAY.year, 10, 2), "Gandhi Jayanti", False),
                    (dt.date(TODAY.year, 10, 20), "Ayudha Pooja", True), (dt.date(TODAY.year, 11, 8), "Deepavali", False),
                    (dt.date(TODAY.year, 12, 25), "Christmas", False), (dt.date(TODAY.year + 1, 1, 15), "Pongal", False),
                    (dt.date(TODAY.year + 1, 1, 26), "Republic Day", False)]:
    send("POST", "masters/holidays", {"date": hd.isoformat(), "name": hn, "optional": opt})
HOLIDAYS = {x["date"] for x in get("masters/holidays")}

# ---------------------------------------------------------------- the people
G, MO, EV = shifts.get("General"), shifts.get("Morning"), shifts.get("Evening")
STORE, WH_A, WH_B, OFFICE = "Sample Store 1", "Sample Warehouse A", "Sample Warehouse B", "Head Office"
PEOPLE = [
    # name, gender, dob, joined, dept, designation, location, shift, salary(basic,hra,special), extra
    ("Meera Krishnan", "Female", "1978-07-21", "2016-04-01", ("Administration",), ("General Manager",), OFFICE, G, (68000, 34000, 30000), {"esi_applicable": False}),
    ("Rajesh Iyer", "Male", "1984-02-11", "2018-06-15", ("Accounts", "Administration"), ("Accountant", "Accounts Executive"), OFFICE, G, (32000, 16000, 9000), {}),
    ("Lakshmi Narayanan", "Female", "1986-11-03", "2019-01-07", ("Administration",), ("HR Executive", "Office Assistant"), OFFICE, G, (26000, 13000, 6000), {}),
    ("Suresh Babu", "Male", "1982-05-30", "2017-03-20", ("Sales",), ("Store Manager",), STORE, G, (36000, 18000, 12000), {}),
    ("Anitha Raman", "Female", "1994-09-14", "2021-08-02", ("Sales",), ("Sales Executive",), STORE, MO, (14000, 5600, 2400), {"store_username": "anitha"}),
    ("Karthik Sundar", "Male", "1996-01-25", "2022-02-14", ("Sales",), ("Sales Executive",), STORE, EV, (13500, 5400, 2100), {"store_username": "karthik"}),
    ("Divya Menon", "Female", "1998-04-08", "2023-07-10", ("Sales",), ("Floor Supervisor", "Sales Executive"), STORE, MO, (16000, 6400, 3600), {"store_username": "divya"}),
    ("Farhan Ali", "Male", "1999-12-19", "2024-03-04", ("Billing", "Sales"), ("Cashier",), STORE, G, (12500, 5000, 1500), {"store_username": "farhan"}),
    ("Prakash Murugan", "Male", "1988-08-17", "2018-10-01", ("Warehouse", "Stores"), ("Warehouse Supervisor", "Store Keeper"), WH_A, G, (24000, 9600, 4400), {}),
    ("Selvi Arumugam", "Female", "1991-03-12", "2020-11-16", ("Warehouse", "Stores"), ("Store Keeper", "Helper"), WH_A, MO, (15000, 6000, 2000), {}),
    ("Vignesh Kumar", "Male", "1997-06-28", "2022-09-05", ("Warehouse", "Stores"), ("Helper", "Packer"), WH_B, EV, (11500, 4600, 1400), {}),
    ("Nandhini Bala", "Female", "1993-10-02", "2021-05-24", ("Alteration",), ("Tailor", "Master Tailor"), STORE, G, (15500, 6200, 2300), {}),
    ("Arun Prasad", "Male", "1995-02-06", "2023-01-09", ("Alteration",), ("Tailor", "Embroiderer"), STORE, G, (13000, 5200, 1800), {}),
    ("Gokul Raj", "Male", "1990-07-07", "2019-12-02", ("Warehouse", "Stores"), ("Driver", "Helper"), WH_B, G, (14000, 5600, 2400), {}),
]
BANKS = ["Sample Bank", "Demo Co-operative Bank"]
emp = {}
for i, (name, gender, dob, joined, dnames, gnames, loc, shift, (b, h, s), extra) in enumerate(PEOPLE):
    d_id = dp(*dnames)
    g_id = dg(*gnames)
    if g_id is None:
        g_id = send("POST", "masters/designations", {"name": gnames[0], "department_id": d_id}).get("id")
        desig[gnames[0]] = g_id
    first = name.split()[0].lower()
    body = {"name": name, "gender": gender, "dob": dob, "joined_on": joined, "phone": "90000%05d" % (100 + i),
            "email": f"{first}@sample-business.example", "department_id": d_id, "designation_id": g_id,
            "location": loc, "shift_id": shift, "weekly_off": "Sunday", "employment_type": "Permanent",
            "bank_name": BANKS[i % 2], "bank_account": "0000%08d" % (40000 + i * 7), "ifsc": "SMPL000%04d" % (10 + i % 3),
            "pan": "ABCPD%04dZ" % (1000 + i), "father_name": "—",
            "current_address": f"{10 + i}, Sample Street, Demo Nagar", "salary": {"basic": b, "hra": h, "special": s}}
    body.update(extra)
    r = send("POST", "employees", body)
    if "id" in r:
        emp[name] = r
print("employees:", len(emp))

# who reports to whom
mgr = {"Rajesh Iyer": "Meera Krishnan", "Lakshmi Narayanan": "Meera Krishnan", "Suresh Babu": "Meera Krishnan",
       "Prakash Murugan": "Meera Krishnan", "Anitha Raman": "Divya Menon", "Karthik Sundar": "Divya Menon",
       "Divya Menon": "Suresh Babu", "Farhan Ali": "Suresh Babu", "Nandhini Bala": "Suresh Babu",
       "Arun Prasad": "Nandhini Bala", "Selvi Arumugam": "Prakash Murugan", "Vignesh Kumar": "Prakash Murugan",
       "Gokul Raj": "Prakash Murugan"}
for n, m in mgr.items():
    if n in emp and m in emp:
        send("PUT", f"employees/{emp[n]['id']}", {"reporting_to_id": emp[m]["id"]})

# a raise, a contract or two, a note on the role history
send("PUT", f"employees/{emp['Divya Menon']['id']}", {"salary": {"basic": 17000, "hra": 6800, "special": 3700}})
send("POST", f"employees/{emp['Karthik Sundar']['id']}/contracts",
     {"contract_type": "Fixed term", "start_date": "2025-02-14", "end_date": (TODAY + dt.timedelta(days=25)).isoformat(), "ctc_annual": 252000})
send("POST", f"employees/{emp['Farhan Ali']['id']}/contracts",
     {"contract_type": "Appointment", "start_date": "2024-03-04", "ctc_annual": 228000})
send("POST", f"employees/{emp['Prakash Murugan']['id']}/history",
     {"kind": "Transfer", "date": "2023-04-01", "detail": "Moved from Sample Warehouse B to Sample Warehouse A"})

# ---------------------------------------------------------------- attendance
ACTIVE = [n for n in emp]
LEAVER, NOTICE = "Gokul Raj", "Arun Prasad"
LEAVER_LAST = M1 + dt.timedelta(days=17)
times = {x["id"]: (x["start_time"], x["end_time"]) for x in o["shifts"]}   # each shift as ESSA defines it
marked = 0
d = START
while d < TODAY:
    if d.weekday() != 6 and d.isoformat() not in HOLIDAYS:
        for n in ACTIVE:
            if n == LEAVER and d > LEAVER_LAST:
                continue
            e = emp[n]
            sh = PEOPLE[[p[0] for p in PEOPLE].index(n)][7]
            t_in, t_out = times.get(sh, ("10:00", "19:00"))
            roll = random.random()
            if roll < 0.03:
                body = {"status": "A"}
            elif roll < 0.05:
                body = {"status": "HD", "in_time": t_in, "out_time": "%02d:00" % (int(t_in[:2]) + 4)}
            else:
                hh, mm = int(t_in[:2]), int(t_in[3:])
                late = random.choice([0] * 8 + [5, 12, 25, 40])
                mm += late - random.randint(0, 6) if not late else late
                hh += mm // 60
                mm = mm % 60 if mm >= 0 else 0
                oh = int(t_out[:2]) + (random.choice([0] * 10 + [1, 2]))
                body = {"in_time": "%02d:%02d" % (hh, mm), "out_time": "%02d:%02d" % (oh, random.randint(0, 15))}
            send("PUT", f"attendance/{e['id']}/{d.isoformat()}", body, quiet=True)
            marked += 1
    d += dt.timedelta(days=1)
# this morning: most people are in, a couple not yet (the dashboard's "present today")
for n in ACTIVE:
    if n in (LEAVER, "Vignesh Kumar", "Lakshmi Narayanan"):
        continue
    sh = PEOPLE[[p[0] for p in PEOPLE].index(n)][7]
    t_in = times.get(sh, ("10:00", "21:00"))[0]
    if t_in <= "11:00":
        send("PUT", f"attendance/{emp[n]['id']}/{TODAY.isoformat()}",
             {"in_time": t_in[:3] + "%02d" % random.choice([0, 2, 4, 9, 18])}, quiet=True)
print("attendance days marked:", marked)
send("POST", "attendance/sync-store", {"date_from": M0.isoformat(), "date_to": TODAY.isoformat()}, quiet=True)

# rosters: this week and next, morning/evening across the store
mon = TODAY - dt.timedelta(days=TODAY.weekday())
for wk in (0, 7):
    a = [emp[n]["id"] for n in ("Anitha Raman", "Divya Menon", "Selvi Arumugam")]
    b = [emp[n]["id"] for n in ("Karthik Sundar", "Vignesh Kumar")]
    send("POST", "roster", {"employee_ids": a if wk == 0 else b, "date_from": (mon + dt.timedelta(days=wk)).isoformat(),
                            "date_to": (mon + dt.timedelta(days=wk + 5)).isoformat(), "shift_id": MO})
    send("POST", "roster", {"employee_ids": b if wk == 0 else a, "date_from": (mon + dt.timedelta(days=wk)).isoformat(),
                            "date_to": (mon + dt.timedelta(days=wk + 5)).isoformat(), "shift_id": EV})

# overtime: approve most of what is older than this month, leave this month's pending
for first in (M3, M2, M1):
    ot = get("overtime", date_from=first.isoformat(), date_to=month_last(first).isoformat(), status="pending")
    ids = [x["id"] for x in ot] if isinstance(ot, list) else []
    if ids:
        send("POST", "overtime/decide", {"ids": ids[: max(1, len(ids) - 2)], "decision": "approved"})
        if len(ids) > 2:
            send("POST", "overtime/decide", {"ids": ids[-1:], "decision": "rejected"}, quiet=True)

# ---------------------------------------------------------------- leave
for first in (M3, M2, M1, M0):
    send("POST", "leave-accrual", {"month": ym(first)}, quiet=True)
LT = {t["code"]: t["id"] for t in o["leave_types"]}


def workday_after(d0, n=0):
    d_ = d0
    while d_.weekday() == 6 or d_.isoformat() in HOLIDAYS:
        d_ += dt.timedelta(days=1)
    for _ in range(n):
        d_ += dt.timedelta(days=1)
        while d_.weekday() == 6 or d_.isoformat() in HOLIDAYS:
            d_ += dt.timedelta(days=1)
    return d_


def ask(name, code, frm, to=None, reason="", decide=None, note=""):
    r = send("POST", "leave", {"employee_id": emp[name]["id"], "leave_type_id": LT.get(code), "from_date": frm.isoformat(),
                               "to_date": (to or frm).isoformat(), "reason": reason}, quiet=True)
    if decide and "id" in r:
        send("POST", f"leave/{r['id']}/{decide}", {"note": note}, quiet=True)


ask("Anitha Raman", "SL", workday_after(M2 + dt.timedelta(days=8)), reason="Fever", decide="approved", note="Get well soon")
ask("Rajesh Iyer", "CL", workday_after(M2 + dt.timedelta(days=21)), reason="Family function", decide="approved")
ask("Selvi Arumugam", "CL", workday_after(M1 + dt.timedelta(days=4)), reason="Personal work", decide="approved")
ask("Karthik Sundar", "LOP", workday_after(M1 + dt.timedelta(days=12)), workday_after(M1 + dt.timedelta(days=12), 1),
    reason="Travelling home", decide="approved")
ask("Nandhini Bala", "CL", workday_after(M1 + dt.timedelta(days=22)), reason="School meeting", decide="rejected",
    note="Festival week — please pick another day")
ask("Divya Menon", "EL", workday_after(TODAY + dt.timedelta(days=9)), workday_after(TODAY + dt.timedelta(days=9), 2),
    reason="Wedding in the family")
ask("Farhan Ali", "CL", workday_after(TODAY + dt.timedelta(days=3)), reason="Bank work")
ask("Vignesh Kumar", "SL", workday_after(TODAY + dt.timedelta(days=1)), reason="Dental appointment")
ask("Lakshmi Narayanan", "CL", workday_after(TODAY + dt.timedelta(days=15)), reason="Personal work")

# ---------------------------------------------------------------- advances
send("POST", "advances", {"employee_id": emp["Karthik Sundar"]["id"], "amount": 6000, "emi": 2000,
                          "issued_on": (M2 + dt.timedelta(days=3)).isoformat(), "recover_from": ym(M2), "reason": "Festival advance"})
send("POST", "advances", {"employee_id": emp["Selvi Arumugam"]["id"], "amount": 15000, "emi": 2500,
                          "issued_on": (M1 + dt.timedelta(days=5)).isoformat(), "recover_from": ym(M1), "reason": "Medical"})
send("POST", "advances", {"employee_id": emp["Farhan Ali"]["id"], "amount": 4000, "emi": 1000,
                          "issued_on": (M0 + dt.timedelta(days=1)).isoformat(), "recover_from": ym(M0), "reason": "Two-wheeler repair"})

# ---------------------------------------------------------------- incentives
send("POST", "incentive-schemes", {"name": "1% on every sale", "basis": "pct", "rate": 1})
send("POST", "incentive-schemes", {"name": "₹50 a piece on stock over 90 days", "basis": "per_piece", "rate": 50,
                                   "filters": {"age_min": 90}})
send("POST", "incentive-schemes", {"name": "Sample Weaves push", "basis": "pct", "rate": 3,
                                   "filters": {"brands": "SAMPLE WEAVES"}})

# ---------------------------------------------------------------- payroll
for first, paid in ((M3, True), (M2, True), (M1, False)):
    m = ym(first)
    send("POST", f"payroll/{m}/compute")
    send("POST", f"payroll/{m}/approve")
    if paid:
        send("POST", f"payroll/{m}/pay", {"date": (month_last(first) + dt.timedelta(days=3)).isoformat(), "mode": "NEFT"})

# ---------------------------------------------------------------- performance
cyc = send("POST", "cycles", {"name": "Quarterly review — " + M3.strftime("%b") + "–" + M1.strftime("%b %Y"),
                              "period_from": M3.isoformat(), "period_to": month_last(M1).isoformat()})
if "id" in cyc:
    for n in ("Anitha Raman", "Karthik Sundar", "Divya Menon", "Farhan Ali"):
        send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": emp[n]["id"], "title": "Sales", "metric": "sales",
                               "weight": 60, "target": 150000})
        send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": emp[n]["id"], "title": "Customer feedback",
                               "weight": 40, "target": 10, "actual": random.choice([7, 8, 9])})
    for n in ("Prakash Murugan", "Selvi Arumugam"):
        send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": emp[n]["id"], "title": "Dispatch on time (%)",
                               "weight": 70, "target": 98, "actual": random.choice([94, 97, 99])})
        send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": emp[n]["id"], "title": "Stock audit accuracy (%)",
                               "weight": 30, "target": 99, "actual": random.choice([97, 99])})
    a1 = send("POST", "appraisals", {"cycle_id": cyc["id"], "employee_id": emp["Divya Menon"]["id"], "rating": 4,
                                     "increment_pct": 8, "effective_on": M0.isoformat(), "status": "approved",
                                     "remarks": "Runs the floor well; strong on the festival rush"})
    send("POST", "appraisals", {"cycle_id": cyc["id"], "employee_id": emp["Anitha Raman"]["id"], "rating": 3,
                                "increment_pct": 5, "status": "submitted", "remarks": "Steady; work on add-on sales"})
    if "id" in a1:
        send("POST", f"appraisals/{a1['id']}/apply")
ann = send("POST", "cycles", {"name": "Annual review " + str(TODAY.year) + "–" + str(TODAY.year + 1)[2:],
                              "period_from": dt.date(TODAY.year, 4, 1).isoformat(),
                              "period_to": dt.date(TODAY.year + 1, 3, 31).isoformat()})
if "id" in ann:   # the screen opens on the newest cycle, so it carries goals too
    for n in ("Suresh Babu", "Divya Menon", "Anitha Raman", "Karthik Sundar", "Farhan Ali"):
        send("POST", "goals", {"cycle_id": ann["id"], "employee_id": emp[n]["id"], "title": "Sales", "metric": "sales",
                               "weight": 50, "target": 600000})
        send("POST", "goals", {"cycle_id": ann["id"], "employee_id": emp[n]["id"], "title": "Customer feedback",
                               "weight": 30, "target": 10, "actual": random.choice([7, 8, 9])})
        send("POST", "goals", {"cycle_id": ann["id"], "employee_id": emp[n]["id"], "title": "Training hours",
                               "weight": 20, "target": 24, "actual": random.choice([8, 12, 16])})
    for n in ("Prakash Murugan", "Selvi Arumugam", "Vignesh Kumar"):
        send("POST", "goals", {"cycle_id": ann["id"], "employee_id": emp[n]["id"], "title": "Dispatch on time (%)",
                               "weight": 70, "target": 98, "actual": random.choice([95, 97, 99])})
        send("POST", "goals", {"cycle_id": ann["id"], "employee_id": emp[n]["id"], "title": "Stock audit accuracy (%)",
                               "weight": 30, "target": 99, "actual": random.choice([97, 98, 99])})

# ---------------------------------------------------------------- leaving
send("POST", f"employees/{emp[LEAVER]['id']}/exit", {"last_working_day": LEAVER_LAST.isoformat(),
                                                    "exit_type": "Resigned", "exit_reason": "Moved to his home town"})
send("POST", f"employees/{emp[NOTICE]['id']}/exit", {"last_working_day": (TODAY + dt.timedelta(days=24)).isoformat(),
                                                    "exit_type": "Resigned", "exit_reason": "Starting his own tailoring unit"})

# ===========================================================================
# record every question the screens ask
entries = {}


def rec(path, params=None):
    params = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    qs = urlencode(params)
    r = client.get("/api/hr/" + path, headers=H, params=params)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
        if r.status_code not in (404,):
            problems.append("record GET %s?%s %s" % (path, qs, r.status_code))
        return None
    body = r.json()
    if path == "settings" and isinstance(body, dict) and body.get("device_key"):
        body["device_key"] = "demo-device-key-not-a-real-one"
    full = "/api/hr/" + path
    search = ("?" + qs) if qs else ""
    entries["GET - " + full + search] = {"key": "GET - " + full + search, "method": "GET", "wh": "-", "path": full,
                                         "search": search, "status": 200, "type": "application/json",
                                         "body": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}
    return body


today = TODAY.isoformat()
for p in ("options", "dashboard", "settings", "store-staff", "incentive-schemes", "payroll", "cycles", "org-chart"):
    rec(p)
for kind in ("departments", "designations", "shifts", "holidays", "leave_types"):
    rec("masters/" + kind)
opts = get("options")
dept_ids = [x["id"] for x in opts["departments"]]
locs = opts.get("locations") or []
for st in ("current", "notice", "exited", "all", ""):
    rec("employees", {"status": st})
for did in dept_ids:
    rec("employees", {"status": "current", "department_id": did})
    rec("org-chart", {"department_id": did})
for loc in locs:
    lv = loc if isinstance(loc, str) else (loc.get("name") or loc.get("value"))
    rec("employees", {"status": "current", "location": lv})
everyone = get("employees", status="all") or []
if not isinstance(everyone, list) or not everyone:
    everyone = list(emp.values())
for e in everyone:
    rec(f"employees/{e['id']}")
    rec(f"employees/{e['id']}/leave-ledger")
    rec(f"employees/{e['id']}/payslips")
    rec("leave-balances", {"employee_id": e["id"]})
for first in (M0, M1, M2, M3, month_first(TODAY, -1)):
    rec("attendance", {"month": ym(first)})
    rec("leave-calendar", {"month": ym(first)})
    pr = rec(f"payroll/{ym(first)}")
    for s in (pr or {}).get("slips", []) if isinstance(pr, dict) else []:
        rec(f"payslips/{s['id']}")
for wk in (-14, -7, 0, 7):
    rec("roster", {"start": (mon + dt.timedelta(days=wk)).isoformat()})
for first, last in ((M0, today), (M1, month_last(M1).isoformat()), (M2, month_last(M2).isoformat())):
    for st in ("pending", "approved", "rejected", ""):
        rec("overtime", {"date_from": first.isoformat(), "date_to": last, "status": st})
    rec("incentives", {"date_from": first.isoformat(), "date_to": last})
for st in ("pending", "approved", "rejected", "cancelled", ""):
    rec("leave", {"status": st})
for st in ("open", "closed", ""):
    rec("advances", {"status": st})
for c in get("cycles") or []:
    rec(f"cycles/{c['id']}/review")
P = {"month": ym(M1), "date_from": M0.isoformat(), "date_to": today, "status": "current", "department_id": "",
     "location": "", "days": 60, "as_on": today}
for k, label, group, params in hr_svc.REPORTS:
    rec(f"reports/{k}", {x: P.get(x) for x in params})
    if "month" in params:
        for first in (M2, M3, M0):
            rec(f"reports/{k}", {x: (ym(first) if x == "month" else P.get(x)) for x in params})

print("recorded %d HR answers" % len(entries))
if problems:
    print("\n%d calls did not go as planned:" % len(problems))
    for p in problems[:40]:
        print("  ", p)

# nothing of ESSA's own identity may reach the demo
leaks = [k for k, e in entries.items() if re.search(r"essa|33AADCE6591N1Z7", e["body"], re.I)]
if leaks:
    sys.exit("refusing to write: ESSA's name is in %d answers, e.g. %s" % (len(leaks), leaks[:3]))

if args.dry:
    sys.exit(0)
man = json.loads(MANIFEST.read_text(encoding="utf-8"))
man["entries"] = [e for e in man["entries"] if not str(e.get("path", "")).startswith("/api/hr/")] + list(entries.values())
man.setdefault("meta", {})["hr"] = {"recordedAt": dt.datetime.utcnow().isoformat() + "Z", "pinnedDay": today,
                                    "entries": len(entries), "source": "ESSA HR backend, fictional sample company"}
MANIFEST.write_text(json.dumps(man, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
print("wrote", MANIFEST)
