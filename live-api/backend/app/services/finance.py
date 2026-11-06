"""
Finance — the books, and every report that reads them.

ONE ENGINE. A Voucher with balanced VoucherLines is the only thing written;
the Daybook, Trial Balance, Trading Account, P&L, Balance Sheet, Payable,
Receivable, TDS, Analyser, BRS and Loan screens are all sums over those lines.
Nothing here keeps a running balance that could disagree with the lines.

NOTHING KEYED TWICE. What the warehouse and the shop already record is turned
into vouchers by `sync_books`, idempotently, keyed on (source, source_id):

  * a posted GRN        -> Purchase:    Dr Purchase Accounts + Input GST, Cr supplier
  * a supplier payment  -> Payment:     Dr supplier, Cr Bank/Cash (+ discount, TDS)
  * a posted debit note -> Debit Note:  Dr supplier, Cr Purchase Returns + Input GST
  * a day's till sales  -> Sales:       Dr Cash / Card / UPI, Cr Sales + Output GST
  * a day's till returns-> Credit Note: Dr Sales Returns + Output GST, Cr Cash

Those vouchers are read-only on Voucher Entry: correcting one is done where the
document lives (unpost the GRN, and the purchase voucher goes with it).

Dates are ISO `YYYY-MM-DD` throughout; the screens show DD-MM-YYYY.
"""
import datetime as dt
import re
import urllib.request
from collections import defaultdict
from xml.sax.saxutils import escape

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from .. import models
from . import dates, businesses, pos_sales

# ---------------------------------------------------------------------------
#  The chart of accounts
# ---------------------------------------------------------------------------
#: group -> (nature, statement section). Nature is which side a balance
#: normally sits: asset/expense debit, liability/income credit. Section decides
#: where it lands: trading (gross profit), pl (net profit), or bs (balance sheet).
GROUPS = {
    "Capital":                      ("liability", "bs"),
    "Loans (Liability)":            ("liability", "bs"),
    "Liabilities":                  ("liability", "bs"),
    "Current Liabilities":          ("liability", "bs"),
    "Sundry Creditors":             ("liability", "bs"),
    "Tax & Duties":                 ("liability", "bs"),
    "Fixed Asset":                  ("asset", "bs"),
    "Investments":                  ("asset", "bs"),
    "Current Asset":                ("asset", "bs"),
    "Loans & Advances (Asset)":     ("asset", "bs"),
    "Deposits (Asset)":             ("asset", "bs"),
    "Cash-In-Hand":                 ("asset", "bs"),
    "Petty Cash":                   ("asset", "bs"),
    "Bank Account":                 ("asset", "bs"),
    "Stock-In-Hand":                ("asset", "bs"),
    "Sundry Debtors":               ("asset", "bs"),
    "Sales":                        ("income", "trading"),
    "Direct Income":                ("income", "trading"),
    "Petty Cash Direct Income":     ("income", "trading"),
    "Purchase":                     ("expense", "trading"),
    "Direct Expense":               ("expense", "trading"),
    "Petty Cash Direct Expense":    ("expense", "trading"),
    "Indirect Incomes":             ("income", "pl"),
    "Petty Cash In-Direct Income":  ("income", "pl"),
    "Indirect Expenses":            ("expense", "pl"),
    "Petty Cash In-Direct Expense": ("expense", "pl"),
    "Rounding":                     ("expense", "pl"),
}
GROUP_NAMES = list(GROUPS)
PETTY_GROUPS = ["Petty Cash Direct Expense", "Petty Cash In-Direct Expense",
                "Petty Cash Direct Income", "Petty Cash In-Direct Income"]
CASH_GROUPS = {"Cash-In-Hand", "Petty Cash"}
LOAN_GROUPS = {"Loans (Liability)", "Loans & Advances (Asset)"}

#: Manage Ledger's Filter — the flag tags plus the questions asked of fields.
LEDGER_TAGS = [
    ("managable_asset", "Manageable Assets"), ("inventory_bank", "Inventory Bank"),
    ("pl_expense", "P&L Expense"), ("daily_expense", "Daily Expense"),
    ("weekly_expense", "Weekly Expense"), ("monthly_expense", "Monthly Expense"),
    ("yearly_expense", "Yearly Expense"), ("asset_depreciation", "Asset Depreciation"),
]
LEDGER_FILTERS = [
    ("gst_available", "GST Available"), ("pan_available", "PAN Available"),
    ("gst_missing", "GST Missing"), ("pan_missing", "PAN Missing"),
    ("billwise_on", "Billwise Enabled"), ("billwise_off", "Billwise Disabled"),
    ("tds", "TDS Ledgers"),
] + LEDGER_TAGS

VTYPES = ["Payment", "Receipt", "Contra", "Journal", "Sales", "Purchase",
          "Credit Note", "Debit Note", "Petty Cash"]
VPREFIX = {"Payment": "PAY", "Receipt": "RCT", "Contra": "CON", "Journal": "JRN",
           "Sales": "SAL", "Purchase": "PUR", "Credit Note": "CRN",
           "Debit Note": "DBN", "Petty Cash": "PTY"}
MODES = ["Cash", "Cheque", "NEFT", "RTGS", "UPI", "Card"]

#: The ledgers the auto-postings need. Created on first use, never deleted.
SYSTEM = {
    "cash":        ("Cash", "Cash-In-Hand"),
    "bank":        ("Bank (Default)", "Bank Account"),
    "card":        ("Card Settlement", "Current Asset"),
    "upi":         ("UPI Settlement", "Current Asset"),
    "purchase":    ("Purchase Accounts", "Purchase"),
    "purchase_ret": ("Purchase Returns", "Purchase"),
    "sales":       ("Sales Accounts", "Sales"),
    "sales_ret":   ("Sales Returns", "Sales"),
    "input_gst":   ("Input GST", "Tax & Duties"),
    "output_gst":  ("Output GST", "Tax & Duties"),
    "tds":         ("TDS Payable", "Tax & Duties"),
    "tds_recv":    ("TDS Receivable", "Current Asset"),
    "disc_recv":   ("Discount Received", "Indirect Incomes"),
    "disc_allow":  ("Discount Allowed", "Indirect Expenses"),
    "freight":     ("Freight Inward", "Direct Expense"),
    "commission":  ("Agent Commission", "Direct Expense"),
    "rounding":    ("Rounding Off", "Rounding"),
}


def r2(v):
    return round(float(v or 0), 2)


def iso(value, fallback=None):
    d = dates.parse(value) if value else None
    if d:
        return d.isoformat()
    if isinstance(fallback, (dt.date, dt.datetime)):
        return (fallback.date() if isinstance(fallback, dt.datetime) else fallback).isoformat()
    return fallback or dt.date.today().isoformat()


def today():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------------------
#  Ledgers
# ---------------------------------------------------------------------------
def system_ledger(db: Session, key: str) -> models.Ledger:
    name, group = SYSTEM[key]
    L = (db.query(models.Ledger)
           .filter(models.Ledger.source == "system", models.Ledger.alias == key).first())
    if not L:
        L = models.Ledger(name=name, group=group, alias=key, source="system", tags=[])
        db.add(L)
        db.flush()
    return L


def ensure_defaults(db: Session):
    for k in SYSTEM:
        system_ledger(db, k)


def party_ledger(db: Session, source: str, src) -> models.Ledger:
    """The ledger mirroring a supplier, agent or transporter — created the first
    time money is owed to or paid to them."""
    L = (db.query(models.Ledger)
           .filter(models.Ledger.source == source, models.Ledger.source_id == src.id).first())
    if L:
        return L
    group = "Sundry Creditors"
    L = models.Ledger(name=src.name, group=group, source=source, source_id=src.id,
                      gstin=getattr(src, "gstin", None), pan=getattr(src, "pan", None),
                      state=getattr(src, "state", None), phone=getattr(src, "phone", None),
                      address=getattr(src, "address", None), billwise=True, tags=[])
    db.add(L)
    db.flush()
    return L


def ledger_out(L: models.Ledger, bal=None) -> dict:
    d = {"id": L.id, "name": L.name, "alias": L.alias if L.source != "system" else None,
         "group": L.group, "business_id": L.business_id, "location": L.location,
         "gstin": L.gstin, "pan": L.pan, "state": L.state, "address": L.address,
         "phone": L.phone, "email": L.email, "bank_account": L.bank_account,
         "ifsc": L.ifsc, "billwise": bool(L.billwise), "credit_days": L.credit_days,
         "tds_applicable": bool(L.tds_applicable), "tds_section": L.tds_section,
         "tds_rate": L.tds_rate, "tags": L.tags or [],
         "opening_balance": r2(L.opening_balance), "source": L.source,
         "system": L.source == "system", "active": bool(L.active)}
    if bal is not None:
        d["balance"] = r2(bal)
    return d


def _ledger_matches(L, flt):
    if not flt:
        return True
    tags = set(L.tags or [])
    return {
        "gst_available": bool(L.gstin), "pan_available": bool(L.pan),
        "gst_missing": not L.gstin, "pan_missing": not L.pan,
        "billwise_on": bool(L.billwise), "billwise_off": not L.billwise,
        "tds": bool(L.tds_applicable),
    }.get(flt, flt in tags)


