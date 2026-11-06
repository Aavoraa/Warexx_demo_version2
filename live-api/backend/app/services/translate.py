"""
Reading a page that isn't written in English.

Essa's LR books are Tamil more often than not — a printed letterhead, and then a
clerk filling the columns by hand down the page. The extractor read those pages
perfectly well and handed back Tamil, which meant every downstream screen, search
box and report was matching English against Tamil and finding nothing: a supplier
typed as "ஏ.கே.ஆர்" never matches the "AKR EXPRESS" already in the master, and a
transporter column nobody can read is a column nobody uses.

Two passes, because one is not enough on its own:

  1. **In the vision call.** The model is reading handwriting off a photograph;
     asking it to translate as it reads costs nothing extra and is far better
     than translating a transcription, because it still has the page in front of
     it. Handwritten Tamil especially — the shape of a character and the sense of
     the word are decided together, and a two-step pipeline throws the image away
     before the second step.
  2. **A sweep over the result.** A model told to translate still hands back the
     occasional Tamil string — a cell it treated as a proper noun, a word it was
     unsure of. So every string that comes out is checked, and the survivors go
     through one text-only translation call. Without this the feature would work
     "usually", which for a register page means someone still has to read every
     row to find out.

WHAT IS NOT TOUCHED is the whole point
--------------------------------------
Only strings containing letters from a non-Latin script are considered at all.
A quantity, an amount, a date, an LR number, an invoice number, a GSTIN and an
HSN code contain no Tamil letters, so they are never sent to a translator and
never rewritten — they come out of this module byte-for-byte as they went in.
The fields themselves are likewise fixed: translation replaces the TEXT inside a
value, never a key, never the shape of the row. So "1 dozen at ₹600" stays 1 and
600 whatever language the page was written in.

And the Tamil is not thrown away. Every value that changes is recorded in
`originals`, keyed by its path, so the register can always show what the page
actually said — which matters the first time someone disputes a reading.
"""
import json
from .. import runtime

#: Unicode block -> the language we report having found. Restricted to real
#: scripts on purpose: "anything non-ASCII" would sweep up ₹, curly quotes and
#: em-dashes and send perfectly good English off to a translator.
SCRIPT_BLOCKS = [
    ((0x0B80, 0x0BFF), "Tamil"),
    ((0x0900, 0x097F), "Hindi"),
    ((0x0980, 0x09FF), "Bengali"),
    ((0x0A00, 0x0A7F), "Punjabi"),
    ((0x0A80, 0x0AFF), "Gujarati"),
    ((0x0C00, 0x0C7F), "Telugu"),
    ((0x0C80, 0x0CFF), "Kannada"),
    ((0x0D00, 0x0D7F), "Malayalam"),
    ((0x0600, 0x06FF), "Arabic"),
]

TAMIL = "Tamil"

#: How many strings go in one translation request. Register pages run to ~16
#: columns × 30 rows; a batch this size keeps a full page to a couple of calls
#: while staying well inside a single reply.
BATCH = 60


def script_of(ch):
    """The language name for one character, or None if it is Latin/punctuation."""
    o = ord(ch)
    for (lo, hi), name in SCRIPT_BLOCKS:
        if lo <= o <= hi:
            return name
    return None


def scripts_in(text):
    """Which non-Latin scripts appear in a string ({} for plain English)."""
    found = {}
    for ch in str(text or ""):
        name = script_of(ch)
        if name:
            found[name] = found.get(name, 0) + 1
    return found


def has_tamil(text):
    return TAMIL in scripts_in(text)


def needs_translation(value):
    """True for a string carrying letters this system cannot read.

    Deliberately narrow: a value has to contain actual non-Latin letters. That is
    what keeps every number, code and date out of the translator — see the module
    docstring, this is the guarantee the feature rests on."""
    return isinstance(value, str) and bool(scripts_in(value))


# ---------------------------------------------------------------------------
#  Walking a nested extraction result
# ---------------------------------------------------------------------------
#: The key every preserved original is filed under. Never walked into — it holds
#: the page's own words on purpose, and translating them would destroy the one
#: copy of what was actually written.
ORIGINALS_KEY = "original_values"


def _walk(obj, path=""):
    """Yield (path, string) for every string anywhere in a dict/list tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == ORIGINALS_KEY:
                continue
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _steps(path):
    """'line_items[2].description' -> ['line_items', 2, 'description']"""
    out = []
    for part in path.split("."):
        while "[" in part:
            head, _, rest = part.partition("[")
            idx, _, part = rest.partition("]")
            if head:
                out.append(head)
            out.append(int(idx))
        if part:
            out.append(part)
    return out


def _assign(obj, path, value):
    node = obj
    steps = _steps(path)
    for s in steps[:-1]:
        node = node[s]
    node[steps[-1]] = value


# ---------------------------------------------------------------------------
#  The translation call
# ---------------------------------------------------------------------------
TRANSLATE_SYSTEM = """You translate individual cell values read off Indian
garment-trade documents (LR / transport registers and supplier invoices) into
English. The text is usually Tamil, often handwritten, and is a name, a place, a
product or a short note — not prose.

