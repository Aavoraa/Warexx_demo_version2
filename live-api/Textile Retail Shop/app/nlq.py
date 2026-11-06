"""Ask the shop a question — "what did we sell last month", "நிலுவை எவ்வளவு".

ROUTING, NOT TEXT-TO-SQL
------------------------
The model's job here is narrow and checkable: pick one of the reports in
reports_lib and say what dates it wants. It never writes a query. That matters
for the same reason it matters upstairs in the warehouse — each report already
knows what it counts, and says in its `note` where the honest figure differs from
the obvious one (that returns come off the seller, that stock value is at cost,
that walk-ins have no customer to name). A generated query knows the columns and
none of that: it would return a number that looks right, disagrees with the same
figure on the Reports screen, and leaves nobody able to say which is wrong.

`report_key` is an enum in the output schema, built from REPORTS itself, so an
unknown report is not something the model can return and the two cannot drift
apart. Dates come back as ISO and are re-parsed here rather than trusted.

The reading — which report, which dates — is always shown back to the user in one
line, so a misroute is caught before the figures are believed.

WITHOUT AN API KEY
------------------
It still works, by scoring the question against each report's keywords and
resolving relative dates itself. That is visibly worse — it is matching words,
not reading a sentence — so it says so in `engine` rather than passing itself off
as the same thing.
"""
import json
import os
import re
import datetime as dt
from pathlib import Path

from app import reports_lib

SHOP_DIR = Path(__file__).resolve().parents[1]
WAREHOUSE_SETTINGS = SHOP_DIR.parent / "backend" / "data" / "settings.json"

NLQ_MODEL = os.environ.get("ESSA_NLQ_MODEL", "claude-opus-5")

SYSTEM = """You route a shop owner's question to ONE report from this shop's catalogue.

Today is {today}.

The catalogue:
{catalogue}

Rules:
- Pick the report that answers the question most directly.
- Questions may be in English or Tamil. Answer the same either way.
- `date_from`/`date_to` are ISO dates. Work out relative periods ("last month",
  "this week", "yesterday", "கடந்த மாதம்") against today's date.
- Reports marked (undated) show the position right now; give them today's date
  for both ends.
- If no report really answers it, still pick the closest and set confidence low.
- `reading` is one short sentence, in the question's own language, saying what
  you understood — this is shown to the user so they can catch a misread."""


def api_key():
    """The key, from the environment or the warehouse's settings file.

    Read rather than duplicated: the shop is served from inside that backend and
    the person who typed the key in once should not have to do it again here.
    """
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        with open(WAREHOUSE_SETTINGS, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("anthropic_api_key") or ""
    except (OSError, ValueError):
        return ""


def available():
    if not api_key():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _catalogue_lines():
    return "\n".join(
        f"- {k}: {v['label']} — {v['blurb']}" + ("" if v["dated"] else "  (undated)")
        for k, v in reports_lib.REPORTS.items())


def _schema():
    return {
        "type": "object",
        "properties": {
            "report_key": {"type": "string", "enum": list(reports_lib.REPORTS.keys())},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "reading": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["report_key", "date_from", "date_to", "reading", "confidence"],
        "additionalProperties": False,
    }


def _ask_model(question):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key())
    system = SYSTEM.format(catalogue=_catalogue_lines(), today=dt.date.today().isoformat())
    msg = client.messages.create(
        model=NLQ_MODEL,
        max_tokens=2048,
        # The catalogue is the same on every question and is most of the prompt,
        # so it is cached; the question sits after the breakpoint.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
        # extra_body rather than a typed argument: `output_config` is newer than
        # the pinned SDK, but the API honours it — versioned by the
        # anthropic-version header, not the client. Low effort because routing one
        # sentence into a fixed list does not reward deliberation, and somebody is
        # waiting on a table.
        extra_body={"output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": _schema()},
        }},
    )
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined to answer this")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    data = json.loads(text)
    data["engine"] = "model"
    return data


# ---- the offline fallback -------------------------------------------------