def list_ledgers(db, group=None, flt=None, name=None, groups=None, with_balance=True):
    q = db.query(models.Ledger).filter(models.Ledger.active.is_(True))
    if group:
        q = q.filter(models.Ledger.group == group)
    if groups:
        q = q.filter(models.Ledger.group.in_(groups))
    if name:
        like = f"%{name.strip()}%"
        q = q.filter(or_(models.Ledger.name.ilike(like), models.Ledger.alias.ilike(like)))
    rows = [L for L in q.order_by(models.Ledger.group, models.Ledger.name).all()
            if _ledger_matches(L, flt)]
    bal = balances(db) if with_balance else {}
    return [ledger_out(L, bal.get(L.id, {}).get("closing", r2(L.opening_balance))
                       if with_balance else None) for L in rows]


LEDGER_FIELDS = ["name", "alias", "group", "business_id", "location", "gstin", "pan",
                 "state", "address", "phone", "email", "bank_account", "ifsc",
                 "billwise", "credit_days", "tds_applicable", "tds_section",
                 "tds_rate", "tags", "opening_balance"]


def save_ledger(db, data: dict, lid=None):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("A ledger needs a name")
    if data.get("group") not in GROUPS:
        raise ValueError("Choose the ledger's type")
    dupe = db.query(models.Ledger).filter(func.lower(models.Ledger.name) == name.lower(),
                                          models.Ledger.active.is_(True))
    if lid:
        dupe = dupe.filter(models.Ledger.id != lid)
    if dupe.first():
        raise ValueError(f"A ledger called {name} already exists")
    L = db.get(models.Ledger, lid) if lid else models.Ledger(tags=[])
    if lid and not L:
        raise LookupError("ledger not found")
    for k in LEDGER_FIELDS:
        if k in data:
            v = data[k]
            if k in ("gstin", "pan") and v:
                v = str(v).strip().upper()
            setattr(L, k, v)
    L.name = name
    L.opening_balance = r2(L.opening_balance)
    if L.source == "system" and lid:
        # a system ledger may be renamed and re-described, never re-grouped:
        # the auto-postings depend on where it sits
        L.group = SYSTEM[L.alias][1]
    if not lid:
        db.add(L)
    db.flush()
    return L


def delete_ledger(db, lid):
    L = db.get(models.Ledger, lid)
    if not L:
        raise LookupError("ledger not found")
    if L.source == "system":
        raise ValueError("The auto-postings use this ledger — it cannot be deleted")
    used = db.query(models.VoucherLine).filter(models.VoucherLine.ledger_id == lid).count()
    if used:
        # kept, because vouchers point at it — but out of every list
        L.active = False
        return {"deactivated": True, "lines": used}
    db.delete(L)
    return {"deleted": True}


# ---------------------------------------------------------------------------
#  Vouchers
# ---------------------------------------------------------------------------
def _next_no(db, vtype):
    prefix = VPREFIX.get(vtype, "VCH")
    n = (db.query(func.count(models.Voucher.id))
           .filter(models.Voucher.vtype == vtype).scalar() or 0) + 1
    while True:
        no = f"{prefix}-{n:05d}"
        if not db.query(models.Voucher.id).filter(models.Voucher.voucher_no == no).first():
            return no
        n += 1


def _write(db, V: models.Voucher, lines, rounding=True):
    """Attach lines (ledger_id, debit, credit, narration, bill_ref) after
    folding any paisa of difference into Rounding Off."""
    V.lines = []
    dr = cr = 0.0
    for ln in lines:
        d, c = r2(ln.get("debit")), r2(ln.get("credit"))
        if not d and not c:
            continue
        V.lines.append(models.VoucherLine(
            ledger_id=ln["ledger_id"], debit=d, credit=c,
            narration=ln.get("narration"), bill_ref=ln.get("bill_ref"),
            cleared_on=ln.get("cleared_on")))
        dr += d
        cr += c
    diff = r2(dr - cr)
    if diff and rounding and abs(diff) < 1:
        rl = system_ledger(db, "rounding")
        V.lines.append(models.VoucherLine(ledger_id=rl.id,
                                          debit=-diff if diff < 0 else 0,
                                          credit=diff if diff > 0 else 0))
        dr = cr = max(dr, cr)
    V.amount = r2(max(dr, cr))
    return r2(dr - cr) if not rounding or abs(diff) >= 1 else 0


def save_voucher(db, data: dict, user=None, vid=None, source="manual", source_id=None):
    vtype = data.get("vtype")
    if vtype not in VTYPES:
        raise ValueError("Choose the voucher type")
    lines = [ln for ln in (data.get("lines") or [])
             if ln.get("ledger_id") and (r2(ln.get("debit")) or r2(ln.get("credit")))]
    if len(lines) < 2:
        raise ValueError("A voucher needs at least one debit and one credit line")
    for ln in lines:
        if not db.get(models.Ledger, int(ln["ledger_id"])):
            raise ValueError("A line names a ledger that does not exist")
    dr = r2(sum(r2(ln.get("debit")) for ln in lines))
    cr = r2(sum(r2(ln.get("credit")) for ln in lines))
    if dr != cr:
        raise ValueError(f"Debits ₹{dr:,.2f} and credits ₹{cr:,.2f} must be equal")
    if vid:
        V = db.get(models.Voucher, vid)
        if not V:
            raise LookupError("voucher not found")
        if V.source not in ("manual", "petty_cash", "transport_payment",
                            "agent_payment", "collection", "credit_note"):
            raise ValueError("This voucher was posted from the warehouse — correct it there")
    else:
        V = models.Voucher(vtype=vtype, voucher_no=_next_no(db, vtype), source=source,
                           source_id=source_id, created_by=(user or {}).get("username"))
        db.add(V)
    V.vtype = vtype
    V.date = iso(data.get("date"), today())
    for k in ("business_id", "location", "mode", "ref_no", "cheque_no",
              "cheque_date", "narration", "party_ledger_id", "tds_amount"):
        if k in data:
            setattr(V, k, data[k])
    if V.cheque_date:
        V.cheque_date = iso(V.cheque_date)
    _write(db, V, lines, rounding=False)
    db.flush()
    return V


def cancel_voucher(db, vid):
    V = db.get(models.Voucher, vid)
    if not V:
        raise LookupError("voucher not found")
    if V.source in ("purchase", "payment", "return", "pos_day", "pos_return"):
        raise ValueError("This voucher was posted from the warehouse — reverse it there")
    V.status = "cancelled"
    return V


def voucher_out(V: models.Voucher, lines=True) -> dict:
    d = {"id": V.id, "voucher_no": V.voucher_no, "vtype": V.vtype, "date": V.date,
         "business_id": V.business_id, "location": V.location, "mode": V.mode,
         "ref_no": V.ref_no, "cheque_no": V.cheque_no, "cheque_date": V.cheque_date,
         "narration": V.narration, "source": V.source, "source_id": V.source_id,
         "party_ledger_id": V.party_ledger_id,
         "party": V.party.name if V.party else None,
         "amount": r2(V.amount), "tds_amount": r2(V.tds_amount), "status": V.status,
         "editable": V.source in ("manual", "petty_cash", "transport_payment",
                                  "agent_payment", "collection", "credit_note"),
         "tally_status": V.tally_status,
         "tally_synced_at": V.tally_synced_at.isoformat() if V.tally_synced_at else None,
         "created_by": V.created_by,
         "created_at": V.created_at.isoformat() if V.created_at else None,
         "modified_at": V.modified_at.isoformat() if V.modified_at else None}
    if lines:
        d["lines"] = [{"id": ln.id, "ledger_id": ln.ledger_id,
                       "ledger": ln.ledger.name if ln.ledger else None,
                       "group": ln.ledger.group if ln.ledger else None,
                       "debit": r2(ln.debit), "credit": r2(ln.credit),
                       "narration": ln.narration, "bill_ref": ln.bill_ref,
                       "cleared_on": ln.cleared_on} for ln in V.lines]
        dr = [ln for ln in d["lines"] if ln["debit"]]
        cr = [ln for ln in d["lines"] if ln["credit"]]
        d["detail"] = " / ".join(filter(None, [
            "Dr " + ", ".join(ln["ledger"] or "" for ln in dr[:2]) if dr else "",
            "Cr " + ", ".join(ln["ledger"] or "" for ln in cr[:2]) if cr else ""]))
    return d


def _vq(db, date_from=None, date_to=None, business_id=None, location=None):
    q = db.query(models.Voucher).filter(models.Voucher.status == "posted")
    if date_from:
        q = q.filter(models.Voucher.date >= date_from)
    if date_to:
        q = q.filter(models.Voucher.date <= date_to)
    if business_id:
        q = q.filter(or_(models.Voucher.business_id == int(business_id),
                         models.Voucher.business_id.is_(None)))
    if location:
        locs = location if isinstance(location, (list, tuple)) else [location]
        q = q.filter(models.Voucher.location.in_(locs))
    return q


def list_vouchers(db, date_from=None, date_to=None, vtype=None, mode=None,
                  ledger_id=None, voucher_no=None, amount_min=None, amount_max=None,
                  business_id=None, location=None, limit=2000):
    q = _vq(db, date_from, date_to, business_id, location).options(
        joinedload(models.Voucher.lines).joinedload(models.VoucherLine.ledger),
        joinedload(models.Voucher.party))
    if vtype:
        q = q.filter(models.Voucher.vtype.in_(vtype if isinstance(vtype, list) else [vtype]))
    if mode:
        q = q.filter(models.Voucher.mode == mode)
    if voucher_no:
        q = q.filter(models.Voucher.voucher_no.ilike(f"%{voucher_no.strip()}%"))
    if amount_min not in (None, ""):
        q = q.filter(models.Voucher.amount >= float(amount_min))
    if amount_max not in (None, ""):
        q = q.filter(models.Voucher.amount <= float(amount_max))
    if ledger_id:
        q = q.filter(models.Voucher.lines.any(models.VoucherLine.ledger_id == int(ledger_id)))
    rows = q.order_by(models.Voucher.date.desc(), models.Voucher.id.desc()).limit(limit).all()
    return [voucher_out(V) for V in rows]


