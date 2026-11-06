"""Bill numbers: which series a till bills on, and the next number in it.

    Ground Floor POS → TG → 26 → 001 → TG26-001

A bill number is not decoration. It is the shop's statutory record of a sale, it
is what a customer quotes, and it is what an auditor counts. Three things follow
from that, and they are why this is a module rather than an f-string:

**No duplicates, ever, however many tills are billing.** The number comes from a
row in `bill_sequences`, handed out by an atomic `UPDATE … SET last_number =
last_number + 1`. Not by reading the last number and adding one — two tills that
read "7" at the same moment both write TG26-008, and one of them loses its sale
to a unique-constraint error at the worst possible moment. The UPDATE takes a row
lock, so the second till waits for the first to finish and then reads 8. That is
the whole trick, and it is the only part of this file that has to be exactly
right.

**No gaps either.** The number is allocated inside the same transaction as the
sale, so a bill that fails half-way gives its number back. A series numbered
1, 2, 4 invites the question of what happened to 3, and the honest answer has to
be "nothing did" — not "the till crashed once".

**The mapping is data.** A floor is a row with a prefix on it. Adding a fifth
floor, or a second store with its own four, is somebody typing into the Floors
screen — nothing here learns a new name.

THE SERIES IS KEYED ON (prefix, financial year), not on the floor. The bill
number *is* prefix + year + number, so anything sharing those two must share one
counter; keying on the floor would let two floors configured with the same prefix
both hand out TG26-001. Two floors sharing a prefix therefore share a register,
which is the only reading under which the numbers stay unique.

FINANCIAL YEAR. India's runs April → March, and `26` in TG26-001 is the year it
STARTED — so a bill rung on 10 September 2026 is TG26-…, and one rung on 10 March
2027 still is. The series restarts at 001 on 1 April 2027 as TG27-001, because
that is a new row in `bill_sequences` and nothing has to run to make it happen.
`FY_START_MONTH` moves the boundary for a business that closes its books
elsewhere.
"""
import re
from datetime import date, datetime

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import BillSequence, Invoice

#: What the shop numbers a bill when the till it was rung on is not mapped to a
#: floor. Deliberately the series this shop has always used, at the width it has
#: always used, so an unmapped till keeps billing exactly as it did rather than
#: refusing a customer over an incomplete master.
FALLBACK_PREFIX = "INV"
FALLBACK_PAD = 6

#: Floor bills are TG26-001 — three digits, and three is a MINIMUM rather than a
#: limit. The 1000th bill on a floor is TG26-1000, not TG26-000: a series that
#: wrapped would repeat a number, which is the one thing that must not happen.
FLOOR_PAD = 3


def fy_start_month():
    """The month a financial year opens on. April unless told otherwise."""
    try:
        month = int(current_app.config.get("FY_START_MONTH", 4))
    except (TypeError, ValueError, RuntimeError):
        return 4
    return month if 1 <= month <= 12 else 4


def financial_year(on=None, start_month=None):
    """The two-digit label for the year `on` falls in — "26" for FY 2026-27.

    The year it STARTED in, which is what the brief's own example says: a bill
    rung in September 2026 is in the year that opened in April 2026, and reads
    26 until the following March.
    """
    on = on or date.today()
    if isinstance(on, datetime):
        on = on.date()
    start = start_month or fy_start_month()
    year = on.year if on.month >= start else on.year - 1
    return f"{year % 100:02d}"


def format_number(prefix, fin_year, seq, pad=FLOOR_PAD):
    """`TG` + `26` + 1 → `TG26-001`. Also `INV` + `` + 1 → `INV-000001`.

    One function for both series, which is what keeps the shop's existing
    numbering intact while the floors get their own: the plain series is simply
    a series with no year in it and a wider number.
    """
    return f"{prefix}{fin_year}-{int(seq):0{pad}d}"


