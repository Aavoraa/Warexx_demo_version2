"""Seed database with sample textile-shop data."""
from datetime import datetime, timedelta, date
import random
import secrets
from app import db
from app.models import (
    User, Category, Product, Customer,
    Invoice, InvoiceItem, StockMovement, LoyaltyTxn
)
from app.master_categories import sync_master_categories


def seed_all():
    # Users
    admin = User(username="admin", full_name="Shop Owner", role="admin",
                 phone="+91 98765 43210", salary=50000, commission_pct=0)
    admin.set_password(secrets.token_urlsafe(24))

    mgr = User(username="manager", full_name="Priya Manager", role="manager",
               phone="+91 90000 11111", salary=25000, commission_pct=2)
    mgr.set_password(secrets.token_urlsafe(24))

    cashier1 = User(username="ravi", full_name="Ravi Kumar", role="cashier",
                    phone="+91 90000 22222", salary=15000, commission_pct=1)
    cashier1.set_password(secrets.token_urlsafe(24))

    cashier2 = User(username="meena", full_name="Meena Selvam", role="cashier",
                    phone="+91 90000 33333", salary=15000, commission_pct=1)
    cashier2.set_password(secrets.token_urlsafe(24))

    db.session.add_all([admin, mgr, cashier1, cashier2])

    # Categories are the warehouse master's — see app/master_categories.py. The
    # sample products below are filed under real codes from it, so a demo shop
    # and a real one speak the same vocabulary from the first day.
    sync_master_categories()

    # Sold by the metre off a roll; the master has no code for cut fabric, so
    # these three stay uncategorised until the shop decides on one.
    UNCODED = None

    # Products
    products_data = [
        # Sarees
        ("SR-001", "Kanchipuram Silk Saree - Red", "LADIES-SAREE", "silk", "Red", "Free", "5208", 18, 3500, 8500, 8),
        ("SR-002", "Banarasi Silk Saree - Gold", "LADIES-SAREE", "silk", "Gold", "Free", "5208", 18, 4200, 9500, 5),
        ("SR-003", "Cotton Saree - Blue Print", "LADIES-SAREE", "cotton", "Blue", "Free", "5208", 5, 450, 1200, 20),
        ("SR-004", "Chiffon Party Saree - Pink", "LADIES-SAREE", "chiffon", "Pink", "Free", "5208", 12, 1200, 2800, 15),
        ("SR-005", "Handloom Saree - Green", "LADIES-SAREE", "cotton", "Green", "Free", "5208", 5, 800, 1800, 3),
        # Salwar
        ("SL-001", "Anarkali Salwar Set - Maroon", "LADIES-CHUDITHAR", "silk blend", "Maroon", "M", "6204", 12, 1500, 3200, 10),
        ("SL-002", "Cotton Salwar Set - Yellow", "LADIES-CHUDITHAR", "cotton", "Yellow", "L", "6204", 5, 600, 1400, 12),
        ("SL-003", "Designer Lehenga - Purple", "LADIES-LEHANGA", "silk", "Purple", "Free", "6204", 18, 6000, 15000, 4),
        # Shirts
        ("SH-001", "Formal White Shirt", "MENS-SHIRT", "cotton", "White", "M", "6205", 5, 400, 850, 25),
        ("SH-002", "Formal White Shirt", "MENS-SHIRT", "cotton", "White", "L", "6205", 5, 400, 850, 25),
        ("SH-003", "Casual Checked Shirt", "MENS-SHIRT", "cotton", "Blue Check", "L", "6205", 5, 350, 750, 18),
        ("SH-004", "Linen Shirt - Beige", "MENS-SHIRT", "linen", "Beige", "XL", "6205", 12, 700, 1500, 8),
        # Kurtas
        ("KR-001", "Men's Cotton Kurta - White", "MENS-KURTA SET", "cotton", "White", "L", "6203", 5, 500, 1100, 15),
        ("KR-002", "Silk Kurta Set - Cream", "MENS-KURTA SET", "silk", "Cream", "M", "6203", 12, 1400, 3200, 6),
        # Fabric
        ("FR-001", "Cotton Fabric Roll", UNCODED, "cotton", "Assorted", "-", "5208", 5, 80, 180, 50),
        ("FR-002", "Silk Fabric Roll", UNCODED, "silk", "Assorted", "-", "5007", 12, 250, 550, 30),
        ("FR-003", "Polyester Fabric Roll", UNCODED, "polyester", "Assorted", "-", "5407", 5, 60, 140, 40),
        # Kids
        ("KD-001", "Kids Frock - Pink", "KIDS-FROCK", "cotton", "Pink", "6Y", "6209", 5, 300, 650, 20),
        ("KD-002", "Kids T-shirt Pack", "KIDS-T-SHIRT", "cotton", "Multi", "8Y", "6109", 5, 250, 500, 30),
        ("KD-003", "Boys Ethnic Set", "KIDS-KURTA SET", "cotton silk", "Blue", "10Y", "6203", 12, 800, 1800, 10),
    ]

    # Resolve the codes once. A code can come back missing when the shop runs
    # without the warehouse's export beside it; the product is still seeded, just
    # without a category, rather than failing the whole init.
    wanted = {code for _, _, code, *_ in products_data if code}
    cat_ids = {c.name: c.id for c in Category.query.filter(Category.name.in_(wanted)).all()}

    products = []
    for idx, (sku, name, code, fabric, color, size, hsn, gst, cost, sell, stock) in enumerate(products_data):
        unit = "mtr" if sku.startswith("FR-") else "pcs"
        p = Product(
            sku=sku, name=name, category_id=cat_ids.get(code),
            fabric=fabric, color=color, size=size, hsn_code=hsn, gst_rate=gst,
            cost_price=cost, selling_price=sell, stock_qty=stock, reorder_level=5, unit=unit
        )
        products.append(p)
        db.session.add(p)
    db.session.flush()
    for p in products:
        db.session.add(StockMovement(product_id=p.id, change=p.stock_qty, reason="opening", reference="seed"))

    # Customers
    customers_data = [
        ("Anitha Ramesh", "+91 99000 11111", "anitha@example.com", "12 Anna Nagar, Chennai", "", "33"),
        ("Karthik Iyer", "+91 99000 22222", "karthik@example.com", "45 T Nagar, Chennai", "", "33"),
        ("Deepa Krishnan", "+91 99000 33333", "deepa@example.com", "8 Adyar, Chennai", "", "33"),
        ("Prakash Boutique (B2B)", "+91 99000 44444", "prakash@boutique.com", "22 Ranganathan St", "33AAECC1234K1Z9", "33"),
        ("Lakshmi Devi", "+91 99000 55555", "lakshmi@example.com", "5 Mylapore, Chennai", "", "33"),
        ("Rajesh Sharma", "+91 99000 66666", "rajesh@example.com", "MG Road, Bangalore", "", "29"),
    ]
    customers = []
    for name, phone, email, addr, gstin, sc in customers_data:
        c = Customer(name=name, phone=phone, email=email, address=addr, gstin=gstin, state_code=sc)
        customers.append(c)
        db.session.add(c)


    db.session.commit()

    # Sample invoices over the last 25 days
    cashiers = [cashier1, cashier2, mgr]
    for days_ago in range(25, 0, -1):
        for _ in range(random.randint(2, 6)):
            invoice_date = datetime.utcnow() - timedelta(days=days_ago, hours=random.randint(9, 20))
            cust = random.choice(customers + [None])
            cashier = random.choice(cashiers)

            inv = Invoice(
                invoice_number=f"INV-{Invoice.query.count() + 1:06d}",
                customer_id=cust.id if cust else None,
                cashier_id=cashier.id,
                invoice_date=invoice_date,
                payment_method=random.choice(["cash", "cash", "card", "upi"]),
                is_interstate=(cust and cust.state_code != "33") if cust else False,
            )
            db.session.add(inv)
            db.session.flush()

            subtotal, tax = 0.0, 0.0
            n_items = random.randint(1, 4)
            picked = random.sample(products, n_items)
            for p in picked:
                qty = random.choice([1, 1, 1, 2, 3])
                if p.stock_qty < qty:
                    continue
                line = qty * p.selling_price
                t = round(line * p.gst_rate / 100.0, 2)
                db.session.add(InvoiceItem(
                    invoice_id=inv.id, product_id=p.id, quantity=qty,
                    unit_price=p.selling_price, gst_rate=p.gst_rate,
                    line_total=line, tax_amount=t
                ))
                p.stock_qty -= qty
                db.session.add(StockMovement(
                    product_id=p.id, change=-qty, reason="sale", reference=inv.invoice_number
                ))
                subtotal += line
                tax += t

            if inv.is_interstate:
                inv.igst = round(tax, 2)
            else:
                inv.cgst = round(tax / 2, 2)
                inv.sgst = round(tax / 2, 2)
            inv.subtotal = round(subtotal, 2)
            inv.total = round(subtotal + tax, 2)
            if cust and inv.total >= 500:
                earned = round(inv.total * 0.01, 2)
                inv.loyalty_earned = earned
                cust.loyalty_points = (cust.loyalty_points or 0) + earned
                cust.total_spent = (cust.total_spent or 0) + inv.total
                db.session.add(LoyaltyTxn(
                    customer_id=cust.id, points=earned, reason="earn", invoice_id=inv.id
                ))
            elif cust:
                cust.total_spent = (cust.total_spent or 0) + inv.total


    db.session.commit()