# ---------------------------------------------------------------------------
#  Auto-postings from the warehouse and the shop
# ---------------------------------------------------------------------------
def _wh_names(db):
    return {w.id: (w.name, w.business_id) for w in db.query(models.Warehouse).all()}


def _auto(db, source, source_id, vtype, date, lines, *, business_id=None, location=None,
          party=None, narration=None, mode=None, ref_no=None, ref_key=None):
    V = models.Voucher(vtype=vtype, voucher_no=_next_no(db, vtype), source=source,
                       source_id=source_id, date=date, business_id=business_id,
                       location=location, party_ledger_id=party.id if party else None,
                       narration=narration, mode=mode, ref_no=ref_key or ref_no,
                       created_by="auto")
    db.add(V)
    _write(db, V, lines)
    return V


def sync_books(db: Session) -> dict:
    """Bring the books up to date with the warehouse and the shop. Safe to call
    on every read: each source row is posted once, and a row that has gone
    (a GRN unposted, a payment deleted) takes its voucher with it."""
    ensure_defaults(db)
    made = defaultdict(int)
    whs = _wh_names(db)
    sys = {k: system_ledger(db, k) for k in SYSTEM}

    def existing(source):
        return {sid: vid for vid, sid in db.query(models.Voucher.id, models.Voucher.source_id)
                .filter(models.Voucher.source == source)}

    def drop(source, ids):
        if ids:
            for V in db.query(models.Voucher).filter(models.Voucher.source == source,
                                                     models.Voucher.source_id.in_(ids)):
                db.delete(V)
            made[source + "_removed"] += len(ids)

    # --- posted GRNs -> Purchase -------------------------------------------
    have = existing("purchase")
    posted = {p.id: p for p in db.query(models.Purchase).options(joinedload(models.Purchase.supplier))
              .filter(models.Purchase.status == "posted")}
    drop("purchase", [sid for sid in have if sid not in posted])
    for pid, p in posted.items():
        if pid in have or not p.supplier:
            continue
        sup = party_ledger(db, "supplier", p.supplier)
        loc, biz = whs.get(p.warehouse_id, (None, None))
        tax, total = r2(p.tax_total), r2(p.grand_total)
        _auto(db, "purchase", pid, "Purchase", iso(p.invoice_date, p.posted_at or p.created_at), [
            {"ledger_id": sys["purchase"].id, "debit": r2(total - tax)},
            {"ledger_id": sys["input_gst"].id, "debit": tax},
            {"ledger_id": sup.id, "credit": total, "bill_ref": f"PUR:{pid}"},
        ], business_id=biz, location=loc, party=sup,
            narration=f"GRN {p.grn_no or pid} · invoice {p.invoice_number or '—'}",
            ref_key=p.invoice_number)
        made["purchase"] += 1

    # --- supplier payments -> Payment --------------------------------------
    have = existing("payment")
    pays = {p.id: p for p in db.query(models.Payment).options(
        joinedload(models.Payment.supplier), joinedload(models.Payment.allocations))}
    drop("payment", [sid for sid in have if sid not in pays])
    for pid, p in pays.items():
        if pid in have or not p.supplier:
            continue
        sup = party_ledger(db, "supplier", p.supplier)
        mode = (p.mode or "NEFT")
        money = sys["cash"] if mode.lower() == "cash" else sys["bank"]
        lines = []
        for a in p.allocations:
            lines.append({"ledger_id": sup.id, "debit": r2(a.settled), "bill_ref": f"PUR:{a.purchase_id}"})
        if not lines:
            continue
        lines += [{"ledger_id": money.id, "credit": r2(p.paid_amount)},
                  {"ledger_id": sys["disc_recv"].id, "credit": r2(p.discount_total)},
                  {"ledger_id": sys["tds"].id, "credit": r2(p.tds_total)},
                  {"ledger_id": sys["purchase_ret"].id, "credit": r2(p.debit_adjust_total)}]
        V = _auto(db, "payment", pid, "Payment", iso(p.date, p.created_at), lines,
                  party=sup, mode=mode, narration=p.remarks or f"Supplier payment {p.receipt_no}",
                  ref_key=p.receipt_no)
        V.cheque_no = p.cheque_no
        V.cheque_date = iso(p.cheque_date) if p.cheque_date else None
        V.tds_amount = r2(p.tds_total)
        made["payment"] += 1

    # --- posted debit notes -> Debit Note ----------------------------------
    have = existing("return")
    rets = {r.id: r for r in db.query(models.PurchaseReturn).options(
        joinedload(models.PurchaseReturn.supplier))
        .filter(models.PurchaseReturn.status == "posted")}
    drop("return", [sid for sid in have if sid not in rets])
    for rid, r in rets.items():
        if rid in have or not r.supplier:
            continue
        sup = party_ledger(db, "supplier", r.supplier)
        tax, total = r2(r.tax_total), r2(r.total)
        _auto(db, "return", rid, "Debit Note", iso(r.date, r.posted_at or r.created_at), [
            {"ledger_id": sup.id, "debit": total,
             "bill_ref": f"PUR:{r.purchase_id}" if r.purchase_id else None},
            {"ledger_id": sys["purchase_ret"].id, "credit": r2(total - tax)},
            {"ledger_id": sys["input_gst"].id, "credit": tax},
        ], party=sup, narration=f"Debit note {r.code or rid} · {r.reason or ''}".strip(" ·"),
            ref_key=r.code)
        made["return"] += 1

    # --- the till, one voucher per day per store ----------------------------
    made.update(_sync_pos(db, sys))
    db.flush()
    return dict(made)


def _sync_pos(db, sys):
    made = defaultdict(int)
    if not pos_sales.available():
        return made
    q = pos_sales.q
    day = "substr(CAST(i.invoice_date AS TEXT), 1, 10)"
    sales = pos_sales._rows(
        f"SELECT {day}, COALESCE(l.name, 'Store'), SUM(i.total), "
        "SUM(COALESCE(i.cgst,0)+COALESCE(i.sgst,0)+COALESCE(i.igst,0)), "
        "SUM(COALESCE(i.discount,0)) "
        f"FROM {q('invoices')} i LEFT JOIN {q('locations')} l ON l.id = i.location_id "
        "WHERE 1=1" + pos_sales.LIVE_BILL + " GROUP BY 1, 2", [])
    tenders = defaultdict(dict)
    for d, loc, method, amt in pos_sales._rows(
            f"SELECT {day}, COALESCE(l.name, 'Store'), p.method, SUM(p.amount) "
            f"FROM {q('invoice_payments')} p JOIN {q('invoices')} i ON i.id = p.invoice_id "
            f"LEFT JOIN {q('locations')} l ON l.id = i.location_id "
            "WHERE 1=1" + pos_sales.LIVE_BILL + " GROUP BY 1, 2, 3", []):
        tenders[(d, loc)][(method or "cash").lower()] = r2(amt)
    cday = "substr(CAST(c.created_at AS TEXT), 1, 10)"
    returns = pos_sales._rows(
        f"SELECT {cday}, COALESCE(l.name, 'Store'), SUM(c.total), "
        "SUM(COALESCE(c.cgst,0)+COALESCE(c.sgst,0)+COALESCE(c.igst,0)) "
        f"FROM {q('credit_notes')} c JOIN {q('invoices')} i ON i.id = c.invoice_id "
        f"LEFT JOIN {q('locations')} l ON l.id = i.location_id GROUP BY 1, 2", [])

    def sync(source, rows, build):
        have = {V.ref_no: V for V in db.query(models.Voucher).filter(models.Voucher.source == source)}
        for row in rows:
            key = f"{row[0]}|{row[1]}"
            total = r2(row[2])
            V = have.pop(key, None)
            if V and r2(V.amount) == total:
                continue
            if V:
                db.delete(V)
            if total <= 0:
                continue
            vtype, lines, narr = build(row)
            _auto(db, source, None, vtype, row[0], lines, location=row[1],
                  narration=narr, ref_key=key)
            made[source] += 1
        for V in have.values():
            db.delete(V)

    def sale(row):
        d, loc, total, tax, _disc = row
        t = tenders.get((d, loc)) or {"cash": r2(total)}
        lines = []
        rest = r2(total)
        for m, amt in t.items():
            led = sys["card"] if m == "card" else sys["upi"] if m == "upi" else sys["cash"]
            lines.append({"ledger_id": led.id, "debit": amt})
            rest = r2(rest - amt)
        if rest:
            lines.append({"ledger_id": sys["cash"].id, "debit": rest})
        lines += [{"ledger_id": sys["sales"].id, "credit": r2(r2(total) - r2(tax))},
                  {"ledger_id": sys["output_gst"].id, "credit": r2(tax)}]
        return "Sales", lines, f"Till sales {loc}"

    def ret(row):
        d, loc, total, tax = row
        return "Credit Note", [
            {"ledger_id": sys["sales_ret"].id, "debit": r2(r2(total) - r2(tax))},
            {"ledger_id": sys["output_gst"].id, "debit": r2(tax)},
            {"ledger_id": sys["cash"].id, "credit": r2(total)},
        ], f"Till returns {loc}"

    sync("pos_day", [r for r in sales if r[0]], sale)
    sync("pos_return", [r for r in returns if r[0]], ret)
    return made


