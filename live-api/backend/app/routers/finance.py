"""Finance — ledgers, vouchers and the books (services/finance).

Company-wide, like supplier payments: one bank account pays bills from every
warehouse, so the books are not split by building. A location is a field on a
voucher that reports can filter by, not a wall between them.

Every read that reports on the books first brings them up to date with the
warehouse and the shop (`sync_books`), so a GRN posted a minute ago is already
in the daybook. It is idempotent and only writes what is missing.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..database import get_db
from ..fastjson import returns_json
from ..services import finance as svc
from ..services import businesses

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _user(request: Request):
    return getattr(request.state, "user", None) or {}


def _synced(db):
    svc.sync_books(db)
    db.commit()


def _fail(e):
    if isinstance(e, LookupError):
        raise HTTPException(404, str(e))
    raise HTTPException(400, str(e))


def _list(v):
    if not v:
        return None
    out = [x for x in (v if isinstance(v, list) else str(v).split(",")) if x not in ("", None)]
    return out or None


# ---------------------------------------------------------------- options
@router.get("/options")
def options(db: Session = Depends(get_db)):
    svc.ensure_defaults(db)
    db.commit()
    return svc.options(db)


@router.post("/sync")
def sync(db: Session = Depends(get_db)):
    made = svc.sync_books(db)
    db.commit()
    return made


# ---------------------------------------------------------------- ledgers
class LedgerIn(BaseModel):
    name: str
    group: str
    alias: Optional[str] = None
    business_id: Optional[int] = None
    location: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    state: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    bank_account: Optional[str] = None
    ifsc: Optional[str] = None
    billwise: bool = False
    credit_days: Optional[int] = None
    tds_applicable: bool = False
    tds_section: Optional[str] = None
    tds_rate: Optional[float] = None
    tags: List[str] = []
    opening_balance: float = 0.0


@returns_json(router.get("/ledgers"))
def ledgers(group: Optional[str] = None, filter: Optional[str] = None,
            name: Optional[str] = None, groups: Optional[str] = None,
            db: Session = Depends(get_db)):
    _synced(db)
    return svc.list_ledgers(db, group, filter, name, _list(groups))


@router.post("/ledgers")
def create_ledger(body: LedgerIn, db: Session = Depends(get_db)):
    try:
        L = svc.save_ledger(db, body.model_dump())
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.ledger_out(L)


@router.put("/ledgers/{lid}")
def update_ledger(lid: int, body: LedgerIn, db: Session = Depends(get_db)):
    try:
        L = svc.save_ledger(db, body.model_dump(), lid)
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.ledger_out(L)


@router.delete("/ledgers/{lid}")
def delete_ledger(lid: int, db: Session = Depends(get_db)):
    try:
        r = svc.delete_ledger(db, lid)
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return r


@returns_json(router.get("/ledgers/{lid}/statement"))
def statement(lid: int, date_from: Optional[str] = None, date_to: Optional[str] = None,
              db: Session = Depends(get_db)):
    _synced(db)
    try:
        return svc.ledger_statement(db, lid, date_from, date_to)
    except LookupError as e:
        _fail(e)


@returns_json(router.get("/ledgers/{lid}/bills"))
def bills(lid: int, db: Session = Depends(get_db)):
    _synced(db)
    try:
        return svc.party_bills(db, lid)
    except LookupError as e:
        _fail(e)


# ---------------------------------------------------------------- vouchers
class LineIn(BaseModel):
    ledger_id: int
    debit: float = 0.0
    credit: float = 0.0
    narration: Optional[str] = None
    bill_ref: Optional[str] = None


class VoucherIn(BaseModel):
    vtype: str
    date: Optional[str] = None
    business_id: Optional[int] = None
    location: Optional[str] = None
    mode: Optional[str] = None
    ref_no: Optional[str] = None
    cheque_no: Optional[str] = None
    cheque_date: Optional[str] = None
    narration: Optional[str] = None
    party_ledger_id: Optional[int] = None
    lines: List[LineIn]


@returns_json(router.get("/vouchers"))
def vouchers(date_from: Optional[str] = None, date_to: Optional[str] = None,
             vtype: Optional[str] = None, mode: Optional[str] = None,
             ledger_id: Optional[int] = None, voucher_no: Optional[str] = None,
             amount_min: Optional[float] = None, amount_max: Optional[float] = None,
             business_id: Optional[int] = None, location: Optional[str] = None,
             db: Session = Depends(get_db)):
    _synced(db)
    return svc.list_vouchers(db, date_from, date_to, _list(vtype), mode, ledger_id,
                             voucher_no, amount_min, amount_max, business_id, _list(location))


@router.get("/vouchers/{vid}")
def voucher(vid: int, db: Session = Depends(get_db)):
    V = db.get(models.Voucher, vid)
    if not V:
        raise HTTPException(404, "voucher not found")
    return svc.voucher_out(V)


@router.post("/vouchers")
def create_voucher(body: VoucherIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.save_voucher(db, body.model_dump(), _user(request))
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


@router.put("/vouchers/{vid}")
def update_voucher(vid: int, body: VoucherIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.save_voucher(db, body.model_dump(), _user(request), vid)
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


@router.delete("/vouchers/{vid}")
def cancel_voucher(vid: int, db: Session = Depends(get_db)):
    try:
        V = svc.cancel_voucher(db, vid)
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


# ---------------------------------------------------------------- statements
@returns_json(router.get("/daybook"))
def daybook(date_from: Optional[str] = None, date_to: Optional[str] = None,
            vtype: Optional[str] = None, location: Optional[str] = None,
            business_id: Optional[int] = None, db: Session = Depends(get_db)):
    _synced(db)
    return svc.list_vouchers(db, date_from, date_to, _list(vtype), None, None, None,
                             None, None, business_id, _list(location), limit=5000)


@router.get("/trial-balance")
def trial_balance(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  business_id: Optional[int] = None, location: Optional[str] = None,
                  db: Session = Depends(get_db)):
    _synced(db)
    return svc.trial_balance(db, date_from, date_to, business_id, _list(location))


@router.get("/trading")
def trading(date_from: Optional[str] = None, date_to: Optional[str] = None,
            business_id: Optional[int] = None, location: Optional[str] = None,
            opening_stock: Optional[float] = None, closing_stock: Optional[float] = None,
            db: Session = Depends(get_db)):
    _synced(db)
    return svc.trading_account(db, date_from, date_to, business_id, _list(location),
                               opening_stock, closing_stock)


@router.get("/pl")
def pl(date_from: Optional[str] = None, date_to: Optional[str] = None,
       business_id: Optional[int] = None, location: Optional[str] = None,
       opening_stock: Optional[float] = None, closing_stock: Optional[float] = None,
       db: Session = Depends(get_db)):
    _synced(db)
    return svc.profit_and_loss(db, date_from, date_to, business_id, _list(location),
                               opening_stock, closing_stock)


@router.get("/balance-sheet")
def balance_sheet(as_on: Optional[str] = None, business_id: Optional[int] = None,
                  location: Optional[str] = None, closing_stock: Optional[float] = None,
                  db: Session = Depends(get_db)):
    _synced(db)
    return svc.balance_sheet(db, as_on, business_id, _list(location), closing_stock)


@router.get("/daily-pl")
def daily_pl(date_from: Optional[str] = None, date_to: Optional[str] = None,
             business_id: Optional[int] = None, location: Optional[str] = None,
             db: Session = Depends(get_db)):
    _synced(db)
    return svc.daily_pl(db, date_from, date_to, business_id, location)


@router.get("/outstanding/{side}")
def outstanding(side: str, as_on: Optional[str] = None, location: Optional[str] = None,
                q: Optional[str] = None, db: Session = Depends(get_db)):
    _synced(db)
    groups = ["Sundry Creditors"] if side == "payable" else ["Sundry Debtors"]
    return svc.outstanding(db, groups, as_on, _list(location), q)


@router.get("/analyser")
def analyser(date_from: Optional[str] = None, date_to: Optional[str] = None,
             location: Optional[str] = None, vtype: Optional[str] = None,
             group: Optional[str] = None, mode: str = "group", db: Session = Depends(get_db)):
    _synced(db)
    return svc.analyser(db, date_from, date_to, _list(location), _list(vtype), _list(group), mode)


@router.get("/tds")
def tds(date_from: Optional[str] = None, date_to: Optional[str] = None,
        location: Optional[str] = None, vtype: Optional[str] = None,
        ledger: Optional[str] = None, db: Session = Depends(get_db)):
    _synced(db)
    return svc.tds_transactions(db, date_from, date_to, _list(location), _list(vtype), _list(ledger))


@router.get("/gst-inbox")
def gst_inbox(date_from: Optional[str] = None, date_to: Optional[str] = None,
              supplier: Optional[str] = None, db: Session = Depends(get_db)):
    return svc.gst_inbox(db, date_from, date_to, supplier)


@router.get("/loans")
def loans(as_on: Optional[str] = None, date_from: Optional[str] = None,
          db: Session = Depends(get_db)):
    _synced(db)
    return svc.loans(db, as_on, date_from)


@router.get("/incentive")
def incentive(date_from: Optional[str] = None, date_to: Optional[str] = None,
              location: Optional[str] = None, remove_zero: bool = False,
              db: Session = Depends(get_db)):
    return svc.incentive(db, date_from, date_to, location, remove_zero)


# ---------------------------------------------------------------- BRS & cheques
@router.get("/brs")
def brs(bank_id: int, date_from: Optional[str] = None, date_to: Optional[str] = None,
        status: Optional[str] = None, modes: Optional[str] = None, db: Session = Depends(get_db)):
    _synced(db)
    try:
        return svc.brs(db, bank_id, date_from, date_to, status, _list(modes))
    except LookupError as e:
        _fail(e)


class ClearIn(BaseModel):
    cleared_on: Optional[str] = None


@router.put("/brs/line/{line_id}")
def clear_line(line_id: int, body: ClearIn, db: Session = Depends(get_db)):
    try:
        ln = svc.set_cleared(db, line_id, body.cleared_on)
    except LookupError as e:
        _fail(e)
    db.commit()
    return {"id": ln.id, "cleared_on": ln.cleared_on}


class ChequeBookIn(BaseModel):
    bank_ledger_id: int
    book_no: Optional[str] = None
    from_no: int
    to_no: int
    issued_on: Optional[str] = None
    remarks: Optional[str] = None


@router.get("/cheque-books")
def cheque_books(bank_id: Optional[int] = None, db: Session = Depends(get_db)):
    _synced(db)
    return svc.cheque_books(db, bank_id)


@router.post("/cheque-books")
def add_cheque_book(body: ChequeBookIn, db: Session = Depends(get_db)):
    if body.to_no < body.from_no or body.to_no - body.from_no > 500:
        raise HTTPException(400, "The leaf range must run upwards, and a book holds at most 500")
    b = models.ChequeBook(**body.model_dump(), cancelled=[])
    b.issued_on = svc.iso(b.issued_on) if b.issued_on else svc.today()
    db.add(b)
    db.commit()
    return {"id": b.id}


class LeafIn(BaseModel):
    no: int
    reason: Optional[str] = None
    undo: bool = False


@router.post("/cheque-books/{bid}/cancel")
def cancel_leaf(bid: int, body: LeafIn, db: Session = Depends(get_db)):
    b = db.get(models.ChequeBook, bid)
    if not b:
        raise HTTPException(404, "cheque book not found")
    keep = [c for c in (b.cancelled or []) if int(c["no"]) != body.no]
    if not body.undo:
        keep.append({"no": body.no, "reason": body.reason or "cancelled"})
    b.cancelled = keep
    db.commit()
    return {"ok": True}


@router.delete("/cheque-books/{bid}")
def delete_cheque_book(bid: int, db: Session = Depends(get_db)):
    b = db.get(models.ChequeBook, bid)
    if b:
        db.delete(b)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- payment screens
@router.get("/transport/pending")
def transport_pending(transport: Optional[str] = None, date_from: Optional[str] = None,
                      date_to: Optional[str] = None, db: Session = Depends(get_db)):
    _synced(db)
    return svc.transport_pending(db, transport, date_from, date_to)


@router.get("/agent/pending")
def agent_pending(agent: Optional[str] = None, date_from: Optional[str] = None,
                  date_to: Optional[str] = None, db: Session = Depends(get_db)):
    _synced(db)
    return svc.agent_pending(db, agent, date_from, date_to)


class PayRow(BaseModel):
    lr_id: int
    amount: float
    discount: float = 0.0


class PayIn(BaseModel):
    party: str
    date: Optional[str] = None
    mode: Optional[str] = None
    bank_ledger_id: Optional[int] = None
    cheque_no: Optional[str] = None
    cheque_date: Optional[str] = None
    ref_no: Optional[str] = None
    remarks: Optional[str] = None
    tds: float = 0.0
    business_id: Optional[int] = None
    location: Optional[str] = None
    rows: List[PayRow]


@router.post("/transport/pay")
def transport_pay(body: PayIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.pay_against(db, body.model_dump(), _user(request), kind="transport")
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


@router.post("/agent/pay")
def agent_pay(body: PayIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.pay_against(db, body.model_dump(), _user(request), kind="agent")
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


class AllocIn(BaseModel):
    bill_ref: str
    amount: float


class CollectIn(BaseModel):
    ledger_id: int
    date: Optional[str] = None
    mode: Optional[str] = None
    bank_ledger_id: Optional[int] = None
    ref_no: Optional[str] = None
    remarks: Optional[str] = None
    paid: float = 0.0
    discount: float = 0.0
    tds: float = 0.0
    business_id: Optional[int] = None
    location: Optional[str] = None
    allocations: List[AllocIn] = []


@router.post("/collections")
def collect(body: CollectIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.collect(db, body.model_dump(), _user(request))
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


class CNRow(BaseModel):
    rate: float = 0.0
    value: float = 0.0


class CreditNoteIn(BaseModel):
    ledger_id: int
    date: Optional[str] = None
    bill_no: Optional[str] = None
    bill_ref: Optional[str] = None
    remarks: Optional[str] = None
    igst: bool = False
    business_id: Optional[int] = None
    location: Optional[str] = None
    rows: List[CNRow]


@router.post("/credit-notes")
def credit_note(body: CreditNoteIn, request: Request, db: Session = Depends(get_db)):
    try:
        V = svc.credit_note(db, body.model_dump(), _user(request))
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return svc.voucher_out(V)


# ---------------------------------------------------------------- petty cash
@router.get("/petty-cash")
def petty(cash_ledger_id: int, month: str, location: Optional[str] = None,
          db: Session = Depends(get_db)):
    return svc.petty_sheet(db, cash_ledger_id, month, location)


class PettyIn(BaseModel):
    cash_ledger_id: int
    month: str
    location: Optional[str] = None
    business_id: Optional[int] = None
    touched: List[int] = []
    rows: List[dict]


@router.post("/petty-cash")
def petty_save(body: PettyIn, request: Request, db: Session = Depends(get_db)):
    try:
        r = svc.petty_save(db, body.model_dump(), _user(request))
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return r


# ---------------------------------------------------------------- opening balance
@router.get("/opening")
def opening(db: Session = Depends(get_db)):
    _synced(db)
    return svc.opening_balances(db)


class OpeningRow(BaseModel):
    ledger_id: int
    opening_balance: float


@router.put("/opening")
def save_opening(rows: List[OpeningRow], db: Session = Depends(get_db)):
    n = svc.save_openings(db, [r.model_dump() for r in rows])
    db.commit()
    return {"saved": n}


# ---------------------------------------------------------------- tally
@router.get("/tally/config")
def tally_config(business_id: Optional[int] = None, db: Session = Depends(get_db)):
    return svc.tally_config(db, businesses.resolve_id(db, business_id))


class TallyIn(BaseModel):
    business_id: Optional[int] = None
    tally_company: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = 9000
    mapping: dict = {}


@router.put("/tally/config")
def save_tally(body: TallyIn, db: Session = Depends(get_db)):
    bid = businesses.resolve_id(db, body.business_id)
    svc.save_tally_config(db, bid, body.model_dump())
    db.commit()
    return svc.tally_config(db, bid)


class TallyPush(BaseModel):
    business_id: Optional[int] = None
    voucher_ids: List[int]


@router.get("/tally/xml", response_class=PlainTextResponse)
def tally_xml(ids: str = Query(...), business_id: Optional[int] = None,
              db: Session = Depends(get_db)):
    cfg = svc.tally_config(db, businesses.resolve_id(db, business_id))
    Vs = (db.query(models.Voucher).options(joinedload(models.Voucher.lines)
          .joinedload(models.VoucherLine.ledger))
          .filter(models.Voucher.id.in_([int(x) for x in ids.split(",") if x.isdigit()])).all())
    return PlainTextResponse(svc.tally_xml(db, Vs, cfg), media_type="application/xml",
                             headers={"Content-Disposition": 'attachment; filename="tally-vouchers.xml"'})


@router.post("/tally/push")
def tally_push(body: TallyPush, db: Session = Depends(get_db)):
    try:
        r = svc.tally_push(db, businesses.resolve_id(db, body.business_id), body.voucher_ids)
    except (ValueError, LookupError) as e:
        _fail(e)
    db.commit()
    return r
