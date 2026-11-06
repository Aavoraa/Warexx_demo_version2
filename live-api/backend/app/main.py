"""WAREXX's live backend: real HR, Finance, sign-in, Users and the Store/CRM.

The code under this package is ESSA's (routers, services, models, unchanged in
behaviour); this file is the small application around it. It is reached only
under /live: vercel.json routes /live/api/* and /live/pos/* here, and the
LivePrefix layer below takes that prefix off. The public demo never gets here,
because its service worker answers /api and /pos from recordings and leaves /live
alone.

It FAILS CLOSED. Deployed (VERCEL set), every request except the health check is
refused until each secret below is set, so no deployment can run on a guessable
password. The Store has no login of its own here: a request with no valid
session is sent to the /live sign-in page, apart from static files and the two
public forms a customer opens from a QR code or a link (/pos/join/, /pos/feedback/).
"""
import hmac
import os
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError, ProgrammingError
from starlette.requests import Request as StarletteRequest

from . import models  # noqa: F401  (register tables)
from .config import AUTH_SECRET
from .database import Base, DB_URL, engine
from .request_memo import RequestMemoMiddleware
from .routers import auth, finance, hr, users
from .security import auth_middleware
from .security_headers import SecurityHeadersMiddleware
from .services import storage as storage_svc

REQUIRED_SECRETS = ("ESSA_AUTH_SECRET", "ESSA_SUPERADMIN_PASSWORD", "ESSA_ADMIN_PASSWORD", "ESSA_USER_PASSWORD")
LIVE_PREFIX = "/live"
POS_PUBLIC = re.compile(r"^/pos/(static|join|feedback)/")


def deployment_problems():
    """What stops this deployment from running safely. Empty on a laptop."""
    if not os.environ.get("VERCEL"):
        return []
    out = [n for n in REQUIRED_SECRETS if not (os.environ.get(n) or "").strip()]
    if len(os.environ.get("ESSA_AUTH_SECRET") or "") < 32 and "ESSA_AUTH_SECRET" not in out:
        out.append("ESSA_AUTH_SECRET (at least 32 characters)")
    if DB_URL.startswith("sqlite"):
        out.append("POSTGRES_URL (no database is attached)")
    return out


class LivePrefix:
    """Takes /live off the path, so the routes and the security rules (written
    against /api/... and /pos/...) see what they expect. _PosMount puts it back as
    the Store's SCRIPT_NAME, so the links the Store writes stay under /live/pos."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope["path"]
            if path == LIVE_PREFIX or path.startswith(LIVE_PREFIX + "/"):
                path = path[len(LIVE_PREFIX):] or "/"
                scope = dict(scope, path=path, raw_path=path.encode())
        await self.inner(scope, receive, send)


class FailClosed:
    """Refuses everything but /api/status while a required secret is missing."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope["path"] != "/api/status":
            missing = deployment_problems()
            if missing:
                resp = JSONResponse({"detail": "The live backend is not configured yet. Set: " + ", ".join(missing)},
                                    status_code=503)
                return await resp(scope, receive, send)
        await self.inner(scope, receive, send)