_PERIODS = [
    (r"\btoday\b|இன்று", lambda t: (t, t)),
    (r"\byesterday\b|நேற்று", lambda t: (t - dt.timedelta(days=1),) * 2),
    (r"\bthis week\b|இந்த வாரம்", lambda t: (t - dt.timedelta(days=t.weekday()), t)),
    (r"\blast week\b|கடந்த வாரம்",
     lambda t: (t - dt.timedelta(days=t.weekday() + 7), t - dt.timedelta(days=t.weekday() + 1))),
    (r"\bthis month\b|இந்த மாதம்", lambda t: (t.replace(day=1), t)),
    (r"\blast month\b|கடந்த மாதம்",
     lambda t: ((t.replace(day=1) - dt.timedelta(days=1)).replace(day=1),
                t.replace(day=1) - dt.timedelta(days=1))),
    (r"\bthis year\b|இந்த ஆண்டு", lambda t: (t.replace(month=1, day=1), t)),
]


def _offline_dates(question, today):
    for pattern, span in _PERIODS:
        if re.search(pattern, question, re.IGNORECASE):
            a, b = span(today)
            return a, b
    days = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+days?\b", question, re.IGNORECASE)
    if days:
        return today - dt.timedelta(days=int(days.group(1))), today
    return today - dt.timedelta(days=30), today


def _offline(question):
    q = question.lower()
    # Score every report and keep the best. The comparison has to be against the
    # running best's own score, not against a constant — an earlier version
    # compared the weight to 0, which made every later report with an equal hit
    # count overwrite the winner, and "which items are running low" came back as
    # sales-by-item because "item" matched.
    best_key, best = "sales_summary", (0, 0)
    for key, spec in reports_lib.REPORTS.items():
        matched = [kw for kw in spec["keywords"] if kw.lower() in q]
        # More keywords beats fewer; for a tie, longer phrases are the stronger
        # evidence — "low stock" says more than "stock".
        score = (len(matched), sum(len(kw) for kw in matched))
        if score > best:
            best_key, best = key, score
    best, score = best_key, best[0]
    today = dt.date.today()
    a, b = _offline_dates(question, today)
    return {"report_key": best, "date_from": a.isoformat(), "date_to": b.isoformat(),
            "reading": f"Matched words to “{reports_lib.REPORTS[best]['label']}”.",
            "confidence": "low" if score == 0 else "medium", "engine": "keywords"}


def _as_date(text, fallback):
    try:
        return dt.date.fromisoformat((text or "").strip()[:10])
    except (ValueError, TypeError):
        return fallback


def ask(question):
    """Answer a question with one report. Never raises — the shop keeps working."""
    question = (question or "").strip()
    if not question:
        return {"error": "Ask something first."}

    try:
        routed = _ask_model(question) if available() else _offline(question)
    except Exception as exc:                        # noqa: BLE001 — reported, not raised
        routed = _offline(question)
        routed["warning"] = f"The model could not be reached ({type(exc).__name__}); matched on words instead."

    today = dt.date.today()
    start = _as_date(routed.get("date_from"), today - dt.timedelta(days=30))
    end = _as_date(routed.get("date_to"), today)
    if start > end:
        start, end = end, start

    key = routed.get("report_key")
    if key not in reports_lib.REPORTS:
        key = "sales_summary"

    result = reports_lib.run(key, start, end)
    result.update({
        "question": question,
        "engine": routed.get("engine"),
        "reading": routed.get("reading") or "",
        "confidence": routed.get("confidence") or "low",
        "from": start.isoformat(), "to": end.isoformat(),
    })
    if routed.get("warning"):
        result["warning"] = routed["warning"]
    return result


EXAMPLES = [
    "what did we sell last month",
    "who sold the most this week",
    "gst for this month",
    "which items are running low",
    "returns last 7 days",
    "what did the offers give away this month",
    "கடந்த மாதம் விற்பனை எவ்வளவு",
    "இருப்பு குறைவான பொருட்கள்",
]