# ---------------------------------------------------------------------------
#  Balances — the one aggregation every statement is built from
# ---------------------------------------------------------------------------
def _line_sums(db, date_from=None, date_to=None, business_id=None, location=None,
               vtypes=None, before=False):
    VL, V = models.VoucherLine, models.Voucher
    q = (db.query(VL.ledger_id, func.sum(VL.debit), func.sum(VL.credit))
           .join(V, V.id == VL.voucher_id).filter(V.status == "posted"))
    if before:
        if date_from:
            q = q.filter(V.date < date_from)
        else:
            return {}
    else:
        if date_from:
            q = q.filter(V.date >= date_from)
        if date_to:
            q = q.filter(V.date <= date_to)
    if business_id:
        q = q.filter(or_(V.business_id == int(business_id), V.business_id.is_(None)))
    if location:
        locs = location if isinstance(location, (list, tuple)) else [location]
        q = q.filter(V.location.in_(locs))
    if vtypes:
        q = q.filter(V.vtype.in_(vtypes))
    return {lid: (r2(d), r2(c)) for lid, d, c in q.group_by(VL.ledger_id)}


def balances(db, date_from=None, date_to=None, business_id=None, location=None, vtypes=None):
    """{ledger_id: {opening, debit, credit, closing}} — signed, + is debit."""
    prior = _line_sums(db, date_from, None, business_id, location, vtypes, before=True)
    period = _line_sums(db, date_from, date_to, business_id, location, vtypes)
    out = {}
    with_opening = not location and not vtypes
    for L in db.query(models.Ledger).all():
        p = prior.get(L.id, (0, 0))
        d, c = period.get(L.id, (0, 0))
        opening = r2(((L.opening_balance or 0) if with_opening else 0) + p[0] - p[1])
        if not (opening or d or c):
            continue
        out[L.id] = {"opening": opening, "debit": d, "credit": c,
                     "closing": r2(opening + d - c)}
    return out


def _ledgers(db):
    return {L.id: L for L in db.query(models.Ledger).all()}


def trial_balance(db, date_from=None, date_to=None, business_id=None, location=None):
    bal = balances(db, date_from, date_to, business_id, location)
    Ls = _ledgers(db)
    groups = defaultdict(lambda: {"rows": [], "opening": 0, "debit": 0, "credit": 0,
                                  "closing_dr": 0, "closing_cr": 0})
    for lid, b in bal.items():
        L = Ls[lid]
        g = groups[L.group]
        row = {"ledger_id": lid, "ledger": L.name, **b,
               "closing_dr": b["closing"] if b["closing"] > 0 else 0,
               "closing_cr": -b["closing"] if b["closing"] < 0 else 0}
        g["rows"].append(row)
        for k in ("opening", "debit", "credit", "closing_dr", "closing_cr"):
            g[k] = r2(g[k] + row[k])
    order = {n: i for i, n in enumerate(GROUP_NAMES)}
    out = [{"group": k, "nature": GROUPS.get(k, ("asset", "bs"))[0], **v,
            "rows": sorted(v["rows"], key=lambda r: r["ledger"])}
           for k, v in sorted(groups.items(), key=lambda kv: order.get(kv[0], 99))]
    tot = {k: r2(sum(g[k] for g in out)) for k in ("debit", "credit", "closing_dr", "closing_cr")}
    tot["difference"] = r2(tot["closing_dr"] - tot["closing_cr"])
    return {"groups": out, "totals": tot}


def _section(db, date_from, date_to, business_id, location, section):
    bal = balances(db, date_from, date_to, business_id, location)
    Ls = _ledgers(db)
    inc, exp = defaultdict(list), defaultdict(list)
    for lid, b in bal.items():
        L = Ls[lid]
        nature, sec = GROUPS.get(L.group, ("asset", "bs"))
        if sec != section:
            continue
        # a statement reads the PERIOD's movement for income and expense
        amt = r2(b["debit"] - b["credit"] + (b["opening"] if section == "bs" else 0))
        if not amt:
            continue
        if nature == "income":
            inc[L.group].append({"ledger_id": lid, "ledger": L.name, "amount": -amt})
        else:
            exp[L.group].append({"ledger_id": lid, "ledger": L.name, "amount": amt})

    def pack(d):
        return [{"group": g, "amount": r2(sum(r["amount"] for r in rows)),
                 "rows": sorted(rows, key=lambda r: -abs(r["amount"]))} for g, rows in d.items()]
    return pack(inc), pack(exp)


def _stock_value(db, as_on=None):
    """Closing stock at cost, from the warehouse's own balances."""
    try:
        v = db.query(func.sum(models.StockBalance.qty * models.StockBalance.avg_cost)).filter(
            models.StockBalance.qty > 0).scalar()
        return r2(v)
    except Exception:  # noqa: BLE001 — a schema without costs reports no stock
        return 0.0


def trading_account(db, date_from=None, date_to=None, business_id=None, location=None,
                    opening_stock=None, closing_stock=None):
    inc, exp = _section(db, date_from, date_to, business_id, location, "trading")
    close = r2(closing_stock) if closing_stock not in (None, "") else _stock_value(db)
    opening = r2(opening_stock or 0)
    total_inc = r2(sum(g["amount"] for g in inc) + close)
    total_exp = r2(sum(g["amount"] for g in exp) + opening)
    gp = r2(total_inc - total_exp)
    return {"income": inc, "expense": exp, "opening_stock": opening, "closing_stock": close,
            "gross_profit": gp, "total": r2(max(total_inc, total_exp))}


def profit_and_loss(db, date_from=None, date_to=None, business_id=None, location=None,
                    opening_stock=None, closing_stock=None):
    tr = trading_account(db, date_from, date_to, business_id, location, opening_stock, closing_stock)
    inc, exp = _section(db, date_from, date_to, business_id, location, "pl")
    gp = tr["gross_profit"]
    total_inc = r2(sum(g["amount"] for g in inc) + max(gp, 0))
    total_exp = r2(sum(g["amount"] for g in exp) + max(-gp, 0))
    return {"trading": tr, "income": inc, "expense": exp, "gross_profit": gp,
            "net_profit": r2(total_inc - total_exp), "total": r2(max(total_inc, total_exp))}


def balance_sheet(db, as_on=None, business_id=None, location=None, closing_stock=None):
    as_on = as_on or today()
    pl = profit_and_loss(db, None, as_on, business_id, location, 0, closing_stock)
    assets, liabs = defaultdict(list), defaultdict(list)
    bal = balances(db, None, as_on, business_id, location)
    Ls = _ledgers(db)
    for lid, b in bal.items():
        L = Ls[lid]
        nature, sec = GROUPS.get(L.group, ("asset", "bs"))
        if sec != "bs" or not b["closing"]:
            continue
        # a balance on the "wrong" side moves across: an overdrawn bank is a
        # liability, an advance to a supplier is an asset
        if b["closing"] > 0:
            assets[L.group].append({"ledger_id": lid, "ledger": L.name, "amount": b["closing"]})
        else:
            liabs[L.group].append({"ledger_id": lid, "ledger": L.name, "amount": -b["closing"]})

    def pack(d):
        return [{"group": g, "amount": r2(sum(r["amount"] for r in rows)),
                 "rows": sorted(rows, key=lambda r: -r["amount"])} for g, rows in d.items()]
    A, Lb = pack(assets), pack(liabs)
    stock = pl["trading"]["closing_stock"]
    if stock:
        A.append({"group": "Closing Stock", "amount": stock,
                  "rows": [{"ledger": "Stock at cost", "amount": stock}]})
    np_ = pl["net_profit"]
    Lb.append({"group": "Profit & Loss A/c", "amount": np_,
               "rows": [{"ledger": "Net profit" if np_ >= 0 else "Net loss", "amount": np_}]})
    ta, tl = r2(sum(g["amount"] for g in A)), r2(sum(g["amount"] for g in Lb))
    return {"as_on": as_on, "assets": A, "liabilities": Lb, "total_assets": ta,
            "total_liabilities": tl, "difference": r2(ta - tl), "net_profit": np_}


def daily_pl(db, date_from, date_to, business_id=None, location=None):
    VL, V = models.VoucherLine, models.Voucher
    Ls = _ledgers(db)
    q = (db.query(V.date, VL.ledger_id, func.sum(VL.debit), func.sum(VL.credit))
           .join(V, V.id == VL.voucher_id).filter(V.status == "posted"))
    if date_from:
        q = q.filter(V.date >= date_from)
    if date_to:
        q = q.filter(V.date <= date_to)
    if business_id:
        q = q.filter(or_(V.business_id == int(business_id), V.business_id.is_(None)))
    if location:
        q = q.filter(V.location == location)
    days = defaultdict(lambda: {"income": 0.0, "expense": 0.0, "purchase": 0.0, "sales": 0.0})
    for d, lid, dr, cr in q.group_by(V.date, VL.ledger_id):
        L = Ls.get(lid)
        if not L or GROUPS.get(L.group, ("", "bs"))[1] == "bs":
            continue
        nature = GROUPS[L.group][0]
        amt = r2((dr or 0) - (cr or 0))
        row = days[d]
        if nature == "income":
            row["income"] = r2(row["income"] - amt)
            if L.group == "Sales":
                row["sales"] = r2(row["sales"] - amt)
        else:
            row["expense"] = r2(row["expense"] + amt)
            if L.group == "Purchase":
                row["purchase"] = r2(row["purchase"] + amt)
    out = [{"date": d, **v, "profit": r2(v["income"] - v["expense"])} for d, v in sorted(days.items())]
    tot = {k: r2(sum(r[k] for r in out)) for k in ("income", "expense", "purchase", "sales", "profit")}
    return {"rows": out, "totals": tot}