def series_for(storey, when=None):
    """(prefix, fin_year, pad) for a FLOOR — the series its next bill belongs to.

    Takes the floor, not the till, and that distinction is the whole of a bug
    this once had. The till's mapping is only one of the two ways a floor is
    settled: `places.resolve` uses the till's storey where it has one, and the
    floor the cashier picked where it has not. Deriving the series from the till
    here while the BILL recorded the resolved floor meant an unmapped counter
    printed "Ground Floor" at the top and a plain INV- number beside it — two
    answers to one question, on a document that is somebody's tax record.

    So both come from the same value now. A floor with no prefix, or one that has
    been switched off, falls back to the shop's plain series along with no floor
    at all; see FALLBACK_PREFIX.
    """
    if storey is not None and storey.active and (storey.prefix or "").strip():
        return storey.prefix.strip().upper(), financial_year(when), FLOOR_PAD
    return FALLBACK_PREFIX, "", FALLBACK_PAD


def _qualified(table):
    """`shop.bill_sequences` where there is a schema, plain where there is not.

    The raw statements below name the table themselves, so they need the same
    qualification the ORM applies — see app/__init__.SHOP_DB_SCHEMA. Without it,
    a shop mounted inside the warehouse on one Postgres would increment a
    sequence in `public` that belongs to nobody.
    """
    return f"{db.metadata.schema}.{table}" if db.metadata.schema else table


def _highest_existing(prefix, fin_year, pad, column=None):
    """The largest number already used on this series, or 0.

    Only ever read when the series row is first created, and it is what stops a
    brand-new counter re-issuing numbers a shop has already printed — on the
    plain INV- series that is every bill it has ever raised. Scanned rather than
    assumed: `max(id)` is not the same question, and has not been since the first
    invoice was deleted.

    `column` is which document's numbers to look through, defaulting to bills.
    """
    column = Invoice.invoice_number if column is None else column
    pattern = re.compile(rf"^{re.escape(prefix)}{re.escape(fin_year)}-(\d+)$")
    rows = db.session.query(column).filter(
        column.like(f"{prefix}{fin_year}-%")).all()
    best = 0
    for (number,) in rows:
        found = pattern.match((number or "").strip())
        if found:
            best = max(best, int(found.group(1)))
    return best


#: How many times to re-try opening a series that another till is opening at the
#: same moment. Only the FIRST bill of a floor's financial year can hit this —
#: after that the row exists and every till takes the fast path — so a couple of
#: attempts is generous rather than a loop hiding a problem.
_OPEN_ATTEMPTS = 4


def next_document_number(prefix, column, pad=FALLBACK_PAD):
    """The next number for a document that is not a bill — `AUD-000001`.

    The same series row and the same atomic increment the till uses, offered to
    anything else that numbers a document. `utils.generate_number` — which the
    credit note, the delivery and the alteration still use — takes the last
    row's id and adds one, and two people saving at the same moment therefore
    get the same number; on a document raised once a day that is rare rather
    than impossible, and rare is how it stays believed.

    `column` is the model column the existing numbers live in, so a series
    opened for the first time on a shop that already has documents starts above
    them rather than re-issuing what is already printed.
    """
    row = _series_row(prefix, "", pad, column=column)
    table = _qualified(BillSequence.__tablename__)
    db.session.execute(
        text(f"UPDATE {table} SET last_number = last_number + 1 WHERE id = :id"),
        {"id": row.id})
    seq = db.session.execute(
        text(f"SELECT last_number FROM {table} WHERE id = :id"),
        {"id": row.id}).scalar()
    db.session.expire(row)
    return format_number(prefix, "", seq, pad)


