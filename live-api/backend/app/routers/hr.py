"""HR — employees, attendance, shifts, leave, payroll, performance (services/hr).

Company-wide like Finance: a person may move between the warehouse and the
stores, and one payroll pays them all. Admin at both ends (security.POLICY),
because every screen here carries somebody's pay or personal details.

The one route outside the sign-in is the attendance devices' punch feed,
/api/hr/device/punches. A biometric reader has no account to sign in with, so it
sends the device key shown on HR Settings instead — checked here, the way
/api/admin/boot checks its own secret.
"""
import hmac
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import hr as svc
from ..services import hr_read

router = APIRouter(prefix="/api/hr", tags=["hr"])


def _user(request: Request):
    return getattr(request.state, "user", None) or {}


def _fail(e):
    if isinstance(e, LookupError):
        raise HTTPException(404, str(e).strip("'\""))
    raise HTTPException(400, str(e))


def _do(db, fn, *a, **k):
    try:
        out = fn(*a, **k)
    except (ValueError, LookupError) as e:
        db.rollback()
        _fail(e)
    db.commit()
    return out


# ---------------------------------------------------------------- options, settings
@router.get("/options")
def options(db: Session = Depends(get_db)):
    out = svc.options(db)
    db.commit()
    return out


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    out = svc.settings(db)
    db.commit()
    return out


@router.put("/settings")
def put_settings(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_settings, db, body)


@router.post("/settings/device-key")
def device_key(db: Session = Depends(get_db)):
    return {"device_key": _do(db, svc.new_device_key, db)}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    out = svc.dashboard(db)
    db.commit()
    return out


# ---------------------------------------------------------------- masters
@router.get("/masters/{kind}")
def masters(kind: str, db: Session = Depends(get_db)):
    if kind not in svc.MASTERS:
        raise HTTPException(404, "unknown list")
    return svc.list_master(db, kind)


@router.post("/masters/{kind}")
def add_master(kind: str, body: Dict, db: Session = Depends(get_db)):
    if kind not in svc.MASTERS:
        raise HTTPException(404, "unknown list")
    return _do(db, svc.save_master, db, kind, body)


@router.put("/masters/{kind}/{mid}")
def edit_master(kind: str, mid: int, body: Dict, db: Session = Depends(get_db)):
    if kind not in svc.MASTERS:
        raise HTTPException(404, "unknown list")
    return _do(db, svc.save_master, db, kind, body, mid)


@router.delete("/masters/{kind}/{mid}")
def del_master(kind: str, mid: int, db: Session = Depends(get_db)):
    if kind not in svc.MASTERS:
        raise HTTPException(404, "unknown list")
    return _do(db, svc.delete_master, db, kind, mid)


# ---------------------------------------------------------------- employees
@router.get("/employees")
def employees(q: Optional[str] = None, department_id: Optional[int] = None,
              designation_id: Optional[int] = None, location: Optional[str] = None,
              status: Optional[str] = "current", employment_type: Optional[str] = None,
              db: Session = Depends(get_db)):
    n = svc.settle_exits(db)
    if n:
        db.commit()
    return svc.list_employees(db, q, department_id, designation_id, location, status, employment_type)


@router.get("/employees/{eid}")
def employee(eid: int, db: Session = Depends(get_db)):
    try:
        return svc.get_employee(db, eid)
    except LookupError as e:
        _fail(e)


@router.post("/employees")
def add_employee(body: Dict, request: Request, db: Session = Depends(get_db)):
    e = _do(db, svc.save_employee, db, body, _user(request))
    return svc.get_employee(db, e.id)


