"""
Mutable runtime settings — the vision API key and model, the provider
preference, and the dead-stock discount ladder. All changed from the UI, all
expected to survive a restart.

Source of truth precedence: what somebody saved in the app wins; otherwise the
environment defaults in config.py.

Those saved values live in the `app_settings` table, not in a JSON file beside
the database as they used to. The file worked while there was always one server
with a disk, and stopped working the moment there might not be:

  * a read-only filesystem has nowhere to write it, and
  * module-level state is per-process, so on a deployment that runs several
    short-lived instances the key saved by whichever one served the settings
    screen is not there for the one that serves the next upload. Vision would
    look like it switched itself off at random, which is a hard fault to chase.

A row is one answer for every instance. An existing settings.json is imported
once, so a laptop that has been running for months keeps the key it already had.
"""
import json
import os

from . import config

_DEFAULTS = {
    "anthropic_api_key": config.ANTHROPIC_API_KEY,
    "vision_model": config.VISION_MODEL,
    "provider_preference": config.EXTRACTION_PROVIDER,   # auto | claude_vision | tesseract
    # The dead-stock discount ladder and the assumptions the cash impact is
    # worked out on — a commercial policy, not a constant, so it is set from the
    # screen rather than compiled into the code that applies it. Empty until
    # somebody changes it: services/dead_stock.py holds the defaults, and a
    # stored blank must not shadow a default that is later improved.
    "dead_stock_rules": {},
    # Whether a hand-keyed consignment must name a confirmed purchase order.
    # Here as well as in config so the buying policy can be changed from the
    # settings screen without a redeploy — the people who decide it are not the
    # people with shell access. See config.REQUIRE_PO_FOR_LR.
    "require_po_for_lr": config.REQUIRE_PO_FOR_LR,
}

LEGACY_SETTINGS_PATH = os.path.join(config.STATE_DIR, "settings.json")

# Read once per process and only to avoid a query per call on the extraction
# path. Writes go straight to the database and clear this, so the instance that
# made the change is immediately correct; another instance picks the change up
# when it next starts. That is the same staleness the JSON file had, without the
# filesystem.
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    values = dict(_DEFAULTS)
    try:
        from .database import SessionLocal
        from .models import AppSetting
        db = SessionLocal()
        try:
            rows = {r.key: r.value for r in db.query(AppSetting).all()}
            if not rows:
                rows = _import_legacy_file(db)
            for k in values:
                # `is not None`, not truthiness. A stored empty string is a
                # decision — it is what "turn vision off" writes — and testing
                # truthiness would read it as "nothing saved" and fall back to
                # ANTHROPIC_API_KEY from the environment, switching vision back
                # on behind the back of whoever just turned it off.
                if rows.get(k) is not None:
                    values[k] = rows[k]
        finally:
            db.close()
    except Exception:
        # A settings table that cannot be read must not take the whole app down
        # — the environment defaults are a working configuration on their own.
        pass

    _cache = values
    return values


def _import_legacy_file(db) -> dict:
    """Carry a pre-existing settings.json into the table, once."""
    if not os.path.exists(LEGACY_SETTINGS_PATH):
        return {}
    try:
        with open(LEGACY_SETTINGS_PATH) as f:
            data = json.load(f)
    except Exception:
        return {}
    from .models import AppSetting
    kept = {}
    for k, v in (data or {}).items():
        if k in _DEFAULTS and v:
            db.add(AppSetting(key=k, value=v))
            kept[k] = v
    if kept:
        db.commit()
    return kept


def get(key):
    return _load().get(key)


def set_many(**kw):
    global _cache
    from .database import SessionLocal
    from .models import AppSetting
    db = SessionLocal()
    try:
        for k, v in kw.items():
            if k not in _DEFAULTS or v is None:
                continue
            row = db.get(AppSetting, k)
            if row:
                row.value = v
            else:
                db.add(AppSetting(key=k, value=v))
        db.commit()
    finally:
        db.close()
    _cache = None


def clear_key():
    """Explicitly stores a blank rather than deleting the row: an absent row
    falls back to ANTHROPIC_API_KEY from the environment, so on a deployment
    that sets one, "turn vision off" would silently turn itself back on."""
    global _cache
    from .database import SessionLocal
    from .models import AppSetting
    db = SessionLocal()
    try:
        row = db.get(AppSetting, "anthropic_api_key")
        if row:
            row.value = ""
        else:
            db.add(AppSetting(key="anthropic_api_key", value=""))
        db.commit()
    finally:
        db.close()
    _cache = None


def masked_key():
    k = get("anthropic_api_key") or ""
    if len(k) < 8:
        return ""
    return f"{k[:6]}…{k[-4:]}"