app = FastAPI(title="WAREXX live backend", docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth_middleware)
app.add_middleware(RequestMemoMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(FailClosed)
app.add_middleware(LivePrefix)            # added last, so it runs first

for _r in (auth, users, hr, finance):
    app.include_router(_r.router)


def boot_database():
    """Build the tables and the starting accounts. Safe to run twice: create_all
    skips what exists, and each seed creates only what is missing."""
    from .database import SessionLocal
    from .services import businesses, users as users_svc
    started = time.time()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        users_svc.seed(db)
        businesses.ensure_seed(db)
        db.commit()
    finally:
        db.close()
    return round(time.time() - started, 2)


@app.get("/api/status")
def status():
    """Whether it is ready. Names of what is missing only, never a value."""
    missing = deployment_problems()
    return {"ok": not missing, "missing": missing,
            "database": "sqlite" if DB_URL.startswith("sqlite") else "postgres"}


@app.post("/api/admin/boot")
def admin_boot(request: Request):
    """Apply the schema to a new database, authorised by ESSA_AUTH_SECRET: at that
    point there are no accounts to sign in with. Public in security.py for that reason."""
    supplied = (request.headers.get("x-essa-boot-key") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, AUTH_SECRET):
        raise HTTPException(401, "Provide ESSA_AUTH_SECRET as x-essa-boot-key")
    return {"ok": True, "seconds": boot_database()}


@app.exception_handler(OperationalError)
@app.exception_handler(ProgrammingError)
def _database_error(request, exc):
    text = str(getattr(exc, "orig", exc)).lower()
    if "no such table" in text or "does not exist" in text:
        return JSONResponse({"detail": "The database has no tables yet. Run the setup step in live-api/README.md."},
                            status_code=503)
    print(f"[live] database error: {type(exc).__name__}", flush=True)
    return JSONResponse({"detail": "The database could not complete that. Please try again."}, status_code=500)


@app.exception_handler(storage_svc.StorageError)
def _storage_error(request, exc):
    return JSONResponse({"detail": f"File storage: {exc}"}, status_code=502)


# ---------------------------------------------------------------- the Store / CRM
_pos_app = None
_pos_error = None
_pos_failed_at = 0.0


def _pos_asgi():
    """The mounted shop, built on first use and kept for the life of the instance."""
    global _pos_app, _pos_error, _pos_failed_at
    if _pos_app is not None or (_pos_error and time.monotonic() - _pos_failed_at < 20):
        return _pos_app
    try:
        import warnings
        from .pos_mount import load_pos_app
        flask_app = load_pos_app()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from starlette.middleware.wsgi import WSGIMiddleware
        _pos_app, _pos_error = WSGIMiddleware(flask_app), None
    except Exception as exc:                   # noqa: BLE001 — reported as a refusal, not raised
        _pos_error = type(exc).__name__
        _pos_failed_at = time.monotonic()
        print(f"[live] the Store did not load: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
    return _pos_app


def _with_store_access(scope):
    """Names the signed-in account for the shop (its app/sso), stripping any copy
    a browser sent. Returns (scope, signed_in)."""
    from urllib.parse import quote

    from . import pos_mount
    from .database import SessionLocal
    from .security import token_from
    from .services import permissions as perms_svc
    from .services.users import resolve_token

    name = perms_svc.STORE_HEADER.encode()
    sso_key, sso_user = pos_mount.SSO_KEY_HEADER.encode(), pos_mount.SSO_USER_HEADER.encode()
    ours = {name, sso_key, sso_user}
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in ours]
    token = token_from(StarletteRequest(dict(scope, headers=list(headers))))
    user = None
    if token:
        db = SessionLocal()
        try:
            user = resolve_token(db, token)
            if user is not None:
                narrow = perms_svc.store_access(perms_svc.normalise(user.permissions), user.role) == "user"
                who = "|".join(quote(x or "", safe="") for x in (user.username, user.role, user.full_name))
                headers += [(sso_key, pos_mount.SSO_KEY.encode()), (sso_user, who.encode())]
                if narrow:
                    headers.append((name, b"user"))
        finally:
            db.close()
    return dict(scope, headers=headers), user is not None


class _PosMount:
    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        path = scope["path"]
        pos = _pos_asgi()
        if pos is None:
            return await JSONResponse({"detail": "The Store is not available right now."},
                                      status_code=503)(scope, receive, send)
        scope, signed_in = _with_store_access(scope)
        if not signed_in and not POS_PUBLIC.match(path):
            return await RedirectResponse(LIVE_PREFIX + "/", status_code=303)(scope, receive, send)
        # the Store writes its links from SCRIPT_NAME: keep them under /live/pos
        scope = dict(scope, path=LIVE_PREFIX + path, raw_path=(LIVE_PREFIX + path).encode(),
                     root_path=LIVE_PREFIX + "/pos")
        await pos(scope, receive, send)


app.mount("/pos", _PosMount())


if not os.environ.get("VERCEL") and os.environ.get("ESSA_BOOT_MIGRATE", "1") != "0":
    boot_database()