def ledger_statement(db, lid, date_from=None, date_to=None):
    L = db.get(models.Ledger, lid)
    if not L:
        raise LookupError("ledger not found")
    VL, V = models.VoucherLine, models.Voucher
    prior = _line_sums(db, date_from, None, before=True).get(lid, (0, 0))
    running = r2((L.opening_balance or 0) + prior[0] - prior[1])
    opening = running
    q = (db.query(VL, V).join(V, V.id == VL.voucher_id)
           .filter(VL.ledger_id == lid, V.status == "posted"))
    if date_from:
        q = q.filter(V.date >= date_from)
    if date_to:
        q = q.filter(V.date <= date_to)
    rows = []
    for ln, v in q.order_by(V.date, V.id):
        running = r2(running + (ln.debit or 0) - (ln.credit or 0))
        rows.append({"date": v.date, "voucher_id": v.id, "voucher_no": v.voucher_no,
                     "vtype": v.vtype, "narration": ln.narration or v.narration,
                     "debit": r2(ln.debit), "credit": r2(ln.credit), "balance": running,
                     "bill_ref": ln.bill_ref, "cleared_on": ln.cleared_on, "line_id": ln.id,
                     "cheque_no": v.cheque_no, "mode": v.mode})
    return {"ledger": ledger_out(L), "opening": opening, "closing": running, "rows": rows}


# ---------------------------------------------------------------------------
#  Bill-wise: what is still pending against a party
# ---------------------------------------------------------------------------
def party_bills(db, lid, as_on=None):
    """Bills raised on a party ledger and what has been set against each, by
    bill_ref. A credit-nature party (supplier) raises on credit; a debtor on
    debit."""
    L = db.get(models.Ledger, lid)
    if not L:
        raise LookupError("ledger not found")
    debtor = GROUPS.get(L.group, ("asset",))[0] == "asset"
    VL, V = models.VoucherLine, models.Voucher
    q = (db.query(VL, V).join(V, V.id == VL.voucher_id)
           .filter(VL.ledger_id == lid, V.status == "posted"))
    if as_on:
        q = q.filter(V.date <= as_on)
    bills, on_acct = {}, 0.0
    for ln, v in q.order_by(V.date, V.id):
        raise_amt = r2(ln.debit - ln.credit) if debtor else r2(ln.credit - ln.debit)
        ref = ln.bill_ref
        if not ref:
            on_acct = r2(on_acct + raise_amt)
            continue
        b = bills.setdefault(ref, {"bill_ref": ref, "date": v.date, "voucher_no": v.voucher_no,
                                   "narration": v.narration, "amount": 0.0, "settled": 0.0})
        if raise_amt > 0 and not b["amount"]:
            b["date"], b["voucher_no"], b["narration"] = v.date, v.voucher_no, v.narration
        if raise_amt > 0:
            b["amount"] = r2(b["amount"] + raise_amt)
        else:
            b["settled"] = r2(b["settled"] - raise_amt)
    d0 = dates.parse(as_on) if as_on else dt.date.today()
    out = []
    for b in bills.values():
        b["pending"] = r2(b["amount"] - b["settled"])
        if abs(b["pending"]) < 0.01:
            continue
        d = dates.parse(b["date"])
        b["days"] = (d0 - d).days if d else None
        out.append(b)
    out.sort(key=lambda b: b["date"] or "")
    return {"ledger": ledger_out(L), "bills": out, "on_account": on_acct,
            "pending": r2(sum(b["pending"] for b in out) + on_acct)}


AGE_BUCKETS = [(30, "0-30"), (60, "31-60"), (90, "61-90"), (180, "91-180"), (10 ** 6, "180+")]


def outstanding(db, groups, as_on=None, location=None, search=None):
    """Payable (creditors) or Receivable (debtors): each party's pending, aged."""
    as_on = as_on or today()
    debtor = GROUPS.get(groups[0], ("asset",))[0] == "asset"
    bal = balances(db, None, as_on, None, location)
    rows = []
    q = db.query(models.Ledger).filter(models.Ledger.group.in_(groups))
    if search:
        q = q.filter(models.Ledger.name.ilike(f"%{search}%"))
    for L in q.order_by(models.Ledger.name):
        b = bal.get(L.id)
        if not b:
            continue
        amt = b["closing"] if debtor else -b["closing"]
        if abs(amt) < 0.01:
            continue
        aged = {lbl: 0.0 for _, lbl in AGE_BUCKETS}
        if L.billwise:
            for bill in party_bills(db, L.id, as_on)["bills"]:
                days = bill["days"] or 0
                for lim, lbl in AGE_BUCKETS:
                    if days <= lim:
                        aged[lbl] = r2(aged[lbl] + bill["pending"])
                        break
            known = r2(sum(aged.values()))
            if r2(amt - known):
                aged["0-30"] = r2(aged["0-30"] + amt - known)
        else:
            aged["0-30"] = r2(amt)
        rows.append({"ledger_id": L.id, "party": L.name, "group": L.group,
                     "gstin": L.gstin, "phone": L.phone, "credit_days": L.credit_days,
                     "pending": r2(amt), **aged})
    rows.sort(key=lambda r: -r["pending"])
    tot = {lbl: r2(sum(r[lbl] for r in rows)) for _, lbl in AGE_BUCKETS}
    tot["pending"] = r2(sum(r["pending"] for r in rows))
    return {"as_on": as_on, "buckets": [lbl for _, lbl in AGE_BUCKETS], "rows": rows, "totals": tot}


# ---------------------------------------------------------------------------
#  Payment screens: transport, agent, credit collection, credit note
# ---------------------------------------------------------------------------
def _settled_by_ref(db, prefix):
    VL, V = models.VoucherLine, models.Voucher
    return {ref: r2(d) for ref, d in db.query(VL.bill_ref, func.sum(VL.debit))
            .join(V, V.id == VL.voucher_id)
            .filter(V.status == "posted", VL.bill_ref.like(prefix + "%"))
            .group_by(VL.bill_ref)}


def transport_pending(db, transport=None, date_from=None, date_to=None, warehouse_id=None):
    """LR entries carrying freight not yet paid — what Transport Payment settles."""
    q = db.query(models.LREntry).filter(models.LREntry.freight_total > 0)
    if transport:
        q = q.filter(models.LREntry.transport == transport)
    if warehouse_id:
        q = q.filter(or_(models.LREntry.warehouse_id == warehouse_id,
                         models.LREntry.warehouse_id.is_(None)))
    paid = _settled_by_ref(db, "LR:")
    out = []
    for e in q.order_by(models.LREntry.id):
        d = iso(e.recv_date or e.lr_date, e.created_at)
        if date_from and d < date_from or date_to and d > date_to:
            continue
        charges = e.freight_charges or {}
        freight = r2(e.freight_amount or charges.get("freight") or e.freight_total)
        other = r2((e.freight_total or 0) - freight)
        done = paid.get(f"LR:{e.id}", 0)
        bal = r2((e.freight_total or 0) - done)
        if bal <= 0.01:
            continue
        out.append({"lr_id": e.id, "transport": e.transport, "supplier": e.supplier_name,
                    "lr_no": e.lr_no, "entry_no": e.lr_entry_no, "invoice_no": e.inv_no,
                    "date": d, "from": e.auto_transfer_location or "", "freight": freight,
                    "other": other, "total": r2(e.freight_total), "paid": done, "balance": bal,
                    "topay": e.paid_topay})
    return out


def agent_pending(db, agent=None, date_from=None, date_to=None, warehouse_id=None):
    """Commission earned on LR entries and not yet paid — Agent Payment."""
    q = db.query(models.LREntry).filter(models.LREntry.agent.isnot(None),
                                        models.LREntry.agent != "")
    if agent:
        q = q.filter(models.LREntry.agent == agent)
    if warehouse_id:
        q = q.filter(or_(models.LREntry.warehouse_id == warehouse_id,
                         models.LREntry.warehouse_id.is_(None)))
    paid = _settled_by_ref(db, "AGT:")
    out = []
    for e in q.order_by(models.LREntry.id):
        rate = e.agent_commission or 0
        if not rate:
            continue
        d = iso(e.recv_date or e.lr_date, e.created_at)
        if date_from and d < date_from or date_to and d > date_to:
            continue
        # under 100 it is a percentage of the goods value; above, a flat fee
        comm = r2((e.amount or 0) * rate / 100) if rate <= 100 else r2(rate)
        done = paid.get(f"AGT:{e.id}", 0)
        bal = r2(comm - done)
        if bal <= 0.01:
            continue
        out.append({"lr_id": e.id, "agent": e.agent, "supplier": e.supplier_name,
                    "invoice_no": e.inv_no, "date": d, "receipt_no": e.lr_entry_no,
                    "receipt_date": iso(e.lr_entry_date, e.created_at) if e.lr_entry_date else d,
                    "goods_value": r2(e.amount), "rate": rate, "commission": comm,
                    "paid": done, "balance": bal})
    return out


