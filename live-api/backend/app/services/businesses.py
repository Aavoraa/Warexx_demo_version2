"""The legal entities this platform runs, and keeping them in step with the till.

WHY THIS EXISTS. Company identity used to be two environment variables —
ESSA_COMPANY_NAME and ESSA_COMPANY_GSTIN — which is exactly right for one company
and wrong for two. Essa and Taqua carry different GSTINs and file separate
returns; a month's trading that cannot be split between them is a month's work to
unpick afterwards.

THE TILL ALREADY KNEW. The shop has carried its own `companies` table, with a
GSTIN per row, for as long as it has had a picker, and every invoice it raises
carries `company_id`. So this is not a new idea being introduced — it is the
warehouse half of an arrangement that already exists downstairs, and the two are
matched on GSTIN, which is the one thing about a legal entity that does not get
retyped differently in two places.

NAMES ARE NOT THE KEY, deliberately. Stores are matched to the shop by name and
that is a known weakness (services/locations.rename_warning). A GSTIN is issued
by the government, is printed on every invoice, and nobody invents a second
spelling of it.
"""
import re

from sqlalchemy.orm import Session

from .. import models
from ..config import COMPANY_NAME, COMPANY_GSTIN


def _code_from(name: str) -> str:
    """A short upper-case code from a name — ESSA from "Essa Garments Pvt Ltd"."""
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return "BIZ"
    first = words[0].upper()
    return first[:8] if len(first) >= 3 else "".join(w[0] for w in words).upper()[:8]


def default_business(db: Session):
    """The business a warehouse belongs to when nobody has said which."""
    b = (db.query(models.Business)
           .filter(models.Business.is_default.is_(True)).first())
    return b or db.query(models.Business).order_by(models.Business.id).first()


def resolve_id(db: Session, business_id=None):
    if business_id:
        b = db.get(models.Business, int(business_id))
        if b:
            return b.id
    b = default_business(db)
    return b.id if b else None


def for_warehouse(db: Session, warehouse_id=None):
    if warehouse_id:
        wh = db.get(models.Warehouse, int(warehouse_id))
        if wh and wh.business_id:
            b = db.get(models.Business, wh.business_id)
            if b:
                return b
    return default_business(db)


def out(b: models.Business, db: Session = None) -> dict:
    d = {"id": b.id, "uuid": b.uuid, "code": b.code, "name": b.name,
         "legal_name": b.legal_name, "gstin": b.gstin, "pan": b.pan,
         "address": b.address, "city": b.city, "state": b.state,
         "state_code": b.state_code, "country": b.country, "pincode": b.pincode,
         "phone": b.phone, "email": b.email,
         "currency": b.currency, "timezone": b.timezone,
         "fy_start_month": b.fy_start_month,
         "is_default": bool(b.is_default), "active": bool(b.active)}
    if db is not None:
        d["warehouse_count"] = db.query(models.Warehouse).filter(
            models.Warehouse.business_id == b.id).count()
    return d


def ensure_seed(db: Session) -> dict:
    """Create the business this install has always been, and file its warehouses.

    Idempotent, and a no-op once done. The company already exists in every
    meaningful sense — it is on every invoice this app has printed, from config —
    so this writes down what is already true rather than asking for it. A
    warehouse with no business is filed under it for the same reason: every
    building that predates this table belongs to the one company there was.
    """
    made = {"businesses": 0, "warehouses_filed": 0, "sequences": 0}

    b = db.query(models.Business).filter(
        models.Business.gstin == COMPANY_GSTIN).first() if COMPANY_GSTIN else None
    if b is None:
        b = default_business(db)
    if b is None:
        b = models.Business(
            code=_code_from(COMPANY_NAME), name=COMPANY_NAME.split()[0],
            legal_name=COMPANY_NAME, gstin=COMPANY_GSTIN or None,
            # The GSTIN's first two digits ARE the state code — reading it off is
            # more reliable than asking somebody to type it a second time.
            state_code=(COMPANY_GSTIN or "")[:2] or None,
            country="India", currency="INR", timezone="Asia/Kolkata",
            fy_start_month=4, is_default=True, active=True)
        db.add(b)
        db.commit()
        db.refresh(b)
        made["businesses"] += 1

    # exactly one default, or `default_business` depends on row order
    if not db.query(models.Business).filter(
            models.Business.is_default.is_(True)).first():
        b.is_default = True
        db.commit()

    made["warehouses_filed"] = db.query(models.Warehouse).filter(
        models.Warehouse.business_id.is_(None)).update(
        {"business_id": b.id}, synchronize_session=False)
    if made["warehouses_filed"]:
        db.commit()

    # Backfill the uuid on rows written before the column existed. Done here
    # rather than by a default, because a default only fires on INSERT.
    blank = db.query(models.Warehouse).filter(
        models.Warehouse.uuid.is_(None)).all()
    if blank:
        import uuid as _uuid
        for wh in blank:
            wh.uuid = str(_uuid.uuid4())
        db.commit()
    return made


def sync_from_shop(db: Session) -> dict:
    """Adopt the till's companies as businesses, matched on GSTIN.

    The shop's `companies` table is where a second entity first appears in
    practice — somebody adds it at the counter because a bill has to carry the
    right GSTIN today. Read here so the warehouse learns about it rather than
    requiring the same company to be typed twice, and matched on GSTIN because a
    name gets spelled two ways and a registration number does not.

    Additive and read-only toward the shop: nothing here writes to it, and a
    company that exists upstairs and not down there is left alone.
    """
    from . import pos_sales
    if not pos_sales.available():
        return {"checked": False, "reason": "the shop could not be read",
                "created": 0, "linked": 0}

    rows = pos_sales._rows(
        "SELECT id, name, gstin, address, state_code, phone "
        "FROM " + pos_sales.q("companies"), [])
    created = linked = 0
    for _cid, name, gstin, address, state_code, phone in rows:
        gst = (gstin or "").strip() or None
        have = None
        if gst:
            have = db.query(models.Business).filter(
                models.Business.gstin == gst).first()
        if have is None:
            have = db.query(models.Business).filter(
                models.Business.name == (name or "").strip()).first()
        if have is not None:
            # fill a blank the warehouse never knew, never overwrite an answer
            if gst and not have.gstin:
                have.gstin = gst
                linked += 1
            continue
        code = _code_from(name)
        n = 1
        while db.query(models.Business).filter(models.Business.code == code).first():
            n += 1
            code = f"{_code_from(name)[:6]}{n}"
        db.add(models.Business(
            code=code, name=(name or "").strip() or code,
            legal_name=(name or "").strip() or None, gstin=gst,
            address=address, state_code=state_code or (gst or "")[:2] or None,
            phone=phone, country="India", currency="INR",
            timezone="Asia/Kolkata", fy_start_month=4, active=True))
        created += 1
    if created or linked:
        db.commit()
    return {"checked": True, "created": created, "linked": linked,
            "shop_companies": len(rows)}