@router.put("/employees/{eid}")
def edit_employee(eid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    _do(db, svc.save_employee, db, body, _user(request), eid)
    return svc.get_employee(db, eid)


@router.post("/employees/{eid}/exit")
def exit_employee(eid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    _do(db, svc.exit_employee, db, eid, body, _user(request))
    return svc.get_employee(db, eid)


@router.post("/employees/{eid}/rejoin")
def rejoin(eid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    _do(db, svc.rejoin_employee, db, eid, body, _user(request))
    return svc.get_employee(db, eid)


@router.post("/employees/{eid}/history")
def add_history(eid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.add_history, db, eid, body, _user(request))


@router.post("/employees/{eid}/contracts")
def add_contract(eid: int, body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_contract, db, eid, body)


@router.put("/employees/{eid}/contracts/{cid}")
def edit_contract(eid: int, cid: int, body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_contract, db, eid, body, cid)


@router.delete("/employees/{eid}/contracts/{cid}")
def del_contract(eid: int, cid: int, db: Session = Depends(get_db)):
    _do(db, svc.delete_contract, db, eid, cid)
    return {"deleted": True}


@router.post("/read-document")
async def read_document(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """An ID proof, letter or resume read into employee-form fields. Nothing is
    stored: the screen shows what was read and files the document on saving."""
    raw = await file.read()
    try:
        return hr_read.read_document(db, raw, file.filename)
    except ValueError as e:
        _fail(e)
    except Exception as e:                       # the model or the network
        raise HTTPException(502, f"The document could not be read ({type(e).__name__}) — check the Claude key in Settings")


@router.post("/employees/{eid}/documents")
async def add_document(eid: int, request: Request, doc_type: str = Form(...), title: str = Form(""),
                       number: str = Form(""), issued_on: str = Form(""), expires_on: str = Form(""),
                       file: Optional[UploadFile] = File(None), db: Session = Depends(get_db)):
    raw = await file.read() if file is not None else None
    return _do(db, svc.save_document, db, eid,
               {"doc_type": doc_type, "title": title, "number": number, "issued_on": issued_on,
                "expires_on": expires_on}, raw, file.filename if file is not None else None, _user(request))


@router.put("/employees/{eid}/documents/{did}")
def edit_document(eid: int, did: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.save_document, db, eid, body, None, None, _user(request), did)


@router.get("/employees/{eid}/documents/{did}/file")
def document_file(eid: int, did: int, db: Session = Depends(get_db)):
    try:
        raw, mime, name = svc.document_file(db, eid, did)
    except LookupError as e:
        _fail(e)
    safe = "".join(c for c in name if c.isalnum() or c in "._- ") or "document"
    return Response(raw, media_type=mime, headers={"Content-Disposition": f'inline; filename="{safe}"'})


@router.delete("/employees/{eid}/documents/{did}")
def del_document(eid: int, did: int, db: Session = Depends(get_db)):
    _do(db, svc.delete_document, db, eid, did)
    return {"deleted": True}


@router.post("/employees/{eid}/photo")
async def upload_photo(eid: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """A new picture: kept as a Photo document and made the record's photo."""
    raw = await file.read()
    _do(db, svc.save_document, db, eid, {"doc_type": "Photo", "title": "Profile photo"}, raw, file.filename,
        _user(request))
    return svc.get_employee(db, eid)


@router.delete("/employees/{eid}/photo")
def delete_photo(eid: int, db: Session = Depends(get_db)):
    _do(db, svc.remove_photo, db, eid)
    return svc.get_employee(db, eid)


@router.get("/employees/{eid}/photo")
def photo(eid: int, db: Session = Depends(get_db)):
    try:
        raw, mime = svc.photo_file(db, eid)
    except LookupError as e:
        _fail(e)
    return Response(raw, media_type=mime, headers={"Cache-Control": "private, max-age=300"})


@router.get("/employees/{eid}/leave-ledger")
def leave_ledger(eid: int, leave_type_id: Optional[int] = None, db: Session = Depends(get_db)):
    return svc.leave_ledger(db, eid, leave_type_id)


@router.get("/employees/{eid}/payslips")
def employee_slips(eid: int, db: Session = Depends(get_db)):
    return svc.employee_slips(db, eid)


@router.get("/store-staff")
def store_staff(db: Session = Depends(get_db)):
    return svc.store_staff(db)


class Usernames(BaseModel):
    usernames: List[str]


@router.post("/store-staff/import")
def import_store_staff(body: Usernames, request: Request, db: Session = Depends(get_db)):
    return {"made": _do(db, svc.import_store_staff, db, body.usernames, _user(request))}


@router.get("/org-chart")
def org_chart(department_id: Optional[int] = None, location: Optional[str] = None,
              db: Session = Depends(get_db)):
    return svc.org_chart(db, department_id, location)


# ---------------------------------------------------------------- attendance
@router.get("/attendance")
def attendance(month: Optional[str] = None, department_id: Optional[int] = None,
               location: Optional[str] = None, db: Session = Depends(get_db)):
    out = svc.month_register(db, month, department_id, location)
    db.commit()
    return out


@router.put("/attendance/{eid}/{date}")
def mark_day(eid: int, date: str, body: Dict, request: Request, db: Session = Depends(get_db)):
    a = _do(db, svc.mark, db, eid, date, body, _user(request))
    return {"id": a.id, "status": a.status, "in_time": a.in_time, "out_time": a.out_time,
            "late": a.late_minutes, "ot": a.ot_minutes}


@router.delete("/attendance/{eid}/{date}")
def clear_day(eid: int, date: str, db: Session = Depends(get_db)):
    _do(db, svc.clear_day, db, eid, date)
    return {"cleared": True}


@router.post("/attendance/bulk")
def bulk(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.bulk_mark, db, body, _user(request))


@router.post("/attendance/import")
async def import_attendance(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = await file.read()
    return _do(db, svc.import_attendance, db, raw, file.filename, _user(request))


@router.post("/attendance/sync-store")
def sync_store(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.sync_store_attendance, db, body.get("date_from"), body.get("date_to"), _user(request))


@router.post("/device/punches")
def device_punches(body: Dict, request: Request, db: Session = Depends(get_db)):
    """Public in security.PUBLIC; authorised here by the device key."""
    sent = request.headers.get("x-attendance-key") or ""
    key = svc.settings(db).get("device_key") or ""
    db.commit()
    if not key or not sent or not hmac.compare_digest(sent, key):
        raise HTTPException(401, "Send the device key from HR Settings in the X-Attendance-Key header")
    return _do(db, svc.device_punches, db, body)


@router.get("/roster")
def roster(start: Optional[str] = None, department_id: Optional[int] = None,
           location: Optional[str] = None, db: Session = Depends(get_db)):
    return svc.roster_week(db, start, department_id, location)


@router.post("/roster")
def assign_roster(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.assign_roster, db, body)


@router.get("/overtime")
def overtime(date_from: str, date_to: str, status: Optional[str] = None, db: Session = Depends(get_db)):
    try:
        return svc.overtime(db, date_from, date_to, status)
    except ValueError as e:
        _fail(e)


@router.post("/overtime/decide")
def decide_overtime(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.decide_overtime, db, body.get("ids") or [], body.get("decision"),
               body.get("minutes"), _user(request))


# ---------------------------------------------------------------- leave
@router.get("/leave")
def leave(status: Optional[str] = None, employee_id: Optional[int] = None,
          date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    return svc.list_leave(db, status, employee_id, date_from, date_to)


@router.post("/leave")
def apply_leave(body: Dict, request: Request, db: Session = Depends(get_db)):
    r = _do(db, svc.apply_leave, db, body, _user(request))
    return svc.leave_out(r)


@router.post("/leave/{rid}/{decision}")
def decide_leave(rid: int, decision: str, body: Optional[Dict] = None, request: Request = None,
                 db: Session = Depends(get_db)):
    r = _do(db, svc.decide_leave, db, rid, decision, (body or {}).get("note"), _user(request))
    return svc.leave_out(r)


@router.get("/leave-balances")
def leave_balances(employee_id: int, as_on: Optional[str] = None, db: Session = Depends(get_db)):
    return svc.leave_balances(db, employee_id, as_on)


@router.post("/leave-adjust")
def leave_adjust(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.adjust_leave, db, body, _user(request))


@router.post("/leave-accrual")
def leave_accrual(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.run_accrual, db, body.get("month"), _user(request))


@router.post("/leave-year-end")
def leave_year_end(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.year_end, db, body.get("year"), _user(request))


@router.get("/leave-calendar")
def leave_calendar(month: Optional[str] = None, db: Session = Depends(get_db)):
    return svc.leave_calendar(db, month)


# ---------------------------------------------------------------- advances
@router.get("/advances")
def advances(status: Optional[str] = None, employee_id: Optional[int] = None, db: Session = Depends(get_db)):
    return svc.list_advances(db, status, employee_id)


@router.post("/advances")
def give_advance(body: Dict, request: Request, db: Session = Depends(get_db)):
    a = _do(db, svc.give_advance, db, body, _user(request))
    return svc.advance_out(db, a)


@router.post("/advances/{aid}/repay")
def repay(aid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.repay_advance, db, aid, body, _user(request))


# ---------------------------------------------------------------- incentives
@router.get("/incentive-schemes")
def schemes(db: Session = Depends(get_db)):
    return [svc.scheme_out(s) for s in db.query(models.HrIncentiveScheme).order_by(models.HrIncentiveScheme.id).all()]


@router.post("/incentive-schemes")
def add_scheme(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_scheme, db, body)


@router.put("/incentive-schemes/{sid}")
def edit_scheme(sid: int, body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_scheme, db, body, sid)


@router.delete("/incentive-schemes/{sid}")
def del_scheme(sid: int, db: Session = Depends(get_db)):
    s = db.get(models.HrIncentiveScheme, sid)
    if s is None:
        raise HTTPException(404, "scheme not found")
    db.delete(s)
    db.commit()
    return {"deleted": True}


@router.get("/incentives")
def incentives(date_from: str, date_to: str, db: Session = Depends(get_db)):
    try:
        return svc.incentives(db, date_from, date_to)
    except ValueError as e:
        _fail(e)


# ---------------------------------------------------------------- payroll
@router.get("/payroll")
def runs(db: Session = Depends(get_db)):
    return svc.list_runs(db)


@router.get("/payroll/{month}")
def run(month: str, db: Session = Depends(get_db)):
    return svc.get_run(db, month) or {"month": month, "status": None, "slips": []}


@router.post("/payroll/{month}/compute")
def compute(month: str, request: Request, db: Session = Depends(get_db)):
    r = _do(db, svc.compute_run, db, month, _user(request))
    return svc.run_out(db, r)


@router.post("/payroll/{month}/approve")
def approve(month: str, request: Request, db: Session = Depends(get_db)):
    r = _do(db, svc.approve_run, db, month, _user(request))
    return svc.run_out(db, r)


@router.post("/payroll/{month}/reopen")
def reopen(month: str, request: Request, db: Session = Depends(get_db)):
    r = _do(db, svc.reopen_run, db, month, _user(request))
    return svc.run_out(db, r)


@router.post("/payroll/{month}/pay")
def pay(month: str, body: Dict, request: Request, db: Session = Depends(get_db)):
    r = _do(db, svc.pay_run, db, month, body, _user(request))
    return svc.run_out(db, r)


@router.get("/payroll/{month}/bank-file")
def bank_file(month: str, db: Session = Depends(get_db)):
    try:
        text = svc.bank_file(db, month)
    except LookupError as e:
        _fail(e)
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="salary-{month}.csv"'})


@router.put("/payslips/{sid}")
def adjust_slip(sid: int, body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.adjust_slip, db, sid, body, _user(request))


@router.get("/payslips/{sid}")
def payslip(sid: int, db: Session = Depends(get_db)):
    try:
        return svc.payslip(db, sid)
    except LookupError as e:
        _fail(e)


# ---------------------------------------------------------------- performance
@router.get("/cycles")
def cycles(db: Session = Depends(get_db)):
    return svc.list_cycles(db)


@router.post("/cycles")
def add_cycle(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_cycle, db, body)


@router.put("/cycles/{cid}")
def edit_cycle(cid: int, body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_cycle, db, body, cid)


@router.get("/cycles/{cid}/review")
def review(cid: int, employee_id: Optional[int] = None, db: Session = Depends(get_db)):
    out = _do(db, svc.review, db, cid, employee_id)
    return out


@router.post("/goals")
def add_goal(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_goal, db, body)


@router.put("/goals/{gid}")
def edit_goal(gid: int, body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.save_goal, db, body, gid)


@router.delete("/goals/{gid}")
def del_goal(gid: int, db: Session = Depends(get_db)):
    g = db.get(models.HrGoal, gid)
    if g is None:
        raise HTTPException(404, "goal not found")
    db.delete(g)
    db.commit()
    return {"deleted": True}


@router.post("/goals/copy")
def copy_goals(body: Dict, db: Session = Depends(get_db)):
    return _do(db, svc.copy_goals, db, int(body.get("cycle_id") or 0), int(body.get("from_employee_id") or 0),
               body.get("to_employee_ids") or [])


@router.post("/appraisals")
def save_appraisal(body: Dict, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.save_appraisal, db, body, _user(request))


@router.post("/appraisals/{aid}/apply")
def apply_appraisal(aid: int, request: Request, db: Session = Depends(get_db)):
    return _do(db, svc.apply_appraisal, db, aid, _user(request))


# ---------------------------------------------------------------- reports
@router.get("/reports")
def report_list():
    return [{"key": k, "label": lbl, "group": g, "params": p} for k, lbl, g, p in svc.REPORTS]


@router.get("/reports/{key}")
def report(key: str, request: Request, db: Session = Depends(get_db)):
    try:
        return svc.report(db, key, dict(request.query_params))
    except (ValueError, LookupError) as e:
        _fail(e)
