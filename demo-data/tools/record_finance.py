"""Record the Finance module's answers for the demo.

    <essa>/backend/.venv/Scripts/python.exe demo-data/tools/record_finance.py --essa D:/ESSA-Next

Finance came from ESSA (frontend/vendor/warexx-app/src/Finance.jsx is its Finance.jsx).
This runs ESSA's real Finance backend IN-PROCESS on a throwaway SQLite file in a temp
folder, keeps a FICTIONAL company's books in it from the start of the financial year
to today — opening balances, suppliers and customers billed and settled, expenses,
bank and cash, a loan, TDS, cheque books and a petty cash sheet — then asks every
question the Finance screens ask and writes the answers into
demo-data/recorded/manifest.json (replacing any /api/finance entries already there).

It never opens a real database, and nobody in it is a real business.
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
ap.add_argument("--essa", required=True)
ap.add_argument("--dry", action="store_true")
args = ap.parse_args()
ROOT = pathlib.Path(args.essa).resolve()

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

tmp = tempfile.mkdtemp(prefix="warexx-fin-demo-")
for leak in ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL",
             "SHOP_DB_SCHEMA", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ESSA_SUPERBOSS_PASSWORD", "BLOB_READ_WRITE_TOKEN"):
    os.environ.pop(leak, None)
os.environ.update({
    "ESSA_STATE_DIR": tmp,
    "ESSA_DATABASE_URL": "sqlite:///" + os.path.join(tmp, "warehouse.db").replace(os.sep, "/"),
    "ESSA_POS_DB": os.path.join(tmp, "shop.db"), "ESSA_WAREHOUSE_DB": os.path.join(tmp, "no-warehouse-here.db"),
    # the fictional company the books are kept for (never ESSA's own)
    "ESSA_COMPANY_NAME": "Sample Company Private Limited", "ESSA_COMPANY_GSTIN": "33AAAAA0000A1Z5",
})
random.seed(20261005)
TODAY = (dt.datetime.utcnow() + dt.timedelta(minutes=330)).date()
FY = dt.date(TODAY.year if TODAY.month >= 4 else TODAY.year - 1, 4, 1)
MONTH0 = TODAY.replace(day=1)

sys.path.insert(0, str(ROOT))
from backend.app import models                                      # noqa: E402
from backend.app.database import engine, DB_URL                     # noqa: E402
from backend.app.main import app                                    # noqa: E402
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
    r = client.get("/api/finance/" + path, headers=H, params=params)
    if r.status_code != 200:
        problems.append("GET %s %s %s" % (path, r.status_code, r.text[:200]))
    return r.json()


def send(method, path, body=None):
    r = client.request(method, "/api/finance/" + path, headers=H, json=body)
    if r.status_code != 200:
        problems.append("%s %s %s %s" % (method, path, r.status_code, r.text[:200]))
        return {}
    return r.json()


o = get("options")
L = {x["name"]: x["id"] for x in get("ledgers")}
CASH, SALES, PURCH, IN_GST, OUT_GST = L["Cash"], L["Sales Accounts"], L["Purchase Accounts"], L["Input GST"], L["Output GST"]
TDS_PAY, ROUND, FREIGHT = L["TDS Payable"], L["Rounding Off"], L["Freight Inward"]
BIZ = o["businesses"][0]["id"] if o.get("businesses") else None
LOC = "Sample Warehouse A"


def ledger(name, group, **kw):
    body = {"name": name, "group": group, "business_id": BIZ}
    body.update(kw)
    r = send("POST", "ledgers", body)
    L[name] = r.get("id")
    return L[name]


# ---------------------------------------------------------------- the chart of accounts
BANK = ledger("Sample Bank — Current A/c", "Bank Account", bank_account="000011112222", ifsc="SMPL0000101", location=LOC)
OD = ledger("Demo Co-operative Bank — OD A/c", "Bank Account", bank_account="000033334444", ifsc="DEMO0000202")
PETTY = ledger("Petty Cash — Store", "Petty Cash", location="Sample Store 1")
CAP = ledger("Partners' Capital", "Capital")
LOAN = ledger("Term Loan — Sample Bank", "Loans (Liability)")
STAFF_ADV = ledger("Advances to Staff", "Loans & Advances (Asset)")
DEP = ledger("Rent Deposit — Warehouse A", "Deposits (Asset)")
FURN = ledger("Furniture & Fixtures", "Fixed Asset")
COMP = ledger("Computers & Printers", "Fixed Asset")
EXP = {n: ledger(n, g, tags=t) for n, g, t in [
    ("Rent", "Indirect Expenses", ["monthly_expense", "pl_expense"]),
    ("Electricity", "Indirect Expenses", ["monthly_expense", "pl_expense"]),
    ("Salaries & Wages", "Indirect Expenses", ["monthly_expense", "pl_expense"]),
    ("Telephone & Internet", "Indirect Expenses", ["monthly_expense", "pl_expense"]),
    ("Printing & Stationery", "Indirect Expenses", ["pl_expense"]),
    ("Repairs & Maintenance", "Indirect Expenses", ["pl_expense"]),
    ("Interest on Loan", "Indirect Expenses", ["pl_expense"]),
    ("Packing Materials", "Direct Expense", ["daily_expense"]),
]}
PETTY_EXP = {n: ledger(n, "Petty Cash In-Direct Expense") for n in ("Tea & Snacks", "Courier Charges", "Auto & Local Fare", "Cleaning Supplies")}
SUPPLIERS = {}
for i, (n, city) in enumerate([("Sample Weaves Mills", "Erode"), ("Demo Silk House", "Kanchipuram"),
                               ("Placeholder Garments Co", "Tiruppur"), ("Example Threads & Trims", "Coimbatore"),
                               ("Model Fabrics Pvt Ltd", "Salem"), ("Test Packaging Works", "Karur")]):
    SUPPLIERS[n] = ledger(n, "Sundry Creditors", billwise=True, credit_days=random.choice([30, 45, 60]),
                          gstin="33AAAAB%04dA1Z%d" % (1000 + i, i), pan="AAAAB%04dA" % (1000 + i), state="Tamil Nadu",
                          address=f"{12 + i}, Mill Road, {city}", phone="90000%05d" % (30000 + i * 17),
                          # ESSA's TDS report counts every line on a "TDS applicable" ledger as tax, so a
                          # supplier marked so would show each bill in full; the section is kept, and the
                          # tax itself goes to TDS Payable on the payment.
                          tds_applicable=False, tds_section="194Q" if i in (0, 4) else None,
                          tds_rate=0.1 if i in (0, 4) else None)
CUSTOMERS = {}
for i, (n, city) in enumerate([("Sample Retail Stores", "Madurai"), ("Demo Boutique", "Chennai"),
                               ("Example Uniforms", "Trichy"), ("Placeholder Fashions", "Salem"),
                               ("Model Mart", "Coimbatore")]):
    CUSTOMERS[n] = ledger(n, "Sundry Debtors", billwise=True, credit_days=30,
                          gstin="33AAAAC%04dB1Z%d" % (2000 + i, i), state="Tamil Nadu",
                          address=f"{40 + i}, Bazaar Street, {city}", phone="90000%05d" % (50000 + i * 23))

# ---------------------------------------------------------------- opening balances (day one of the year)
opening = [(CAP, -3500000), (LOAN, -1200000), (BANK, 1850000), (OD, -150000), (CASH, 85000), (PETTY, 15000),
           (FURN, 640000), (COMP, 310000), (DEP, 300000), (STAFF_ADV, 25000)]
diff = -sum(v for _, v in opening)
opening[0] = (CAP, opening[0][1] + diff)   # capital takes up the difference, so the opening trial balance agrees
send("PUT", "opening", [{"ledger_id": lid, "opening_balance": v} for lid, v in opening])

# ---------------------------------------------------------------- a year of trading
def v(vtype, date, lines, **kw):
    body = {"vtype": vtype, "date": date.isoformat(), "business_id": BIZ, "location": kw.pop("location", LOC),
            "lines": [{"ledger_id": lid, "debit": round(dr, 2), "credit": round(cr, 2), "bill_ref": ref, "narration": None}
                      for lid, dr, cr, ref in lines]}
    body.update(kw)
    return send("POST", "vouchers", body)


bill_no = {"P": 0, "S": 0}
open_purch, open_sales = [], []
d = FY
while d <= TODAY:
    wd = d.weekday()
    if wd != 6:
        # counter sales, cash and bank (daily)
        amt = random.randint(70, 150) * 1000
        gst = round(amt * 0.05, 2)
        v("Sales", d, [(CASH if random.random() < 0.6 else BANK, amt + gst, 0, None), (SALES, 0, amt, None),
                       (OUT_GST, 0, gst, None)], mode=random.choice(["Cash", "UPI", "Card"]), narration="Counter sales")
        # a credit sale now and then
        if random.random() < 0.25:
            bill_no["S"] += 1
            cn, cid = random.choice(list(CUSTOMERS.items()))
            amt = random.randint(60, 240) * 1000
            gst = round(amt * 0.05, 2)
            ref = "SI-%04d" % bill_no["S"]
            v("Sales", d, [(cid, amt + gst, 0, ref), (SALES, 0, amt, None), (OUT_GST, 0, gst, None)],
              party_ledger_id=cid, ref_no=ref, narration=f"Credit sale to {cn}")
            if random.random() > 0.15:      # some customers are slow — their bills age on Receivable
                open_sales.append((d, cid, ref, amt + gst))
        # a supplier bill twice a week
        if wd in (1, 4):
            bill_no["P"] += 1
            sn, sid = random.choice(list(SUPPLIERS.items()))
            amt = random.randint(60, 260) * 1000
            gst = round(amt * 0.05, 2)
            ref = "PB-%04d" % bill_no["P"]
            fr = random.choice([0, 0, 1500, 2800])
            lines = [(PURCH, amt, 0, None), (IN_GST, gst, 0, None), (sid, 0, amt + gst + fr, ref)]
            if fr:
                lines.append((FREIGHT, fr, 0, None))
            v("Purchase", d, lines, party_ledger_id=sid, ref_no=ref, narration=f"Goods from {sn}")
            if random.random() > 0.12:      # and some bills wait on a dispute — they age on Payable
                open_purch.append((d, sid, ref, amt + gst + fr))
    # settle bills that have come due (most of them, not all — so Payable/Receivable have something to age)
    for bucket, side in ((open_purch, "P"), (open_sales, "S")):
        keep = []
        for bd, pid, ref, total in bucket:
            due = bd + dt.timedelta(days=random.choice([25, 35, 50]))
            if due <= d and random.random() < 0.85 and d.weekday() != 6:
                if side == "P":
                    tds = round(total * 0.001, 2) if pid in (SUPPLIERS["Sample Weaves Mills"], SUPPLIERS["Model Fabrics Pvt Ltd"]) else 0
                    mode = random.choice(["NEFT", "RTGS", "Cheque"])
                    lines = [(pid, total, 0, ref), (BANK, 0, total - tds, None)] + ([(TDS_PAY, 0, tds, None)] if tds else [])
                    v("Payment", d, lines, party_ledger_id=pid, mode=mode, ref_no=ref, tds_amount=tds or None,
                      cheque_no=str(400100 + len(keep) + bd.toordinal() % 400) if mode == "Cheque" else None,
                      cheque_date=d.isoformat() if mode == "Cheque" else None, narration=f"Paid against {ref}")
                else:
                    v("Receipt", d, [(BANK, total, 0, None), (pid, 0, total, ref)], party_ledger_id=pid,
                      mode=random.choice(["NEFT", "UPI", "Cheque"]), ref_no=ref, narration=f"Received against {ref}")
            else:
                keep.append((bd, pid, ref, total))
        bucket[:] = keep
    # month-end and monthly items
    last = calendar.monthrange(d.year, d.month)[1]
    if d.day == 3:
        v("Payment", d, [(EXP["Rent"], 120000, 0, None), (BANK, 0, 108000, None), (TDS_PAY, 0, 12000, None)],
          mode="NEFT", tds_amount=12000, narration="Warehouse rent (TDS 194-I)")
        v("Payment", d, [(EXP["Telephone & Internet"], 6800, 0, None), (BANK, 0, 6800, None)], mode="UPI",
          narration="Phones and broadband")
    if d.day == 10:
        amt = random.randint(38, 55) * 1000
        v("Payment", d, [(EXP["Electricity"], amt, 0, None), (BANK, 0, amt, None)], mode="NEFT", narration="Electricity")
    if d.day == 5:
        v("Payment", d, [(LOAN, 50000, 0, None), (EXP["Interest on Loan"], 11200, 0, None), (BANK, 0, 61200, None)],
          mode="NEFT", narration="Term loan EMI")
    if d.day == last:
        v("Payment", d, [(EXP["Salaries & Wages"], 485000, 0, None), (BANK, 0, 485000, None)], mode="NEFT",
          narration="Salaries for the month")
        v("Contra", d, [(BANK, 300000, 0, None), (CASH, 0, 300000, None)], mode="Cash", narration="Cash deposited")
        v("Contra", d, [(PETTY, 12000, 0, None), (CASH, 0, 12000, None)], mode="Cash", narration="Petty cash top-up")
    if d.day == 18 and random.random() < 0.6:
        amt = random.randint(4, 18) * 1000
        v("Payment", d, [(EXP[random.choice(["Repairs & Maintenance", "Printing & Stationery", "Packing Materials"])], amt, 0, None),
                         (CASH, 0, amt, None)], mode="Cash", narration="Shop expenses")
    d += dt.timedelta(days=1)

# a discount journal, a staff advance and a credit note
v("Journal", FY + dt.timedelta(days=182), [(L["Discount Allowed"], 4200, 0, None), (CUSTOMERS["Demo Boutique"], 0, 4200, None)],
  narration="Settlement discount allowed")
v("Payment", TODAY - dt.timedelta(days=12), [(STAFF_ADV, 15000, 0, None), (CASH, 0, 15000, None)], mode="Cash",
  narration="Advance to a staff member")
cn_c = CUSTOMERS["Example Uniforms"]
send("POST", "credit-notes", {"ledger_id": cn_c, "date": (TODAY - dt.timedelta(days=6)).isoformat(), "bill_no": "CN-0001",
                              "remarks": "Short-supplied sizes returned", "business_id": BIZ, "location": LOC,
                              "rows": [{"rate": 5, "value": 18000}]})

# today: a few vouchers, so Voucher Entry and the Daybook (which open on today) are not empty
v("Receipt", TODAY, [(CASH, 6500, 0, None), (L["Sales Accounts"], 0, 6500, None)], mode="Cash", narration="Alteration charges collected")
v("Payment", TODAY, [(EXP["Printing & Stationery"], 2350, 0, None), (CASH, 0, 2350, None)], mode="Cash", narration="Bill books")

# cheque books
send("POST", "cheque-books", {"bank_ledger_id": BANK, "book_no": "CB-01", "from_no": 400100, "to_no": 400149,
                              "issued_on": FY.isoformat(), "remarks": "First book of the year"})
send("POST", "cheque-books", {"bank_ledger_id": BANK, "book_no": "CB-02", "from_no": 400150, "to_no": 400199,
                              "issued_on": (FY + dt.timedelta(days=150)).isoformat()})
books = get("cheque-books", bank_id=BANK) or []
if isinstance(books, list) and books:
    send("POST", f"cheque-books/{books[0]['id']}/cancel", {"no": 400117, "reason": "Spoilt — wrong amount written"})

# petty cash: this month and last month, a few spends a day
for first in (MONTH0.replace(day=1) - dt.timedelta(days=1), TODAY):
    first = first.replace(day=1)
    ndays = (TODAY.day if first == MONTH0 else calendar.monthrange(first.year, first.month)[1])
    rows = []
    for name, lid in PETTY_EXP.items():
        rows.append({"ledger_id": lid, "days": [random.choice([0, 0, 0, 40, 60, 120, 150]) if (first + dt.timedelta(days=i)).weekday() != 6 else 0
                                                for i in range(ndays)]})
    send("POST", "petty-cash", {"cash_ledger_id": PETTY, "month": first.strftime("%Y-%m"), "location": "Sample Store 1",
                                "business_id": BIZ, "touched": list(range(1, ndays + 1)), "rows": rows})

# Tally settings (where Tally would run — nothing is sent anywhere)
send("PUT", "tally/config", {"business_id": BIZ, "host": "localhost", "port": 9000, "company": "Sample Company Pvt Ltd"})

print("books kept: %d vouchers" % len(get("vouchers", date_from=FY.isoformat(), date_to=TODAY.isoformat()) or []))

# ===========================================================================
# record every question the screens ask
entries = {}


def rec(path, params=None, keep_order=False):
    params = {k: (",".join(v) if isinstance(v, list) else v) for k, v in (params or {}).items() if v not in (None, "", [])}
    qs = urlencode(params)
    r = client.get("/api/finance/" + path, headers=H, params=params)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        problems.append("record GET %s?%s %s" % (path, qs, r.status_code))
        return None
    full = "/api/finance/" + path
    search = ("?" + qs) if qs else ""
    entries["GET - " + full + search] = {"key": "GET - " + full + search, "method": "GET", "wh": "-", "path": full,
                                         "search": search, "status": 200, "type": "application/json",
                                         "body": r.text}
    return r.json()


today = TODAY.isoformat()
fy = FY.isoformat()
m0 = MONTH0.isoformat()
opts = rec("options")
rec("ledgers")
all_ledgers = rec("ledgers", {}) or []
for g in sorted({x["group"] for x in all_ledgers}):
    rec("ledgers", {"group": g})
for flt in [f["key"] for f in opts.get("filters", [])]:
    rec("ledgers", {"filter": flt})
for x in all_ledgers:
    rec(f"ledgers/{x['id']}/statement", {"date_from": fy, "date_to": today})
    if x["group"] in ("Sundry Creditors", "Sundry Debtors"):
        rec(f"ledgers/{x['id']}/bills")
rec("opening")
# Voucher Entry (opens on today), the list per type, and each voucher of today and the last week
for vt in [""] + opts.get("vtypes", []):
    rec("vouchers", {"vtype": vt, "date_from": today, "date_to": today})
    rec("vouchers", {"vtype": vt, "date_from": m0, "date_to": today})
for x in rec("vouchers", {"date_from": (TODAY - dt.timedelta(days=7)).isoformat(), "date_to": today}) or []:
    rec(f"vouchers/{x['id']}")
rec("vouchers", {"vtype": "Purchase", "date_from": m0, "date_to": today})
rec("daybook", {"date_from": today, "date_to": today})
rec("daybook", {"date_from": m0, "date_to": today})
rec("daybook", {"date_from": (TODAY - dt.timedelta(days=7)).isoformat(), "date_to": today})
for p, extra in (("trial-balance", {}), ("trading", {}), ("pl", {})):
    rec(p, {"date_from": fy, "date_to": today, **extra})
    rec(p, {"date_from": m0, "date_to": today, **extra})
rec("balance-sheet", {"as_on": today})
rec("daily-pl", {"date_from": m0, "date_to": today})
for side in ("payable", "receivable"):
    rec(f"outstanding/{side}", {"as_on": today})
for mode in ("group", "ledger", "vtype", "location"):
    rec("analyser", {"date_from": fy, "date_to": today, "mode": mode})
rec("analyser", {"date_from": fy, "date_to": today})
rec("tds", {"date_from": fy, "date_to": today})
rec("tds", {"date_from": m0, "date_to": today})
rec("gst-inbox", {"date_from": m0, "date_to": today})
rec("loans", {"date_from": fy, "as_on": today})
rec("incentive", {"date_from": m0, "date_to": today})
for b in opts.get("banks", []):
    rec("cheque-books", {"bank_id": b["id"]})
    for st in ("", "pending", "cleared"):
        rec("brs", {"bank_id": b["id"], "status": st, "date_from": m0, "date_to": today})
    rec("brs", {"bank_id": b["id"], "date_from": fy, "date_to": today})
rec("cheque-books")
for c in opts.get("cash", []) + [{"id": PETTY}]:
    for first in (MONTH0, (MONTH0 - dt.timedelta(days=1)).replace(day=1)):
        rec("petty-cash", {"cash_ledger_id": c["id"], "month": first.strftime("%Y-%m")})
for b in opts.get("businesses", []):
    rec("tally/config", {"business_id": b["id"]})
rec("tally/config")
rec("transport/pending")
rec("agent/pending")

print("recorded %d Finance answers" % len(entries))
if problems:
    print("\n%d calls did not go as planned:" % len(problems))
    for p in problems[:40]:
        print("  ", p)

leaks = [k for k, e in entries.items() if re.search(r"(?<![-\w])essa(?![-\w])|33AADCE6591N1Z7", e["body"], re.I)]
if leaks:
    print("\nESSA's name is in %d answers, e.g. %s" % (len(leaks), leaks[:3]))
    b = entries[leaks[0]]["body"]
    mm = re.search(r"(?i)(?<![-\w])essa(?![-\w])", b)
    if mm:
        print("   …", b[max(0, mm.start() - 80): mm.end() + 80])
    if not args.dry:
        sys.exit("refusing to write")
if args.dry:
    sys.exit(0)
# Large answers go in recorded/bodies/ (fetched only when that screen asks), not inline in
# manifest.json, which every visitor downloads on every page load.
BODIES = MANIFEST.parent / "bodies"
if os.name == "nt":   # a deep checkout can pass Windows' 260-character path limit
    BODIES = pathlib.Path(r"\\?" + "\\" + str(BODIES.resolve()))
for old in BODIES.glob("fin-*"):
    old.unlink()
total = inline = 0
for e in entries.values():
    total += len(e["body"])
    if len(e["body"]) > 20000:
        name = "fin-" + re.sub(r"[^A-Za-z0-9]+", "-", (e["path"] + e["search"])[len("/api/finance/"):]).strip("-")[:110] + ".json"
        (BODIES / name).write_text(e.pop("body"), encoding="utf-8")
        e["file"] = name
    else:
        inline += len(e["body"])
print("answers: %.1f MB in all, %.1f MB kept in manifest.json" % (total / 1e6, inline / 1e6))
man = json.loads(MANIFEST.read_text(encoding="utf-8"))
man["entries"] = [e for e in man["entries"] if not str(e.get("path", "")).startswith("/api/finance/")] + list(entries.values())
man.setdefault("meta", {})["finance"] = {"recordedAt": dt.datetime.utcnow().isoformat() + "Z", "pinnedDay": today,
                                         "entries": len(entries), "source": "ESSA Finance backend, fictional sample company"}
MANIFEST.write_text(json.dumps(man, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
print("wrote", MANIFEST)