def _master_party(db, model, source, name):
    obj = db.query(model).filter(model.name == name).first()
    if not obj:
        obj = model(name=name)
        db.add(obj)
        db.flush()
    L = party_ledger(db, source, obj)
    if source != "supplier":
        L.group = "Sundry Creditors"
    return L


def pay_against(db, data: dict, user, *, kind):
    """Transport or agent payment: Dr expense (freight / commission) through the
    party, settling each picked LR by reference.

    Booked in two steps so the party ledger tells the whole story: the party is
    credited with what was earned (Dr expense, Cr party) and then debited with
    what was paid (Dr party, Cr bank, Cr TDS, Cr discount)."""
    sys = {k: system_ledger(db, k) for k in SYSTEM}
    rows = data.get("rows") or []
    if not rows:
        raise ValueError("Tick at least one row to pay")
    if not data.get("party"):
        raise ValueError("Choose who is being paid")
    if kind == "transport":
        party = _master_party(db, models.Transport, "transport", data["party"])
        expense, prefix, vsrc = sys["freight"], "LR", "transport_payment"
    else:
        party = _master_party(db, models.Agent, "agent", data["party"])
        expense, prefix, vsrc = sys["commission"], "AGT", "agent_payment"
    mode = data.get("mode") or "NEFT"
    bank = (db.get(models.Ledger, int(data["bank_ledger_id"])) if data.get("bank_ledger_id")
            else sys["cash"] if mode == "Cash" else sys["bank"])
    gross = r2(sum(r2(r.get("amount")) for r in rows))
    disc = r2(sum(r2(r.get("discount")) for r in rows))
    tds = r2(data.get("tds") or 0)
    net = r2(gross - disc - tds)
    lines = [{"ledger_id": expense.id, "debit": gross}]
    lines += [{"ledger_id": party.id, "credit": r2(r.get("amount")),
               "bill_ref": f"{prefix}:{r['lr_id']}"} for r in rows]
    lines += [{"ledger_id": party.id, "debit": r2(r2(r.get("amount"))),
               "bill_ref": f"{prefix}:{r['lr_id']}"} for r in rows]
    lines += [{"ledger_id": bank.id, "credit": net},
              {"ledger_id": sys["disc_recv"].id, "credit": disc},
              {"ledger_id": sys["tds"].id, "credit": tds}]
    payload = {"vtype": "Payment", "date": data.get("date"), "mode": mode,
               "ref_no": data.get("ref_no"), "cheque_no": data.get("cheque_no"),
               "cheque_date": data.get("cheque_date"), "party_ledger_id": party.id,
               "business_id": data.get("business_id"), "location": data.get("location"),
               "tds_amount": tds,
               "narration": data.get("remarks") or f"{'Freight' if kind == 'transport' else 'Commission'} paid to {party.name}",
               "lines": lines}
    # the LR refs are settled by the party DEBIT lines; the matching credits
    # carry a different ref so they do not cancel each other in _settled_by_ref
    for ln in payload["lines"]:
        if ln.get("credit") and ln.get("bill_ref"):
            ln["bill_ref"] = None
    V = save_voucher(db, payload, user, source=vsrc)
    # the party passes through (credited, then debited), which doubles the
    # line totals; the voucher's figure is what was settled
    V.amount = gross
    return V


def debtor_bills(db, lid):
    return party_bills(db, lid)


def collect(db, data: dict, user):
    """Credit Sale Collection: Dr Cash/Bank (+discount, +TDS), Cr the customer,
    allocated across their pending bills."""
    sys = {k: system_ledger(db, k) for k in SYSTEM}
    lid = data.get("ledger_id")
    cust = db.get(models.Ledger, int(lid)) if lid else None
    if not cust:
        raise ValueError("Choose the customer")
    paid = r2(data.get("paid"))
    disc = r2(data.get("discount"))
    tds = r2(data.get("tds"))
    allocs = [a for a in (data.get("allocations") or []) if r2(a.get("amount"))]
    total = r2(paid + disc + tds)
    if total <= 0:
        raise ValueError("Enter the amount received")
    mode = data.get("mode") or "Cash"
    bank = (db.get(models.Ledger, int(data["bank_ledger_id"])) if data.get("bank_ledger_id")
            else sys["cash"] if mode == "Cash" else sys["bank"])
    lines = [{"ledger_id": bank.id, "debit": paid},
             {"ledger_id": sys["disc_allow"].id, "debit": disc},
             {"ledger_id": sys["tds_recv"].id, "debit": tds}]
    used = 0.0
    for a in allocs:
        amt = r2(a["amount"])
        lines.append({"ledger_id": cust.id, "credit": amt, "bill_ref": a["bill_ref"]})
        used = r2(used + amt)
    if r2(total - used) > 0:
        lines.append({"ledger_id": cust.id, "credit": r2(total - used)})
    if used > total + 0.01:
        raise ValueError("More has been allocated than was received")
    return save_voucher(db, {"vtype": "Receipt", "date": data.get("date"), "mode": mode,
                             "ref_no": data.get("ref_no"), "party_ledger_id": cust.id,
                             "business_id": data.get("business_id"),
                             "location": data.get("location"), "tds_amount": tds,
                             "narration": data.get("remarks") or f"Collection from {cust.name}",
                             "lines": lines}, user, source="collection")


def credit_note(db, data: dict, user):
    """Customer Credit Note: Dr Sales Returns + Output GST per tax rate, Cr the
    customer (against a bill if one is named)."""
    sys = {k: system_ledger(db, k) for k in SYSTEM}
    cust = db.get(models.Ledger, int(data["ledger_id"])) if data.get("ledger_id") else None
    if not cust:
        raise ValueError("Choose the customer")
    rows = [r for r in (data.get("rows") or []) if r2(r.get("value"))]
    if not rows:
        raise ValueError("Add at least one tax line")
    igst = bool(data.get("igst"))
    value = r2(sum(r2(r["value"]) for r in rows))
    tax = r2(sum(r2(r2(r["value"]) * float(r.get("rate") or 0) / 100) for r in rows))
    total = r2(value + tax)
    return save_voucher(db, {
        "vtype": "Credit Note", "date": data.get("date"), "party_ledger_id": cust.id,
        "business_id": data.get("business_id"), "location": data.get("location"),
        "ref_no": data.get("bill_no"),
        "narration": (data.get("remarks") or f"Credit note to {cust.name}")
        + (" · IGST" if igst else ""),
        "lines": [{"ledger_id": sys["sales_ret"].id, "debit": value},
                  {"ledger_id": sys["output_gst"].id, "debit": tax},
                  {"ledger_id": cust.id, "credit": total,
                   "bill_ref": data.get("bill_ref") or None}]}, user, source="credit_note")


# ---------------------------------------------------------------------------
#  Petty cash: a month sheet of ledgers x days
# ---------------------------------------------------------------------------
def petty_sheet(db, cash_ledger_id, month, location=None):
    """month: YYYY-MM. Rows are the petty-cash ledgers, columns the days; each
    cell is what that ledger moved through the cash ledger that day."""
    y, m = (int(x) for x in month.split("-"))
    first = dt.date(y, m, 1)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    days = (nxt - first).days
    Ls = (db.query(models.Ledger).filter(models.Ledger.group.in_(PETTY_GROUPS),
                                         models.Ledger.active.is_(True))
            .order_by(models.Ledger.group, models.Ledger.name).all())
    q = (db.query(models.Voucher).options(joinedload(models.Voucher.lines))
           .filter(models.Voucher.source == "petty_cash", models.Voucher.status == "posted",
                   models.Voucher.date >= first.isoformat(), models.Voucher.date < nxt.isoformat()))
    if location:
        q = q.filter(models.Voucher.location == location)
    cells = defaultdict(dict)
    for V in q:
        if not any(ln.ledger_id == int(cash_ledger_id) for ln in V.lines):
            continue
        day = int(V.date[8:10])
        for ln in V.lines:
            if ln.ledger_id != int(cash_ledger_id):
                cells[ln.ledger_id][day] = r2(cells[ln.ledger_id].get(day, 0) + ln.debit + ln.credit)
    rows = []
    for L in Ls:
        income = GROUPS[L.group][0] == "income"
        vals = [cells[L.id].get(d, 0) for d in range(1, days + 1)]
        rows.append({"ledger_id": L.id, "particulars": L.name, "group": L.group,
                     "mode": "Inc" if income else "Exp", "days": vals, "total": r2(sum(vals))})
    stmt = ledger_statement(db, int(cash_ledger_id), first.isoformat(),
                            (nxt - dt.timedelta(days=1)).isoformat())
    return {"month": month, "days": days, "rows": rows,
            "opening": stmt["opening"], "closing": stmt["closing"]}


