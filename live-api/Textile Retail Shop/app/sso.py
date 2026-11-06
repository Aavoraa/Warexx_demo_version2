"""Sign the Store in as whoever is signed in to Essa.

Inside the Essa app the Store is a screen in a frame, and asking for a second
username and password there — a different one, for a till the person was
already allowed to open — was a login nobody needed. So when the shop is served
by the Essa backend (backend/app/pos_mount), the mount names the Essa account on
every request and the shop signs that account in.

TRUST. The name arrives as a header, and a header is something any client can
send. It is believed only beside `x-essa-sso-key` carrying the key the mount
generated when it loaded this shop (ESSA_STORE_SSO_KEY, made fresh per process
and never written down); the mount strips any copy a browser sent. A shop run on
its own has no key configured, believes nothing, and keeps its own login — the
phone at /floor, a standalone install, a till outside Essa.

WHICH SHOP USER. One per Essa account, with the same username: the Essa
`admin` is the shop's `admin`. An account the shop has never seen is created on
first visit with a random password (it signs in through Essa, never through the
form). Its shop role follows the Essa role — Admin and above run the store,
User bills — and is kept in step, so a promotion in Users & Access reaches the
till. A shop account switched off in Store → Staff stays off: the shop's own
"active" still decides.

Essa's Store access (User → billing screens only) is a separate header, read by
modules.store_user, and still narrows whoever this signs in.
"""
import hmac
import os
import secrets
from urllib.parse import unquote

from flask_login import current_user, login_user

KEY_HEADER = "x-essa-sso-key"
USER_HEADER = "x-essa-store-user"

#: Essa role -> shop role. Admin and above manage the store; a User bills.
ROLE = {"superboss": "admin", "superadmin": "admin", "admin": "admin", "user": "cashier"}


def _key():
    return os.environ.get("ESSA_STORE_SSO_KEY") or ""


def essa_account(req):
    """(username, role, full name) of the Essa account behind this request, or
    None when the request did not come through the Essa mount."""
    key = _key()
    sent = req.headers.get(KEY_HEADER) or ""
    if not key or not sent or not hmac.compare_digest(sent, key):
        return None
    parts = [unquote(p) for p in (req.headers.get(USER_HEADER) or "").split("|")]
    if len(parts) != 3 or not parts[0]:
        return None
    return parts[0], parts[1], parts[2]


def sign_in(req, db, User):
    """Sign the request's Essa account in, creating its shop user if needed.

    Returns the shop User now signed in, or None (no Essa account, or its shop
    account is switched off — the shop's own login page then answers)."""
    acct = essa_account(req)
    if not acct:
        return None
    username, essa_role, full_name = acct
    role = ROLE.get(essa_role, "cashier")
    if current_user.is_authenticated and current_user.username == username:
        user = current_user._get_current_object()
    else:
        user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username, full_name=full_name or username, role=role)
        user.set_password(secrets.token_urlsafe(24))
        db.session.add(user)
        db.session.commit()
    elif user.role != role and user.role != "manager":
        # kept in step with Users & Access; a shop-side "manager" is a role Essa
        # has no word for, so it is left as the store set it
        user.role = role
        db.session.commit()
    if not user.active:
        return None
    if not (current_user.is_authenticated and current_user.id == user.id):
        login_user(user)
    return user
