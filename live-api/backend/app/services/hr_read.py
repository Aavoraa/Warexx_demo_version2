"""Reading an employee's papers into the employee form.

An Aadhaar card, a PAN card, a licence, a passport, a cancelled cheque, a resume,
an offer letter: each carries part of what the joining form asks for. This reads
one upload and returns the form fields it found — it writes nothing. The screen
shows them beside what the form already holds and the person ticks what to take,
so a misread is a box left unticked rather than a wrong date of birth on file.

Two readers, in the order they are tried:

  * the vision model (the key and model set in Settings, as for invoices and LR
    pages) reads names, dates and addresses off a phone photo or a PDF;
  * offline, with no key, the text of a PDF — or of a photo, when Tesseract is
    installed — is searched for the numbers that have fixed shapes in India: PAN,
    Aadhaar, IFSC, email, mobile. Names and addresses need the model.

Every value is normalised to what the form stores before it leaves here, and
anything that does not survive normalisation (a 'PAN' that is not ten
characters in the PAN pattern) is dropped rather than suggested.
"""
import base64
import io
import json
import os
import re
import tempfile

from .. import models as M, runtime
from . import translate
from .hr import DOC_TYPES, GENDERS, _date

MAX_BYTES = 10 * 1024 * 1024
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".pdf": "application/pdf"}

#: what can be read, in the form's own field names
FIELDS = ["name", "father_name", "gender", "dob", "blood_group", "phone", "email", "address",
          "permanent_address", "aadhaar", "pan", "uan", "bank_name", "bank_account", "ifsc",
          "designation", "department", "joined_on", "location"]

#: the papers whose address is the one the person is registered at
ID_PROOFS = {"Aadhaar", "Voter ID", "Passport", "Driving licence", "Address proof"}

PROMPT = """You are reading ONE document from an Indian employee's joining file — an
ID card, a bank proof, a resume, a certificate or an employment letter, often a
phone photo. Return ONE JSON object, nothing else:

{
  "document_type": one of %(types)s,
  "document_number": the card/letter's own number (Aadhaar, PAN, passport, DL, EPIC, certificate or letter no.) or null,
  "issued_on": "YYYY-MM-DD" or null,
  "expires_on": "YYYY-MM-DD" or null,
  "title": a short note naming the document, e.g. "B.Com, Madras University, 2019" or "Trends Retail, Mar 2024", or null,
  "person": {
    "name": the employee's full name as printed,
    "father_name": father's or husband's name (S/O, D/O, W/O, C/O lines) or null,
    "gender": "Female" | "Male" | "Other" | null,
    "dob": "YYYY-MM-DD" or null (only a full date; a year of birth alone is null),
    "blood_group": e.g. "B+" or null,
    "phone": 10-digit mobile or null,
    "email": or null,
    "address": the full address printed on the document, one line, or null,
    "aadhaar": 12 digits or null (never a Virtual ID),
    "pan": or null,
    "uan": the EPF UAN or null,
    "bank_name": or null, "bank_account": or null, "ifsc": or null,
    "designation": the job title an offer/appointment letter gives this person, or null,
    "department": or null,
    "joined_on": "YYYY-MM-DD" the joining date an offer/appointment letter gives, or null,
    "location": the branch/store they are posted to, or null
  }
}

Rules: read only what is printed; use null for anything absent or unreadable —
never guess a digit. Dates on Indian documents are day-first (05/03/1990 is 5 March).
On a relieving letter, experience letter, payslip or Form 16 from a PREVIOUS
employer, the designation, department, joining date and location are that old
job's: leave those four null, but do read the UAN and PAN. On a bank proof the
name is the account holder's. Return ONLY the JSON object.
""" % {"types": json.dumps(DOC_TYPES)} + translate.VISION_LANGUAGE_RULES


# ---------------------------------------------------------------- normalising
PAN_RE = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")
AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[ -]?)?([6-9]\d{4})[ -]?(\d{5})(?!\d)")
DOB_RE = re.compile(r"(?:DOB|D\.O\.B|Date of Birth|Birth)\W{0,3}(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})", re.I)
UAN_RE = re.compile(r"UAN\W{0,6}(\d{12})", re.I)
BLOOD = {"a+": "A+", "a-": "A-", "b+": "B+", "b-": "B-", "ab+": "AB+", "ab-": "AB-", "o+": "O+", "o-": "O-"}