Rules:
- Company, transporter, agent and person names: give the standard ENGLISH form if
  the business is normally written in English (ஏ.கே.ஆர் எக்ஸ்பிரஸ் -> "AKR EXPRESS"),
  otherwise a clean romanisation.
- Place names: the usual English spelling (கோயம்புத்தூர் -> "Coimbatore").
- Product and material words: translate them (துண்டு -> "Towel",
  தலையணை உறை -> "Pillow Cover", கைக்குட்டை -> "Handkerchief").
- Keep any digits, codes and punctuation that are already in the value exactly as
  they are. Never add, drop or reorder a number.
- If a value is already English, return it unchanged.
- Return ONLY a JSON object mapping each input index (as a string) to its English
  text: {"0": "...", "1": "..."}. No prose, no code fence."""


def _client():
    if not runtime.get("anthropic_api_key"):
        return None, None
    try:
        import anthropic
    except Exception:
        return None, None
    return (anthropic.Anthropic(api_key=runtime.get("anthropic_api_key")),
            runtime.get("vision_model") or "claude-3-5-sonnet-20241022")


def translate_texts(texts):
    """{original: english} for the strings that could be translated.

    Missing keys mean "left alone" — an unreachable API or an unparseable reply
    must leave the Tamil in place rather than blank the cell. A register row with
    one field still in Tamil is readable; a register row with an empty
    transporter is a lost fact."""
    todo = [t for t in dict.fromkeys(texts) if needs_translation(t)]
    if not todo:
        return {}
    client, model = _client()
    if client is None:
        return {}
    out = {}
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        payload = json.dumps({str(n): t for n, t in enumerate(chunk)},
                             ensure_ascii=False, indent=0)
        try:
            msg = client.messages.create(
                model=model, max_tokens=4000, system=TRANSLATE_SYSTEM,
                messages=[{"role": "user", "content":
                           f"Translate these {len(chunk)} values:\n{payload}"}])
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text").strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except Exception:
            continue                      # this chunk stays in its own language
        for n, src in enumerate(chunk):
            got = data.get(str(n))
            if isinstance(got, str) and got.strip() and got.strip() != src:
                out[src] = got.strip()
    return out


# ---------------------------------------------------------------------------
#  The public entry points
# ---------------------------------------------------------------------------
def sweep(obj):
    """Translate every non-Latin string inside `obj`, in place.

    Returns {"languages": ["Tamil"], "originals": {path: original},
             "translated": n, "note": str}. `obj` is mutated; the caller keeps the
    originals beside it so nothing the page said is lost."""
    found = [(path, val) for path, val in _walk(obj) if needs_translation(val)]
    langs = {}
    for _, val in found:
        for name, n in scripts_in(val).items():
            langs[name] = langs.get(name, 0) + n
    if not found:
        return {"languages": [], "originals": {}, "translated": 0, "note": ""}

    mapping = translate_texts([v for _, v in found])
    originals = {}
    for path, val in found:
        english = mapping.get(val)
        if english:
            _assign(obj, path, english)
            originals[path] = val
    order = sorted(langs, key=lambda k: -langs[k])
    if not mapping:
        note = (f"{len(found)} value(s) are in {', '.join(order)} and could not be "
                f"translated — set the vision key in Settings to read them in English")
    elif len(originals) < len(found):
        note = (f"translated {len(originals)} {order[0]} value(s); "
                f"{len(found) - len(originals)} could not be translated")
    else:
        note = f"translated {len(originals)} value(s) from {', '.join(order)}"
    return {"languages": order, "originals": originals,
            "translated": len(originals), "note": note}


def language_of(obj):
    """The non-Latin scripts still present in a structure, most-used first."""
    langs = {}
    for _, val in _walk(obj):
        for name, n in scripts_in(val).items():
            langs[name] = langs.get(name, 0) + n
    return sorted(langs, key=lambda k: -langs[k])


# ---------------------------------------------------------------------------
#  What the vision prompts say
# ---------------------------------------------------------------------------
#: Appended to every prompt that reads a page. Pass 1 of the two described at the
#: top: the model has the image, so it translates while it can still see the
#: handwriting rather than working from a transcription of it.
VISION_LANGUAGE_RULES = """
LANGUAGE
This page may be written in Tamil — printed, handwritten, or a mix of Tamil and
English. Read it in whatever language it is written and RETURN EVERY TEXT VALUE
IN ENGLISH:
- Names of suppliers, transporters, agents and people: the standard English form
  the business normally uses, otherwise a clean romanisation.
- Places: the usual English spelling.
- Product, material and note text: translated into English.
Translate the WORDS ONLY. Numbers, amounts, quantities, dates, invoice numbers,
LR/docket numbers, GSTIN, HSN and any other code must come through EXACTLY as
printed or written — never re-spell, re-order, convert or "correct" a digit, and
never move a value into a different field because of the language it was in.

PRESERVE THE ORIGINAL. Whenever you translate a value, add an "original_values"
key to the object that value belongs to, mapping the field name to the text
exactly as it appears on the page:
  {"transport": "AKR EXPRESS", "original_values": {"transport": "ஏ.கே.ஆர் எக்ஸ்பிரஸ்"}}
Objects where you translated nothing get no "original_values" key.

Also return a top-level "source_language" key: "Tamil" if any Tamil appears on
the page, "English" if none does, or the language you actually found.
"""