def _series_row(prefix, fin_year, pad, column=None):
    """The `bill_sequences` row for this series, created at the right start.

    The only genuinely contended moment in this file, and it happens once per
    floor per financial year: several tills ringing the first bill of the year
    together all find no row and all try to insert one. Two things can then go
    wrong, and both are somebody else winning the race rather than an error:

      IntegrityError    the other till's row landed first and the unique
                        constraint refused this one.
      OperationalError  the other till's write still had the database, and this
                        one could not get in. SQLite only; see
                        app/__init__._enable_concurrent_writes for why the wait
                        is usually enough on its own.

    Either way the answer is to look again — the row the winner wrote is the row
    this till wanted. Each attempt is wrapped in a SAVEPOINT so a failed insert
    undoes only itself; an ordinary rollback here would throw away the sale being
    billed around it.
    """
    for attempt in range(_OPEN_ATTEMPTS):
        row = BillSequence.query.filter_by(prefix=prefix, fin_year=fin_year).first()
        if row is not None:
            return row
        mark = db.session.begin_nested()
        try:
            row = BillSequence(prefix=prefix, fin_year=fin_year,
                               last_number=_highest_existing(prefix, fin_year,
                                                             pad, column))
            db.session.add(row)
            mark.commit()
            return row
        except (IntegrityError, OperationalError):
            mark.rollback()
            if attempt == _OPEN_ATTEMPTS - 1:
                raise
    # Unreachable: the loop either returns a row or re-raises on its last turn.
    raise RuntimeError(f"could not open the {prefix}{fin_year} bill series")


def allocate(storey, when=None):
    """Take the next number on this floor's series. THE call that hands one out.

    Returns (bill_number, prefix, fin_year, seq).

    `storey` is the floor the bill is being raised on, as `places.resolve`
    settled it — the same value the invoice records. Called from inside the
    transaction that is writing the bill, and it holds that series' row until
    the transaction ends: that is what serialises two tills on one floor, and
    what gives the number back if the sale fails. A till on another floor is on
    another row and never waits.
    """
    prefix, fin_year, pad = series_for(storey, when)
    row = _series_row(prefix, fin_year, pad)
    table = _qualified(BillSequence.__tablename__)

    # The increment and the read are two statements and have to be, because not
    # every database this runs on supports UPDATE … RETURNING. They are safe as a
    # pair: the UPDATE takes the row lock, so a second till blocks on it until
    # this transaction commits, and the SELECT then reads this transaction's own
    # value — never a number another till is in the middle of taking.
    db.session.execute(
        text(f"UPDATE {table} SET last_number = last_number + 1 WHERE id = :id"),
        {"id": row.id})
    seq = db.session.execute(
        text(f"SELECT last_number FROM {table} WHERE id = :id"),
        {"id": row.id}).scalar()
    # The ORM's copy of the row is a statement out of date now. Expired rather
    # than left alone, so anything reading it later gets the number that is
    # actually in the table.
    db.session.expire(row)
    return format_number(prefix, fin_year, seq, pad), prefix, fin_year, int(seq)


def peek(storey, when=None):
    """What the next bill on this floor would be, WITHOUT taking it.

    For the billing screen, which shows the number before the sale is committed.
    It is a look, not a reservation, and the difference matters: reserving the
    number for a cart that is then abandoned puts a hole in a statutory series,
    and "what happened to TG26-007" is a question with no good answer. So the
    till shows this as the next number and the committed bill carries the
    authority — on a quiet floor they are always the same, and on a busy one the
    cashier sees the real number on the bill a second later.

    Takes the same resolved floor `allocate` does, so what the screen promises
    and what the bill gets cannot come from different places.
    """
    prefix, fin_year, pad = series_for(storey, when)
    row = BillSequence.query.filter_by(prefix=prefix, fin_year=fin_year).first()
    seq = (row.last_number if row else _highest_existing(prefix, fin_year, pad)) + 1
    return {"number": format_number(prefix, fin_year, seq, pad),
            "prefix": prefix, "fin_year": fin_year, "seq": seq,
            "mapped": prefix != FALLBACK_PREFIX,
            "floor": storey.name if storey is not None else None}


#: The four storeys the brief names, with the prefixes it gives them. Offered by
#: the Floors screen as a starting point for a store that has none — typed in
#: once by a button rather than by hand — and used nowhere else. Nothing in the
#: billing path reads this: a shop with five floors, or one, is a shop with
#: different rows in the `floors` table and no different code.
STANDARD_FLOORS = (
    ("Ground Floor", "TG"),
    ("First Floor", "TF"),
    ("Second Floor", "TS"),
    ("Third Floor", "TT"),
)