def _clean_text(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip(" ,.;:")
    return s or None


def _aadhaar(v):
    d = re.sub(r"\D", "", str(v or ""))
    # a real Aadhaar never starts with 0 or 1
    return f"{d[:4]} {d[4:8]} {d[8:]}" if len(d) == 12 and d[0] not in "01" else None


def _blood(v):
    s = str(v or "").lower().replace(" ", "").replace("ve", "").replace("positive", "+").replace("negative", "-")
    s = s.replace("(", "").replace(")", "")
    return BLOOD.get(s)


def _phone(v):
    d = re.sub(r"\D", "", str(v or ""))
    d = d[-10:] if len(d) >= 10 else d
    return d if len(d) == 10 and d[0] in "6789" else None


def _iso(v):
    if not v:
        return None
    d = _date(v)
    return d.isoformat() if d else None


def normalise(person):
    """The model's (or the regexes') reading, as the form stores it."""
    p = person or {}
    out = {
        "name": _clean_text(p.get("name")),
        "father_name": _clean_text(p.get("father_name")),
        "gender": next((g for g in GENDERS if g.lower() == str(p.get("gender") or "").strip().lower()), None),
        "dob": _iso(p.get("dob")),
        "blood_group": _blood(p.get("blood_group")),
        "phone": _phone(p.get("phone")),
        "email": (lambda m: m.group(0).lower() if m else None)(EMAIL_RE.search(str(p.get("email") or ""))),
        "address": _clean_text(p.get("address")),
        "aadhaar": _aadhaar(p.get("aadhaar")),
        "pan": (lambda m: m.group(1) if m else None)(PAN_RE.search(str(p.get("pan") or "").upper().replace(" ", ""))),
        "uan": (lambda d: d if len(d) == 12 else None)(re.sub(r"\D", "", str(p.get("uan") or ""))),
        "bank_name": _clean_text(p.get("bank_name")),
        "bank_account": (lambda d: d if 6 <= len(d) <= 18 else None)(re.sub(r"\D", "", str(p.get("bank_account") or ""))),
        "ifsc": (lambda m: m.group(1) if m else None)(IFSC_RE.search(str(p.get("ifsc") or "").upper().replace(" ", ""))),
        "designation": _clean_text(p.get("designation")),
        "department": _clean_text(p.get("department")),
        "joined_on": _iso(p.get("joined_on")),
        "location": _clean_text(p.get("location")),
    }
    if out["name"] and out["name"].isupper():
        out["name"] = out["name"].title()
    if out["father_name"] and out["father_name"].isupper():
        out["father_name"] = out["father_name"].title()
    return {k: v for k, v in out.items() if v}


def _guess_type(text):
    t = text.lower()
    for words, kind in ((("income tax department", "permanent account number"), "PAN"),
                        (("aadhaar", "unique identification", "enrolment no"), "Aadhaar"),
                        (("election commission", "electors photo"), "Voter ID"),
                        (("driving licence", "driving license"), "Driving licence"),
                        (("republic of india", "passport no"), "Passport"),
                        (("ifsc",), "Bank proof"),
                        (("relieving",), "Relieving letter"), (("experience certificate", "to whom it may concern"), "Experience letter"),
                        (("offer letter", "pleased to offer"), "Offer letter"), (("appointment",), "Appointment letter"),
                        (("form 16", "form no. 16"), "Previous employer Form 16"),
                        (("curriculum vitae", "resume", "career objective"), "Resume")):
        if any(w in t for w in words):
            return kind
    return "Other"


def read_offline(text):
    """What the fixed-shape numbers in a page's text give, with no model."""
    up = text.upper()
    kind = _guess_type(text)
    person = {}
    m = PAN_RE.search(up)
    if m:
        person["pan"] = m.group(1)
    # twelve digits are an Aadhaar only on an Aadhaar — on a cheque they are the account
    m = AADHAAR_RE.search(text) if kind == "Aadhaar" else None
    if m:
        person["aadhaar"] = "".join(m.groups())
    m = IFSC_RE.search(up)
    if m:
        person["ifsc"] = m.group(1)
    m = EMAIL_RE.search(text)
    if m:
        person["email"] = m.group(0)
    m = PHONE_RE.search(text)
    if m:
        person["phone"] = "".join(m.groups())
    m = DOB_RE.search(text)
    if m:
        person["dob"] = m.group(1)
    m = UAN_RE.search(text)
    if m:
        person["uan"] = m.group(1)
    number = {"PAN": person.get("pan"), "Aadhaar": person.get("aadhaar")}.get(kind)
    return {"document_type": kind, "document_number": number, "person": person}


# ---------------------------------------------------------------- the readers
def _vision_ready():
    if not runtime.get("anthropic_api_key"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        return None


def read_vision(raw, media):
    import anthropic
    client = anthropic.Anthropic(api_key=runtime.get("anthropic_api_key"))
    b64 = base64.standard_b64encode(raw).decode()
    block = ({"type": "document", "source": {"type": "base64", "media_type": media, "data": b64}}
             if media == "application/pdf" else
             {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    # streamed with room to spare, as the invoice reader is: the current models
    # think before they answer and max_tokens covers both
    with client.messages.stream(
        model=runtime.get("vision_model") or "claude-opus-5-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise ValueError("The model declined to read this document")
    data = _parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
    if not data:
        raise ValueError("The document could not be read — try a clearer photo")
    return data


def _pdf_text(raw):
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages[:5])
    except Exception:
        return ""


def _photo_text(raw, ext):
    from ..extraction.tesseract_ocr import TesseractProvider
    ocr = TesseractProvider()
    if not ocr.available():
        return None
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        return ocr._ocr(path)
    finally:
        os.unlink(path)


def _match(db, model, name):
    if not name:
        return None
    low = name.strip().lower()
    return next((x for x in db.query(model).filter(model.active.isnot(False)).all() if x.name.strip().lower() == low), None)


def read_document(db, raw, filename):
    """One upload → {doc_type, number, issued_on, expires_on, title, fields, provider, note}."""
    ext = os.path.splitext(filename or "")[1].lower()
    media = MEDIA.get(ext)
    if not media:
        raise ValueError("Upload a PDF or a photo (JPG, PNG or WebP)")
    if not raw:
        raise ValueError("The file is empty")
    if len(raw) > MAX_BYTES:
        raise ValueError("The file is over 10 MB")
    if _vision_ready():
        data, provider = read_vision(raw, media), "vision"
        note = ""
    else:
        text = _pdf_text(raw) if media == "application/pdf" else _photo_text(raw, ext)
        if text is None:
            return {"doc_type": None, "fields": {}, "provider": "none",
                    "note": "Reading photos needs the Claude key — set it in Settings. "
                            "A PDF with text in it can be read without one."}
        data, provider = read_offline(text), "offline"
        note = ("Read without the Claude key: only numbers with a fixed pattern (PAN, Aadhaar, IFSC, "
                "mobile, email) are picked up. Set the key in Settings to read names, dates and addresses.")
        if not text.strip():
            note = "This PDF has no text in it (it is a scan). " + note
    kind = data.get("document_type") if data.get("document_type") in DOC_TYPES else "Other"
    fields = normalise(data.get("person"))
    # an ID card's address is where the person is registered; a resume's is where they live
    if "address" in fields and kind in ID_PROOFS:
        fields["permanent_address"] = fields.pop("address")
    out = {"doc_type": kind, "number": _clean_text(data.get("document_number")),
           "issued_on": _iso(data.get("issued_on")), "expires_on": _iso(data.get("expires_on")),
           "title": _clean_text(data.get("title")), "fields": fields, "provider": provider, "note": note}
    dep = _match(db, M.HrDepartment, fields.get("department"))
    desig = _match(db, M.HrDesignation, fields.get("designation"))
    out["department_id"] = dep.id if dep else None
    out["designation_id"] = desig.id if desig else None
    if desig and desig.department_id and not dep:
        out["department_id"] = desig.department_id
    if kind == "Aadhaar" and not out["number"]:
        out["number"] = fields.get("aadhaar")
    if kind == "PAN" and not out["number"]:
        out["number"] = fields.get("pan")
    return out
