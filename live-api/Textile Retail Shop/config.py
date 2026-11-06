import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    # Signs sessions, CSRF tokens and the customer QR/feedback links, so it must be
    # stable across instances and never a known value: derived from the backend secret.
    SECRET_KEY = os.environ.get("SECRET_KEY") or (
        __import__("hashlib").sha256(("shop:" + os.environ["ESSA_AUTH_SECRET"]).encode()).hexdigest()
        if os.environ.get("ESSA_AUTH_SECRET") else __import__("secrets").token_urlsafe(48))
    # Where the shop's data lives.
    #
    # An explicit DATABASE_URL wins, whatever its scheme — that is somebody
    # saying exactly which database to use, and a SQLite path is a perfectly
    # ordinary thing to say (a test, a second till, a copy to poke at). An
    # earlier version of this only accepted Postgres URLs here and silently fell
    # back to the shop's own file for anything else, which meant a run that
    # thought it was pointed at a scratch database was quietly writing to the
    # real one.
    #
    # Failing that, the deployment's own spellings, in the order the warehouse
    # reads them (backend/app/config.DB_URL_CANDIDATES). Reading only
    # DATABASE_URL meant a host that spells it POSTGRES_URL — which is what
    # Vercel and Supabase hand out — sent the warehouse to Postgres and the shop
    # to a SQLite file nothing else could see, with no error to say so.
    #
    # `postgres://` is rewritten because SQLAlchemy has refused that spelling
    # since 1.4 while most dashboards still print it.
    SQLALCHEMY_DATABASE_URI = next(
        (v.replace("postgres://", "postgresql://", 1)
         for v in (os.environ.get(k, "").strip() for k in (
             "DATABASE_URL", "ESSA_DATABASE_URL", "POSTGRES_URL",
             "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING"))
         if v),
        f"sqlite:///{BASE_DIR / 'textile_shop.db'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # How long a till waits for another till's write before giving up.
    #
    # SQLite allows one writer at a time, and its default behaviour when it finds
    # the database busy is to fail AT ONCE rather than wait — which at a shop
    # with two counters means the second cashier's sale dies with "database is
    # locked" while the first one's is still committing. A timeout turns that
    # into a wait of a few milliseconds, which is what it should always have
    # been. Fifteen seconds is far longer than any bill takes and short enough
    # that a genuinely stuck database still surfaces as an error.
    #
    # Only for SQLite: Postgres has proper row locking and this parameter does
    # not exist there.
    if SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}}
    # Re-read templates from disk on every request so edits appear on a refresh
    # (otherwise Flask caches them until the server restarts).
    TEMPLATES_AUTO_RELOAD = True

    # The sign-in cookie is not sent with a form another website posts here.
    # The shop's forms carry no token of their own, so without this a page on any
    # site could submit one — a refund, a floor move — as whoever is signed in
    # (Firefox and Safari send a cookie with no SameSite on such a post). Lax
    # still sends it on every page of the shop and inside the Essa frame, which
    # is the same site; only the cross-site POST loses it.
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # Business / GST defaults
    # The shop's own name, shown on its login screen, its chrome and its bills.
    #
    # The default is the generic word, NOT a business name. It used to be "Taqua
    # Silks", which is one particular shop — so every other install of this
    # software greeted its staff with somebody else's brand, and printed it on
    # the login page of a store that had been given a perfectly good name of its
    # own when it was created.
    #
    # Where the shop runs mounted inside the warehouse app, the mount replaces
    # this with the STORE's real name (see backend/app/pos_mount). The
    # environment still wins over both, for a standalone install.
    SHOP_NAME = os.environ.get("SHOP_NAME", "Store")
    SHOP_ADDRESS = os.environ.get("SHOP_ADDRESS", "Municipal Office Rd, Noyyal, Tiruppur, Tamil Nadu 641604")
    SHOP_PHONE = os.environ.get("SHOP_PHONE", "+91 98765 43210")
    SHOP_GSTIN = os.environ.get("SHOP_GSTIN", "33ABCDE1234F1Z5")
    SHOP_STATE_CODE = os.environ.get("SHOP_STATE_CODE", "33")  # Tamil Nadu

    # Loyalty: 1 point per rupee spent above LOYALTY_MIN_BILL, 1 point = LOYALTY_POINT_VALUE INR
    LOYALTY_EARN_RATE = float(os.environ.get("LOYALTY_EARN_RATE", "0.01"))  # 1% of bill
    LOYALTY_POINT_VALUE = float(os.environ.get("LOYALTY_POINT_VALUE", "1.0"))
    LOYALTY_MIN_BILL = float(os.environ.get("LOYALTY_MIN_BILL", "500"))

    LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "5"))

    # The month a financial year opens on — April, for India. It decides the two
    # digits in a floor bill number (TG**26**-001 is the year that began in April
    # 2026) and therefore when each floor's series restarts at 001. Config rather
    # than a constant because a business that closes its books in January is
    # still a business; see app/billing_numbers.py.
    FY_START_MONTH = int(os.environ.get("FY_START_MONTH", "4"))