def petty_save(db, data, user):
    """Replace the petty-cash vouchers for the days edited."""
    cash = int(data["cash_ledger_id"])
    month = data["month"]
    loc = data.get("location")
    edits = defaultdict(list)    # day -> [(ledger, amount)]
    for row in data.get("rows") or []:
        L = db.get(models.Ledger, int(row["ledger_id"]))
        if not L:
            continue
        for i, v in enumerate(row.get("days") or []):
            if r2(v):
                edits[i + 1].append((L, r2(v)))
    days = {int(d) for d in (data.get("touched") or [])} or set(edits)
    made = 0
    for day in sorted(days):
        date = f"{month}-{day:02d}"
        for V in (db.query(models.Voucher)
                    .filter(models.Voucher.source == "petty_cash", models.Voucher.date == date,
                            models.Voucher.status == "posted")):
            if any(ln.ledger_id == cash for ln in V.lines) and (not loc or V.location == loc):
                db.delete(V)
        db.flush()
        if not edits.get(day):
            continue
        lines, net = [], 0.0
        for L, amt in edits[day]:
            if GROUPS[L.group][0] == "income":
                lines.append({"ledger_id": L.id, "credit": amt})
                net = r2(net + amt)
            else:
                lines.append({"ledger_id": L.id, "debit": amt})
                net = r2(net - amt)
        lines.append({"ledger_id": cash, "debit": net} if net > 0 else {"ledger_id": cash, "credit": -net})
        save_voucher(db, {"vtype": "Petty Cash", "date": date, "location": loc,
                          "business_id": data.get("business_id"), "mode": "Cash",
                          "narration": f"Petty cash {date}", "lines": lines},
                     user, source="petty_cash")
        made += 1
    return {"vouchers": made}


# ---------------------------------------------------------------------------
#  Bank reconciliation, cheque books, loans, TDS, GST, analyser
# ---------------------------------------------------------------------------
def brs(db, bank_id, date_from=None, date_to=None, status=None, modes=None):
    st = ledger_statement(db, bank_id, date_from, date_to)
    rows = st["rows"]
    if status == "cleared":
        rows = [r for r in rows if r["cleared_on"]]
    elif status == "uncleared":
        rows = [r for r in rows if not r["cleared_on"]]
    if modes:
        rows = [r for r in rows if _mode_kind(db, r) in modes]
    uncleared_dr = r2(sum(r["debit"] for r in st["rows"] if not r["cleared_on"]))
    uncleared_cr = r2(sum(r["credit"] for r in st["rows"] if not r["cleared_on"]))
    return {**st, "rows": rows, "uncleared_debit": uncleared_dr, "uncleared_credit": uncleared_cr,
            "bank_balance": r2(st["closing"] - uncleared_dr + uncleared_cr)}


_KIND = {"payment": "Supplier", "purchase": "Supplier", "return": "Supplier",
         "transport_payment": "Transport", "agent_payment": "Agent",
         "collection": "Customer", "credit_note": "Customer", "pos_day": "Customer"}


def _mode_kind(db, row):
    V = db.get(models.Voucher, row["voucher_id"])
    return _KIND.get(V.source, "Voucher") if V else "Voucher"


def set_cleared(db, line_id, cleared_on):
    ln = db.get(models.VoucherLine, line_id)
    if not ln:
        raise LookupError("line not found")
    ln.cleared_on = iso(cleared_on) if cleared_on else None
    return ln


def cheque_books(db, bank_id=None):
    q = db.query(models.ChequeBook).options(joinedload(models.ChequeBook.bank))
    if bank_id:
        q = q.filter(models.ChequeBook.bank_ledger_id == bank_id)
    used = defaultdict(list)
    for V in db.query(models.Voucher).filter(models.Voucher.cheque_no.isnot(None),
                                             models.Voucher.cheque_no != ""):
        digits = re.sub(r"\D", "", V.cheque_no or "")
        if digits:
            used[int(digits)].append({"voucher_no": V.voucher_no, "date": V.date,
                                      "amount": r2(V.amount), "party": V.party.name if V.party else None,
                                      "status": V.status})
    out = []
    for b in q.order_by(models.ChequeBook.id.desc()):
        leaves = []
        cancelled = {int(c["no"]): c.get("reason") for c in (b.cancelled or [])}
        for n in range(b.from_no or 0, (b.to_no or 0) + 1):
            if n in cancelled:
                leaves.append({"no": n, "status": "cancelled", "reason": cancelled[n]})
            elif used.get(n):
                # the voucher's own status is "posted"; the leaf's is "issued"
                leaves.append({**used[n][0], "no": n, "voucher_status": used[n][0]["status"],
                               "status": "issued"})
            else:
                leaves.append({"no": n, "status": "unused"})
        out.append({"id": b.id, "bank_ledger_id": b.bank_ledger_id,
                    "bank": b.bank.name if b.bank else None, "book_no": b.book_no,
                    "from_no": b.from_no, "to_no": b.to_no, "issued_on": b.issued_on,
                    "remarks": b.remarks, "leaves": leaves,
                    "used": sum(1 for x in leaves if x["status"] == "issued"),
                    "cancelled": sum(1 for x in leaves if x["status"] == "cancelled"),
                    "unused": sum(1 for x in leaves if x["status"] == "unused")})
    return out


def loans(db, as_on=None, date_from=None):
    as_on = as_on or today()
    bal = balances(db, date_from, as_on)
    out = []
    for L in (db.query(models.Ledger).filter(models.Ledger.group.in_(LOAN_GROUPS))
                .order_by(models.Ledger.name)):
        b = bal.get(L.id, {"opening": r2(L.opening_balance), "debit": 0, "credit": 0,
                           "closing": r2(L.opening_balance)})
        liab = GROUPS[L.group][0] == "liability"
        out.append({"ledger_id": L.id, "loan": L.name, "group": L.group,
                    "bank_account": L.bank_account,
                    "opening": -b["opening"] if liab else b["opening"],
                    "received": b["credit"] if liab else b["debit"],
                    "repaid": b["debit"] if liab else b["credit"],
                    "closing": -b["closing"] if liab else b["closing"]})
    return out


def tds_transactions(db, date_from=None, date_to=None, location=None, vtypes=None, ledger_ids=None):
    tds_ids = {L.id for L in db.query(models.Ledger).filter(
        or_(models.Ledger.tds_applicable.is_(True),
            models.Ledger.alias.in_(["tds", "tds_recv"])))}
    if ledger_ids:
        tds_ids &= {int(x) for x in ledger_ids}
    VL, V = models.VoucherLine, models.Voucher
    q = (db.query(VL, V).join(V, V.id == VL.voucher_id)
           .filter(V.status == "posted", VL.ledger_id.in_(tds_ids or [-1])))
    if date_from:
        q = q.filter(V.date >= date_from)
    if date_to:
        q = q.filter(V.date <= date_to)
    if location:
        q = q.filter(V.location.in_(location if isinstance(location, list) else [location]))
    if vtypes:
        q = q.filter(V.vtype.in_(vtypes))
    rows = []
    for ln, v in q.order_by(V.date):
        rows.append({"date": v.date, "voucher_no": v.voucher_no, "vtype": v.vtype,
                     "party": v.party.name if v.party else None,
                     "pan": v.party.pan if v.party else None,
                     "section": v.party.tds_section if v.party else None,
                     "ledger": ln.ledger.name, "gross": r2(v.amount),
                     "tds": r2(ln.credit or ln.debit), "location": v.location})
    return {"rows": rows, "total": r2(sum(r["tds"] for r in rows))}


def analyser(db, date_from=None, date_to=None, locations=None, vtypes=None, groups=None,
             mode="group"):
    """Group summary, ledger summary or month-wise movement, filtered by
    location, voucher type and ledger type."""
    Ls = _ledgers(db)
    VL, V = models.VoucherLine, models.Voucher
    q = (db.query(V.date, VL.ledger_id, func.sum(VL.debit), func.sum(VL.credit))
           .join(V, V.id == VL.voucher_id).filter(V.status == "posted"))
    if date_from:
        q = q.filter(V.date >= date_from)
    if date_to:
        q = q.filter(V.date <= date_to)
    if locations:
        q = q.filter(V.location.in_(locations))
    if vtypes:
        q = q.filter(V.vtype.in_(vtypes))
    agg = defaultdict(lambda: [0.0, 0.0])
    for d, lid, dr, cr in q.group_by(V.date, VL.ledger_id):
        L = Ls.get(lid)
        if not L or (groups and L.group not in groups):
            continue
        key = (L.group if mode == "group" else L.name if mode == "ledger"
               else (d or "")[:7])
        agg[key][0] += dr or 0
        agg[key][1] += cr or 0
    rows = [{"key": k, "debit": r2(v[0]), "credit": r2(v[1]), "net": r2(v[0] - v[1])}
            for k, v in sorted(agg.items())]
    return {"mode": mode, "rows": rows,
            "totals": {"debit": r2(sum(r["debit"] for r in rows)),
                       "credit": r2(sum(r["credit"] for r in rows))}}


