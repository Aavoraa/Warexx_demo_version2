"""Record the CRM module's pages for the demo.

    <essa>/backend/.venv/Scripts/python.exe demo-data/tools/record_crm.py --essa D:/ESSA-Next

The CRM came from ESSA, where it is part of the Store — server-rendered pages of
the shop app ("Textile Retail Shop"), shown in a frame at /pos/... . This builds a
throwaway shop database in a temp folder, fills it with a FICTIONAL business's
customers and their CRM records (orders, loyalty, gift vouchers, coupons, price
lists, feedback, leads, walk-ins, sign-ups), serves it through ESSA's own /pos
mount in-process, and records every CRM page — and the record pages each one links
to — into demo-data/recorded/manifest.json. Any /pos CRM entries already there are
replaced; the demo's own Store pages and stylesheet are left alone.

It never opens a real database, and nobody in it is a real person.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import tempfile
from html import unescape
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE.parent / "recorded" / "manifest.json"

ap = argparse.ArgumentParser()
ap.add_argument("--essa", required=True)
ap.add_argument("--dry", action="store_true")
ap.add_argument("--per-section", type=int, default=12, help="record pages linked from each CRM screen, up to this many")
args = ap.parse_args()
ROOT = pathlib.Path(args.essa).resolve()
SHOP = ROOT / "Textile Retail Shop"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

tmp = tempfile.mkdtemp(prefix="warexx-crm-demo-")
WH_DB = os.path.join(tmp, "warehouse.db").replace(os.sep, "/")
SHOP_DB = os.path.join(tmp, "shop.db").replace(os.sep, "/")
for leak in ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL",
             "SHOP_DB_SCHEMA", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ESSA_SUPERBOSS_PASSWORD", "BLOB_READ_WRITE_TOKEN",
             "MESSAGE_WEBHOOK_URL", "SECRET_KEY"):
    os.environ.pop(leak, None)
os.environ.update({
    "ESSA_STATE_DIR": tmp, "ESSA_POS_DB": SHOP_DB, "ESSA_WAREHOUSE_DB": os.path.join(tmp, "no-warehouse-here.db"),
    "SHOP_NAME": "Sample Company", "SECRET_KEY": "demo-recording-only-" + os.urandom(8).hex(),
    "ESSA_COMPANY_NAME": "Sample Company Private Limited", "ESSA_COMPANY_GSTIN": "33AAAAA0000A1Z5",
})
# seed_live.py: seed the database it names and record nothing
LIVE = os.environ.get("WAREXX_SEED_LIVE") == "1"
LOGIN_PASSWORD = os.environ["ESSA_SUPERADMIN_PASSWORD"] if LIVE else "super@123"
if LIVE:
    SHOP_DB, WH_DB = os.environ["WAREXX_SEED_SHOP_URL"], os.environ["WAREXX_SEED_WH_URL"]
    if SHOP_DB.startswith("postgresql"):
        os.environ["SHOP_DB_SCHEMA"] = "shop"
    os.environ["ESSA_POS_DB"] = os.environ["WAREXX_SEED_SHOP_URL"].replace("sqlite:///", "") if os.environ["WAREXX_SEED_SHOP_URL"].startswith("sqlite") else ""
random.seed(20261005)
NOW = dt.datetime.now()
TODAY = NOW.date()

# ===========================================================================
# the shop's database: its own sample seed, then a fictional business's CRM
os.environ["DATABASE_URL"] = SHOP_DB if LIVE else "sqlite:///" + SHOP_DB
sys.path.insert(0, str(SHOP))
import app as shop_pkg                                              # noqa: E402
from app import db                                                  # noqa: E402
from app import models as SM                                        # noqa: E402
from app.seed import seed_all                                       # noqa: E402

shop_app = shop_pkg.create_app()
with shop_app.app_context():
    db.create_all()
    seed_all()
    users = SM.User.query.all()
    staff = [u for u in users if u.role in ("cashier", "manager")] or users
    boss = next((u for u in users if u.role == "admin"), users[0])
    products = SM.Product.query.all()

    FIRST = ["Aarthi", "Bharath", "Chitra", "Dinesh", "Ezhil", "Fathima", "Ganesh", "Harini", "Ilango", "Janani",
             "Kavin", "Lavanya", "Manoj", "Nila", "Oviya", "Pradeep", "Revathi", "Sanjay", "Tharun", "Uma",
             "Vasanth", "Yamini", "Zara", "Bala", "Geetha", "Hari", "Indira", "Jeeva", "Keerthi", "Mohan"]
    LAST = ["S", "R", "K", "M", "P", "V", "N", "A"]
    CITIES = ["Coimbatore", "Tiruppur", "Erode", "Salem", "Madurai", "Chennai"]
    AREAS = ["RS Puram", "Gandhipuram", "Peelamedu", "Saibaba Colony", "Town Hall", "Avinashi Road"]
    CATS = ["Regular", "Gold", "Platinum", "Wholesale"]
    custs = []
    for i, fn in enumerate(FIRST):
        c = SM.Customer(name=f"{fn} {random.choice(LAST)}", phone="90000%05d" % (20000 + i * 37),
                        email=f"{fn.lower()}@sample-customer.example", city=random.choice(CITIES),
                        area=random.choice(AREAS), category=random.choice(CATS), gender=random.choice(["F", "M"]),
                        code="CUS%04d" % (100 + i), loyalty_card="LC%06d" % (500000 + i * 13),
                        dob=dt.date(1975 + i % 25, 1 + i % 12, 1 + (i * 7) % 27),
                        created_at=NOW - dt.timedelta(days=random.randint(30, 400)), source="counter")
        custs.append(c)
        db.session.add(c)
    db.session.flush()
    allc = SM.Customer.query.all()

    # nine months of bills, so the analyser has frequency, recency and lapsed people
    n = SM.Invoice.query.count()
    for c in allc:
        visits = random.choice([0, 1, 2, 3, 4, 6, 9])
        for _ in range(visits):
            n += 1
            when = NOW - dt.timedelta(days=random.randint(1, 270), hours=random.randint(1, 9))
            inv = SM.Invoice(invoice_number=f"INV-{n:06d}", customer_id=c.id, cashier_id=random.choice(staff).id,
                             invoice_date=when, payment_method=random.choice(["cash", "upi", "card"]))
            db.session.add(inv)
            db.session.flush()
            sub = 0.0
            for p in random.sample(products, random.randint(1, 3)):
                q = random.choice([1, 1, 2])
                line = q * p.selling_price
                sub += line
                db.session.add(SM.InvoiceItem(invoice_id=inv.id, product_id=p.id, quantity=q, unit_price=p.selling_price,
                                              gst_rate=p.gst_rate, line_total=line, tax_amount=round(line * p.gst_rate / 100, 2)))
            inv.subtotal = round(sub, 2)
            inv.total = round(sub * 1.05, 2)
            earned = round(inv.total * 0.01, 2)
            inv.loyalty_earned = earned
            c.loyalty_points = (c.loyalty_points or 0) + earned
            c.total_spent = (c.total_spent or 0) + inv.total
            db.session.add(SM.LoyaltyTxn(customer_id=c.id, points=earned, reason="earn", invoice_id=inv.id))
    db.session.flush()
    bills = SM.Invoice.query.filter(SM.Invoice.customer_id.isnot(None)).all()

    # price lists
    pcs = [SM.PriceCategory(name="Wholesale", discount_pct=12), SM.PriceCategory(name="Staff", discount_pct=20),
           SM.PriceCategory(name="Bulk order (50+)", discount_pct=8)]
    db.session.add_all(pcs)
    db.session.flush()
    for p in products[:10]:
        db.session.add(SM.PriceListItem(category_id=pcs[0].id, design_no=p.sku, price=round(p.selling_price * 0.88)))
        db.session.add(SM.PriceListItem(category_id=pcs[1].id, design_no=p.sku, price=round(p.selling_price * 0.80)))
    for c in custs[:4]:
        c.price_category_id = pcs[0].id
        c.customer_type = "wholesale"

    # coupons
    camps = [SM.CouponCampaign(name="Festival ₹200 off", kind="amount", value=200, min_bill=2000, valid_days=30, created_by_id=boss.id),
             SM.CouponCampaign(name="10% for coming back", kind="percent", value=10, min_bill=1000, max_discount=500,
                               valid_days=45, issue_at_settlement=True, settlement_min_bill=3000, created_by_id=boss.id),
             SM.CouponCampaign(name="Birthday treat", kind="amount", value=300, min_bill=1500, valid_days=15, created_by_id=boss.id)]
    db.session.add_all(camps)
    db.session.flush()
    for i, c in enumerate(random.sample(allc, 18)):
        camp = camps[i % 3]
        db.session.add(SM.Coupon(code="CPN%05d" % (10000 + i * 7), campaign_id=camp.id, value=camp.value, customer_id=c.id,
                                 valid_from=TODAY - dt.timedelta(days=10), valid_to=TODAY + dt.timedelta(days=camp.valid_days),
                                 uses_allowed=1, issued_via=random.choice(["manual", "settlement"]), created_by_id=boss.id,
                                 created_at=NOW - dt.timedelta(days=random.randint(1, 20))))

    # gift vouchers
    for i in range(6):
        c = random.choice(allc)
        b = SM.GiftVoucherBatch(number="GVB-%04d" % (i + 1), mode="direct", customer_id=c.id,
                                issued_on=TODAY - dt.timedelta(days=5 * i + 2), valid_from=TODAY - dt.timedelta(days=5 * i + 2),
                                valid_to=TODAY + dt.timedelta(days=180), method=random.choice(["cash", "upi", "card"]),
                                issued_by_id=boss.id, created_by_id=boss.id, received_by=c.name)
        db.session.add(b)
        db.session.flush()
        total = 0
        for j in range(random.randint(1, 4)):
            v = random.choice([500, 1000, 2000])
            total += v
            db.session.add(SM.GiftVoucher(code="GV%06d" % (200000 + i * 10 + j), batch_id=b.id, value=v,
                                          given_to=random.choice(FIRST) + " " + random.choice(LAST)))
        b.amount = total

    # customer orders
    for i in range(14):
        c = random.choice(custs)
        od = TODAY - dt.timedelta(days=random.randint(0, 40))
        o = SM.CustomerOrder(number="CO-%05d" % (i + 1), customer_id=c.id, order_date=od,
                             delivery_date=od + dt.timedelta(days=random.randint(5, 25)), contact_no=c.phone, city=c.city,
                             category=random.choice(["Bridal", "Uniform", "Tailoring", "Bulk"]),
                             received_by_id=random.choice(staff).id, created_by_id=boss.id,
                             status=random.choice(["new", "new", "confirmed", "in_progress", "ready", "delivered", "cancelled"]),
                             notes=random.choice(["", "Call before delivery", "Match blouse colour", "Gift wrap"]))
        db.session.add(o)
        db.session.flush()
        for p in random.sample(products, random.randint(1, 3)):
            db.session.add(SM.CustomerOrderLine(order_id=o.id, description=p.name, design_no=p.sku, size=p.size,
                                                color=p.color, qty=random.choice([1, 2, 5]), rate=p.selling_price))

    # feedback
    qs = [SM.FeedbackQuestion(text="How was our staff's service?", kind="rating", required=True, sort_order=1),
          SM.FeedbackQuestion(text="How would you rate the collection?", kind="rating", sort_order=2),
          SM.FeedbackQuestion(text="Did you find what you came for?", kind="yesno", sort_order=3),
          SM.FeedbackQuestion(text="Anything we should do better?", kind="text", sort_order=4)]
    db.session.add_all(qs)
    db.session.flush()
    COMMENTS = ["Very helpful staff", "Loved the silk collection", "Billing was slow on Saturday", "",
                "Parking is difficult", "Great festival offers", "", "Trial rooms need more mirrors"]
    for inv in random.sample(bills, min(40, len(bills))):
        r = random.choices([5, 4, 3, 2, 1], [40, 32, 15, 8, 5])[0]
        f = SM.CustomerFeedback(customer_id=inv.customer_id, invoice_id=inv.id, rating=r, comments=random.choice(COMMENTS),
                                source=random.choice(["counter", "link", "qr"]),
                                created_at=inv.invoice_date + dt.timedelta(hours=2))
        db.session.add(f)
        db.session.flush()
        db.session.add(SM.FeedbackAnswer(feedback_id=f.id, question_id=qs[0].id, value=str(r), score=r))
        r2 = max(1, min(5, r + random.choice([-1, 0, 0, 1])))
        db.session.add(SM.FeedbackAnswer(feedback_id=f.id, question_id=qs[1].id, value=str(r2), score=r2))
        yn = "yes" if r >= 3 else random.choice(["yes", "no"])
        db.session.add(SM.FeedbackAnswer(feedback_id=f.id, question_id=qs[2].id, value=yn, score=5 if yn == "yes" else 0))

    # leads
    INTEREST = ["Bridal silk sarees", "School uniforms for 120 students", "Corporate gifting — 40 shawls",
                "Kurta sets for a family function", "Wholesale cotton sarees", "Lehenga for engagement"]
    for i in range(12):
        st = random.choice(["new", "contacted", "follow_up", "won", "lost"])
        ld = SM.Lead(name=random.choice(FIRST) + " " + random.choice(LAST), phone="90000%05d" % (60000 + i * 41),
                     city=random.choice(CITIES), source=random.choice(["walk_in", "phone", "whatsapp", "website", "referral"]),
                     interest=random.choice(INTEREST), expected_value=random.choice([8000, 15000, 45000, 120000]),
                     status=st, next_follow_up=TODAY + dt.timedelta(days=random.randint(-3, 10)),
                     assigned_to_id=random.choice(staff).id, created_by_id=boss.id,
                     lost_reason="Bought elsewhere" if st == "lost" else None,
                     created_at=NOW - dt.timedelta(days=random.randint(1, 30)))
        db.session.add(ld)
        db.session.flush()
        db.session.add(SM.LeadActivity(lead_id=ld.id, kind="call", note="Called; asked for photos of the range", by_id=ld.assigned_to_id))

    # walk-ins today (Customer Divert)
    LOOK = ["Sarees", "Kurtis", "Men's shirts", "Kids wear", "Bridal", "Dress material"]
    for i in range(8):
        c = random.choice(allc + [None, None])
        db.session.add(SM.CustomerVisit(customer_id=c.id if c else None, name=c.name if c else random.choice(FIRST),
                                        phone=c.phone if c else None, looking_for=random.choice(LOOK),
                                        party_size=random.choice([1, 2, 3]), staff_id=random.choice(staff).id,
                                        status=random.choice(["waiting", "attending", "attending", "billed", "left"]),
                                        diverted_by_id=boss.id, created_at=NOW - dt.timedelta(minutes=15 * i + 5)))

    # self-registrations waiting for approval
    for i in range(4):
        fn = random.choice(FIRST)
        db.session.add(SM.Customer(name=f"{fn} {random.choice(LAST)}", phone="90000%05d" % (80000 + i * 19),
                                   email=f"{fn.lower()}.{i}@sample-customer.example", city=random.choice(CITIES),
                                   source="self", awaiting_approval=True, created_at=NOW - dt.timedelta(hours=3 * i + 1)))

    # wishes and campaign messages queued (Send SMS)
    for c in random.sample(custs, 8):
        db.session.add(SM.ScheduledMessage(customer_id=c.id, phone=c.phone, kind=random.choice(["birthday", "custom"]),
                                           body=f"Dear {c.name.split()[0]}, our festival collection is in. Visit us this week!",
                                           scheduled_for=NOW + dt.timedelta(days=random.randint(0, 5)),
                                           status=random.choice(["queued", "queued", "sent"]), created_by_id=boss.id))
    # promotions the till applies by itself (built the way the admin form builds them)
    def scheme(name, code, buys, gets, kind="buy_get_free", days=(-10, 30)):
        sc = SM.PromotionScheme(name=name, code=code, scheme_type=kind, active=True, priority=100, stackable=False,
                                reward_selection="cheapest", out_of_stock="block", return_policy="reclaim_value",
                                start_date=TODAY + dt.timedelta(days=days[0]), end_date=TODAY + dt.timedelta(days=days[1]))
        db.session.add(sc)
        db.session.flush()
        for i, (qty, prods) in enumerate(buys):
            c = SM.PromotionCondition(scheme_id=sc.id, min_qty=qty, sort_order=i)
            db.session.add(c)
            db.session.flush()
            for pr in prods:
                db.session.add(SM.PromotionConditionItem(condition_id=c.id, product_id=pr.id))
        for i, (qty, value, prods) in enumerate(gets):
            r = SM.PromotionReward(scheme_id=sc.id, qty=qty, value=value, sort_order=i)
            db.session.add(r)
            db.session.flush()
            for pr in prods:
                db.session.add(SM.PromotionRewardItem(reward_id=r.id, product_id=pr.id))
    by = {p.sku: p for p in products}
    shirts = [by[k] for k in ("SH-001", "SH-002", "SH-003", "SH-004") if k in by]
    kids = [by[k] for k in ("KD-001", "KD-002", "KD-003") if k in by]
    sarees = [by[k] for k in ("SR-003", "SR-004", "SR-005") if k in by]
    fabric = [by[k] for k in ("FR-001", "FR-002") if k in by]
    if shirts:
        scheme("Buy 2 shirts, get 1 free", "PROMO-000001", [(2, shirts)], [(1, 100, shirts)])
    if kids:
        scheme("Kids wear: buy 3, get the cheapest free", "PROMO-000002", [(3, kids)], [(1, 100, kids)])
    if sarees and fabric:
        scheme("Saree + blouse fabric at 50% off", "PROMO-000003", [(1, sarees)], [(1, 50, fabric)], days=(-3, 12))
    if sarees:
        scheme("Pongal preview: 2 sarees, 1 free", "PROMO-000004", [(2, sarees)], [(1, 100, sarees)], days=(40, 75))

    db.session.commit()
    print("shop: %d customers, %d bills with a customer" % (SM.Customer.query.count(), len(bills)))

for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.") or k == "config"]:
    del sys.modules[name]
sys.path.remove(str(SHOP))

# ===========================================================================
# ESSA's backend, which mounts that shop at /pos and signs it in as the admin
# ESSA reads ESSA_DATABASE_URL first; the shop it mounts reads DATABASE_URL — one file each
os.environ["ESSA_DATABASE_URL"] = WH_DB if LIVE else "sqlite:///" + WH_DB
os.environ["DATABASE_URL"] = SHOP_DB if LIVE else "sqlite:///" + SHOP_DB
sys.path.insert(0, str(ROOT))
from backend.app import models                                      # noqa: E402
from backend.app.database import engine, DB_URL                     # noqa: E402
from backend.app.main import app                                    # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

if not LIVE and (not DB_URL.startswith("sqlite") or tmp.replace(os.sep, "/") not in DB_URL):
    sys.exit("refusing to run: the database is not the throwaway one (%s)" % DB_URL)
models.Base.metadata.create_all(bind=engine)
client = TestClient(app)
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                  json={"username": "superadmin", "password": LOGIN_PASSWORD}).json()["token"]
H = {"Authorization": "Bearer " + tok}
_probe = client.get("/pos/", headers=H)
if _probe.status_code == 503:
    from backend.app import main as _main
    sys.exit("the shop did not load in ESSA's /pos mount: %s" % getattr(_main, "_pos_error", "?"))

# The CRM's screens, as ESSA's menu lists them (path under /pos)
CRM = [("pos:customers", "/customers/"), ("pos:crm_orders", "/orders/"), ("pos:loyalty", "/loyalty/"),
       ("pos:gift_vouchers", "/gift-vouchers/"), ("pos:gift_coupons", "/coupons/"), ("pos:promotions", "/promotions/"),
       ("pos:price_list", "/price-list/"), ("pos:crm_analyser", "/crm-analyser/"), ("pos:send_sms", "/send-sms/"),
       ("pos:feedback_dashboard", "/crm-feedback/"), ("pos:feedback_questions", "/crm-feedback/questions"),
       ("pos:feedback_report", "/crm-feedback/report"), ("pos:feedback_form", "/crm-feedback/form"),
       ("pos:customer_divert", "/customer-divert/"), ("pos:leads", "/leads/"), ("pos:self_registration", "/registration/")]
# Customers and Promotions are recorded too (replacing the demo's earlier Store pages for
# them), so every CRM screen shows the same customers.
KEEP = set()

entries, problems, statics = {}, [], set()
SKIP_LINK = re.compile(r"/(export|print|delete|cancel|logout|csv|xlsx|pdf)\b|\.(csv|xlsx|pdf)$", re.I)


def fetch(path):
    r = client.get("/pos" + path, headers=H, follow_redirects=False)
    return r


def rec(path, wh="1"):
    sep = "&" if "?" in path else "?"
    full = "/pos" + path
    r = fetch(path)
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        problems.append("%s -> %s %s" % (full, r.status_code, r.headers.get("location", "")))
        return None
    # The page's script comments name the app it was written for ("served inside the
    # Essa backend"); in the demo that app is WAREXX. Header and setting names
    # (X-Essa-…, essa_…) are code, not words, and are left alone.
    body = re.sub(r"(?<![-\w])(ESSA|Essa|essa)(?![-\w])", "WAREXX", r.text)
    # links the shop wrote out in full carry the recorder's own host; on the demo they are its own pages
    body = body.replace("http://testserver", "")
    u = urlsplit(full)
    search = ("?" + u.query + "&wh=" + wh) if u.query else ("?wh=" + wh)
    key = "GET - " + u.path + search
    entries[key] = {"key": key, "method": "GET", "wh": "-", "path": u.path, "search": search, "status": 200,
                    "type": "text/html; charset=utf-8", "body": body}
    for s in re.findall(r'(?:src|href)="(/pos/static/[^"?#]+)', body):
        statics.add(s)
    return body


recorded_roots = 0
for key, root in ([] if LIVE else CRM):
    if root in KEEP:
        continue
    body = rec(root)
    if body is None:
        continue
    recorded_roots += 1
    base = "/pos" + root.split("?")[0]
    base = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
    links = []
    for href in re.findall(r'href="([^"#]+)"', body):
        href = unescape(href)
        if not href.startswith(base) or href.rstrip("/") == ("/pos" + root).rstrip("/") or SKIP_LINK.search(href):
            continue
        if href not in links:
            links.append(href)
    # every record's own page (a customer, an order, a lead…), so the one clicked is the one shown;
    # the screen's other links (new, edit, tabs) up to --per-section
    own = [h for h in links if re.fullmatch(re.escape(base) + r"\d+", h.split("?")[0])]
    rest = [h for h in links if h not in own][: args.per_section]
    for href in own + rest:
        rec(href[len("/pos"):])

# The Promotions screen's own kind of scheme, saved the way its Save button saves them.
vf, vt = (TODAY - dt.timedelta(days=7)).isoformat(), (TODAY + dt.timedelta(days=45)).isoformat()
for body in [
    {"name": "Festive 10% off bills above ₹3,000", "applicable": "same_bill", "mode": "bill_value",
     "benefits": [{"mode": "discount_pct", "price_from": 3000, "value": 10}]},
    {"name": "₹250 off on bills of ₹5,000 and more", "applicable": "same_bill", "mode": "bill_value",
     "benefits": [{"mode": "fixed", "price_from": 5000, "value": 250, "flat": True}]},
    {"name": "Buy 2, get 1 free", "applicable": "same_bill", "mode": "bill_item",
     "benefits": [{"mode": "free_item", "qty_from": 2, "value": 1}]},
    {"name": "₹200 coupon for the next bill (on ₹2,500+)", "applicable": "next_bill", "mode": "bill_value",
     "benefits": [{"mode": "coupon", "price_from": 2500, "value": 200}]},
    {"name": "Double reward points at settlement", "applicable": "settlement", "mode": "bill_value",
     "benefits": [{"mode": "points", "price_from": 1000, "value": 2}]},
]:
    body.update({"valid_from": vf, "valid_to": vt, "active": True})
    r = client.post("/pos/promotions/api/schemes", headers=H, json=body)
    if r.status_code != 200:
        problems.append("save scheme %r -> %s %s" % (body["name"], r.status_code, r.text[:160]))

if LIVE:
    if problems:
        sys.exit("\n".join(problems))
    sys.exit(0)

# The Promotions screen draws its schemes from JSON it fetches after the page loads.
def rec_json(path, search=""):
    r = client.get("/pos" + path + search, headers=H)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        problems.append("%s%s -> %s" % (path, search, r.status_code))
        return None
    def scrub(v):   # the shop's master brand list includes ESSA's own; not in the demo
        if isinstance(v, list):
            return [scrub(x) for x in v if not (isinstance(x, str) and re.search(r"(?<![-\w])essa(?![-\w])", x, re.I))]
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        return v
    data = scrub(r.json())
    key = "GET - /pos" + path + search
    entries[key] = {"key": key, "method": "GET", "wh": "-", "path": "/pos" + path, "search": search, "status": 200,
                    "type": "application/json", "body": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}
    if path.endswith("/api/schemes"):
        print("schemes listing:", {k: (len(v) if isinstance(v, list) else v) for k, v in data.items()} if isinstance(data, dict) else len(data))
    return data


listing = rec_json("/promotions/api/schemes", "?q=")
rec_json("/promotions/api/schemes")
rec_json("/promotions/api/scheme-values")
rec_json("/promotions/api/products", "?q=")
rows = listing if isinstance(listing, list) else (listing or {}).get("schemes") or (listing or {}).get("rows") or []
for row in rows:
    if isinstance(row, dict) and row.get("id"):
        rec_json("/promotions/api/schemes/%d" % row["id"])

# static files the CRM pages use that the demo does not have yet (never its app.css)
man = json.loads(MANIFEST.read_text(encoding="utf-8"))
have = {e["path"] for e in man["entries"]}
for s in sorted(statics):
    if s in have or s.endswith("/app.css"):
        continue
    r = client.get(s, headers=H)
    if r.status_code == 200 and r.headers.get("content-type", "").split(";")[0] in ("text/css", "application/javascript", "text/javascript"):
        entries["GET - " + s] = {"key": "GET - " + s, "method": "GET", "wh": "-", "path": s, "search": "", "status": 200,
                                 "type": r.headers["content-type"], "body": r.text}
    else:
        problems.append("static %s -> %s (%s)" % (s, r.status_code, r.headers.get("content-type")))

print("CRM screens recorded: %d, pages in all: %d" % (recorded_roots, len(entries)))
if problems:
    print("\n%d did not record:" % len(problems))
    for p in problems[:50]:
        print("  ", p)

leaks = [k for k, e in entries.items() if re.search(r"\bessa\b|33AADCE6591N1Z7", re.sub(r"(?i)x-essa-[a-z-]+|essa_[a-z_]+", "", e["body"]), re.I)]
if leaks:
    print("\nESSA's name appears in %d pages, e.g. %s" % (len(leaks), leaks[:5]))
    for k in leaks[:3]:
        b = entries[k]["body"]
        for mm in re.finditer(r"(?i)\bessa\b", b):
            print("   …", b[max(0, mm.start() - 70): mm.end() + 70].replace("\n", " "))
            break
    if not args.dry:
        sys.exit("refusing to write")

if args.dry:
    sys.exit(0)
# Each page goes in recorded/bodies/ (fetched only when it is opened), not inline in
# manifest.json, which every visitor downloads on every page load.
BODIES = MANIFEST.parent / "bodies"
if os.name == "nt":   # a deep checkout can pass Windows' 260-character path limit
    BODIES = pathlib.Path(r"\\?" + "\\" + str(BODIES.resolve()))
for old in BODIES.glob("crm-*"):
    old.unlink()
for e in entries.values():
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (e["path"] + e["search"]).strip("/")).strip("-")[:120]
    ext = ".html" if e["type"].startswith("text/html") else ".json" if "json" in e["type"] else ".css" if "css" in e["type"] else ".js"
    name = "crm-" + slug + ext
    (BODIES / name).write_text(e.pop("body"), encoding="utf-8")
    e["file"] = name
crm_paths = {e["path"] for e in entries.values()}
man["entries"] = [e for e in man["entries"] if e["path"] not in crm_paths] + list(entries.values())
man.setdefault("meta", {})["crm"] = {"recordedAt": dt.datetime.utcnow().isoformat() + "Z", "pinnedDay": TODAY.isoformat(),
                                     "entries": len(entries), "source": "ESSA shop CRM pages, fictional sample business"}
MANIFEST.write_text(json.dumps(man, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
print("wrote", MANIFEST)