def gst_inbox(db, date_from=None, date_to=None, supplier=None):
    """Every purchase invoice's GST as the books carry it, checked for what a
    GSTR-2B match needs: the supplier's GSTIN, and a tax that adds up."""
    q = db.query(models.Purchase).options(joinedload(models.Purchase.supplier)).filter(
        models.Purchase.status == "posted")
    rows = []
    for p in q.order_by(models.Purchase.id.desc()):
        d = iso(p.invoice_date, p.created_at)
        if date_from and d < date_from or date_to and d > date_to:
            continue
        s = p.supplier
        if supplier and (not s or supplier.lower() not in s.name.lower()):
            continue
        issues = []
        if not s or not s.gstin:
            issues.append("GSTIN missing")
        elif not re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", s.gstin.strip().upper()):
            issues.append("GSTIN malformed")
        if r2(p.taxable_total) and r2(p.tax_total) == 0:
            issues.append("No tax")
        if r2(r2(p.taxable_total) + r2(p.tax_total) - r2(p.grand_total)) and abs(
                r2(p.taxable_total) + r2(p.tax_total) - r2(p.grand_total)) >= 1:
            issues.append("Totals differ")
        rate = r2(100 * p.tax_total / p.taxable_total) if p.taxable_total else 0
        rows.append({"purchase_id": p.id, "date": d, "invoice_no": p.invoice_number,
                     "grn_no": p.grn_no, "supplier": s.name if s else "—",
                     "gstin": s.gstin if s else None, "state_code": s.state_code if s else None,
                     "taxable": r2(p.taxable_total), "tax": r2(p.tax_total),
                     "total": r2(p.grand_total), "rate": rate, "issues": issues})
    tot = {k: r2(sum(r[k] for r in rows)) for k in ("taxable", "tax", "total")}
    tot["flagged"] = sum(1 for r in rows if r["issues"])
    return {"rows": rows, "totals": tot}


def opening_balances(db):
    rows = [ledger_out(L) for L in db.query(models.Ledger)
            .filter(models.Ledger.active.is_(True)).order_by(models.Ledger.group, models.Ledger.name)]
    dr = r2(sum(r["opening_balance"] for r in rows if r["opening_balance"] > 0))
    cr = r2(-sum(r["opening_balance"] for r in rows if r["opening_balance"] < 0))
    return {"rows": rows, "debit": dr, "credit": cr, "difference": r2(dr - cr)}


def save_openings(db, rows):
    n = 0
    for r in rows:
        L = db.get(models.Ledger, int(r["ledger_id"]))
        if L:
            L.opening_balance = r2(r.get("opening_balance"))
            n += 1
    return n


# ---------------------------------------------------------------------------
#  Incentive — the store's staff commission, read from the till
# ---------------------------------------------------------------------------
def incentive(db, date_from=None, date_to=None, location=None, remove_zero=False):
    if not pos_sales.available():
        return {"available": False, "rows": [], "totals": {}}
    q = pos_sales.q
    where, params = pos_sales._window(date_from, date_to, "i.invoice_date")
    if location:
        where += " AND l.name = ?"
        params.append(location)
    rows = pos_sales._rows(
        "SELECT u.id, u.full_name, u.username, COALESCE(u.commission_pct, 0), "
        "COUNT(DISTINCT i.id), SUM(ii.quantity), SUM(ii.line_total) "
        f"FROM {q('invoice_items')} ii JOIN {q('invoices')} i ON i.id = ii.invoice_id "
        f"JOIN {q('users')} u ON u.id = COALESCE(ii.staff_id, i.staff_id) "
        f"LEFT JOIN {q('locations')} l ON l.id = i.location_id "
        "WHERE 1=1" + pos_sales.LIVE_BILL + where + " GROUP BY u.id, u.full_name, u.username, u.commission_pct",
        params)
    out = []
    for uid, name, login, pct, bills, qty, value in rows:
        inc = r2((value or 0) * (pct or 0) / 100)
        if remove_zero and not inc:
            continue
        out.append({"staff_id": uid, "employee": name or login, "code": login,
                    "bills": bills, "qty": r2(qty), "sales": r2(value), "pct": r2(pct),
                    "incentive": inc})
    out.sort(key=lambda r: -r["sales"])
    tot = {k: r2(sum(r[k] for r in out)) for k in ("qty", "sales", "incentive")}
    return {"available": True, "rows": out, "totals": tot}


# ---------------------------------------------------------------------------
#  Tally
# ---------------------------------------------------------------------------
def tally_config(db, business_id):
    c = db.query(models.TallyConfig).filter(models.TallyConfig.business_id == business_id).first()
    return {"business_id": business_id, "tally_company": c.tally_company if c else "",
            "host": c.host if c else "", "port": c.port if c else 9000,
            "mapping": (c.mapping if c else None) or {}}


def save_tally_config(db, business_id, data):
    c = db.query(models.TallyConfig).filter(models.TallyConfig.business_id == business_id).first()
    if not c:
        c = models.TallyConfig(business_id=business_id)
        db.add(c)
    for k in ("tally_company", "host", "port", "mapping"):
        if k in data:
            setattr(c, k, data[k])
    return c


def _tally_name(cfg, L):
    """What Tally calls this ledger: the auto-posting ledgers by their role
    (Tally Configuration's Purchase and Sales tabs), any other by its own id,
    and otherwise its name here."""
    m = cfg.get("mapping") or {}
    if L.source == "system" and (m.get("system") or {}).get(L.alias):
        return m["system"][L.alias]
    return (m.get("ledgers") or {}).get(str(L.id)) or L.name


def tally_xml(db, vouchers, cfg):
    vt_map = (cfg.get("mapping") or {}).get("voucher_types") or {}
    out = []
    for V in vouchers:
        d = (V.date or "").replace("-", "")
        entries = []
        for ln in V.lines:
            amt = r2(ln.debit or 0) or -r2(ln.credit or 0)
            entries.append(
                "<ALLLEDGERENTRIES.LIST>"
                f"<LEDGERNAME>{escape(_tally_name(cfg, ln.ledger))}</LEDGERNAME>"
                f"<ISDEEMEDPOSITIVE>{'Yes' if amt > 0 else 'No'}</ISDEEMEDPOSITIVE>"
                f"<AMOUNT>{-amt:.2f}</AMOUNT>"
                "</ALLLEDGERENTRIES.LIST>")
        vt = escape(vt_map.get(V.vtype) or V.vtype)
        out.append(
            f'<TALLYMESSAGE xmlns:UDF="TallyUDF"><VOUCHER VCHTYPE="{vt}" ACTION="Create">'
            f"<DATE>{d}</DATE><VOUCHERTYPENAME>{vt}</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{escape(V.voucher_no or '')}</VOUCHERNUMBER>"
            f"<NARRATION>{escape(V.narration or '')}</NARRATION>"
            + "".join(entries) + "</VOUCHER></TALLYMESSAGE>")
    company = escape(cfg.get("tally_company") or "")
    return ("<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY>"
            "<IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
            f"<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>"
            "</REQUESTDESC><REQUESTDATA>" + "".join(out)
            + "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")


def tally_push(db, business_id, voucher_ids):
    cfg = tally_config(db, business_id)
    if not cfg["host"]:
        raise ValueError("Set the Tally machine and port on Tally Configuration first")
    Vs = (db.query(models.Voucher).options(joinedload(models.Voucher.lines)
          .joinedload(models.VoucherLine.ledger))
          .filter(models.Voucher.id.in_(voucher_ids)).all())
    if not Vs:
        raise ValueError("Tick the vouchers to transfer")
    xml = tally_xml(db, Vs, cfg)
    url = f"http://{cfg['host']}:{cfg['port'] or 9000}"
    try:
        req = urllib.request.Request(url, data=xml.encode("utf-8"),
                                     headers={"Content-Type": "text/xml"})
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = r.read().decode("utf-8", "replace")
    except OSError as e:
        raise ValueError(f"Tally did not answer at {url} — is it open with ODBC/HTTP on? ({e})")
    created = re.search(r"<CREATED>(\d+)</CREATED>", reply)
    errors = re.search(r"<ERRORS>(\d+)</ERRORS>", reply)
    status = "ok" if errors is None or errors.group(1) == "0" else "error"
    stamp = dt.datetime.utcnow()
    for V in Vs:
        V.tally_synced_at, V.tally_status = stamp, status
    return {"status": status, "created": int(created.group(1)) if created else 0,
            "errors": int(errors.group(1)) if errors else 0, "reply": reply[:2000]}


# ---------------------------------------------------------------------------
#  What the screens' dropdowns offer
# ---------------------------------------------------------------------------
def options(db):
    biz = [{"id": b.id, "code": b.code, "name": b.legal_name or b.name, "is_default": bool(b.is_default)}
           for b in db.query(models.Business).filter(models.Business.active.is_(True))
           .order_by(models.Business.id)]
    locs = sorted({w.name for w in db.query(models.Warehouse).filter(models.Warehouse.active.is_(True))}
                  | {s.name for s in db.query(models.Store).filter(models.Store.active.is_(True))})
    banks = [{"id": L.id, "name": L.name} for L in db.query(models.Ledger)
             .filter(models.Ledger.group == "Bank Account", models.Ledger.active.is_(True))
             .order_by(models.Ledger.name)]
    cash = [{"id": L.id, "name": L.name} for L in db.query(models.Ledger)
            .filter(models.Ledger.group.in_(CASH_GROUPS), models.Ledger.active.is_(True))
            .order_by(models.Ledger.name)]
    return {"groups": [{"name": g, "nature": n, "section": s} for g, (n, s) in GROUPS.items()],
            "filters": [{"key": k, "label": lbl} for k, lbl in LEDGER_FILTERS],
            "tags": [{"key": k, "label": lbl} for k, lbl in LEDGER_TAGS],
            "vtypes": VTYPES, "modes": MODES, "businesses": biz, "locations": locs,
            "banks": banks, "cash": cash,
            "transports": [t.name for t in db.query(models.Transport).order_by(models.Transport.name)],
            "agents": [a.name for a in db.query(models.Agent).order_by(models.Agent.name)],
            "petty_groups": PETTY_GROUPS}
