"""
Where an uploaded invoice actually lives.

On a laptop it is a file under `data/uploads`, which is what this has always
been. On Vercel there is no such place — the filesystem is read-only apart from
`/tmp`, and `/tmp` is per-invocation, so a file written while handling the upload
is gone before anyone asks to see it. There the bytes go to Vercel Blob and the
database keeps the URL.

Two things this deliberately does NOT do:

*Serve the blob URL to the browser.* Every read is proxied back through
`/api/documents/{id}/image`, which is policed like every other route, so an
invoice scan stays behind the login it was behind before. The store is private,
so its URLs would not work in a browser anyway — but the proxy is what makes
that a design decision rather than a dependency on a dashboard setting nobody
would think to check before flipping.

*Change how extraction works.* The engine takes a filesystem path and opens it
(`engine.run_extraction`, `lr_svc.extract_lr`). Rather than rewrite that to take
bytes, `materialise` puts the bytes somewhere real for the length of one request
— which is exactly what `/tmp` is for, and is the only thing it is reliable for.
"""
import mimetypes
import os
import tempfile

import httpx

from ..config import UPLOAD_DIR

# Set by Vercel when a Blob store is attached to the project. Its presence is
# what switches this module over — there is no separate mode flag to forget.
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
BLOB_API = "https://blob.vercel-storage.com"
# Pinned: the header is required, and a floating version would let a change at
# the other end alter what `put` returns without anything here being edited.
#
# The REST interface behind the official SDK is not published, so this and the
# header names below are read out of @vercel/blob's own published bundle, where
# the constant is `BLOB_API_VERSION = 12`. Two earlier values here were guesses
# and both were rejected. If uploads start failing after a platform change, this
# number is the first thing to check — against that bundle, not against
# intuition.
BLOB_API_VERSION = "12"

# Every upload states the access level, and the API rejects the request if it
# disagrees with how the store was created ("Cannot use public access on a
# private store"). Private is the default here because that is what a store
# created today is, and because a supplier invoice should not be readable by
# anyone holding a URL — reads are proxied through the API precisely so it is
# not. Overridable for a store deliberately made public.
BLOB_ACCESS = os.environ.get("BLOB_ACCESS", "private")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class StorageError(RuntimeError):
    """Something went wrong storing or fetching a file, with the reason in the
    message. A distinct type so the routers can turn it into an answer that
    names the storage rather than a bare 500 that names nothing."""


def using_blob() -> bool:
    return bool(BLOB_TOKEN)


def _read_headers() -> dict:
    """Reads carry the token too.

    A Blob store created today is PRIVATE, and a private object refuses an
    unauthenticated GET — so a read without this returns 403 and an invoice
    appears to have vanished. The header is harmless on a public store, which
    is why it is sent unconditionally rather than switched on a setting that
    would have to be kept in step with the dashboard.
    """
    return {"authorization": f"Bearer {BLOB_TOKEN}"} if BLOB_TOKEN else {}


def backend_name() -> str:
    return "vercel-blob" if using_blob() else "local-disk"


# ---------------------------------------------------------------------------
#  writing
# ---------------------------------------------------------------------------

def save(raw: bytes, name: str) -> str:
    """Store these bytes durably and return the reference to keep in the DB.

    The reference is a filesystem path on a laptop and an https URL on Vercel.
    Callers treat it as opaque and hand it back to the functions below — which
    is why `Document.stored_path` did not need a new column or a migration.
    """
    if not using_blob():
        stored = os.path.join(UPLOAD_DIR, name)
        # The name is content-addressed by every caller, so a file that is
        # already there has identical bytes. Not rewriting it also avoids
        # clobbering the read-only sample invoices the seeder puts here.
        if not os.path.exists(stored):
            with open(stored, "wb") as f:
                f.write(raw)
            os.chmod(stored, 0o644)
        return stored

    # No x-add-random-suffix: it defaults to off, which is what keeps the
    # content hash the caller chose as the whole name.
    #
    # x-allow-overwrite IS needed, and its absence was the second bug here. The
    # names are content hashes, so uploading the same invoice twice targets the
    # same object — and without this the API rejects that as a collision rather
    # than treating it as the no-op it is. Identical bytes under an identical
    # name is exactly the case worth allowing.
    # The name goes in the QUERY STRING, not in the path. `PUT /{name}` looks
    # like the obvious REST shape and is what this did first; the API answers it
    # with "Invalid pathname", because it reads the name from ?pathname= and
    # there was none. The SDK builds exactly this:
    #     const params = new URLSearchParams({ pathname });
    #     requestApi(`/?${params.toString()}`, { method: 'PUT', body, headers })
    try:
        r = httpx.put(
            f"{BLOB_API}/",
            params={"pathname": name},
            content=raw,
            headers={
                "authorization": f"Bearer {BLOB_TOKEN}",
                "x-api-version": BLOB_API_VERSION,
                "x-content-type": mime_for(name),
                # The name is x-vercel-blob-access, not the x-access an
                # educated guess produces — taken from the official SDK's own
                # dist, which is the only place it is written down.
                "x-vercel-blob-access": BLOB_ACCESS,
                "x-allow-overwrite": "1",
                "x-cache-control-max-age": "31536000",
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"could not reach blob storage: {type(exc).__name__}: {exc}") from exc

    # raise_for_status alone gives "Client error '400 Bad Request' for url …",
    # which names neither the objection nor the field it was about. The body
    # does, and without it the only way to find out is another deploy.
    if r.status_code >= 400:
        raise StorageError(
            f"blob storage refused the upload: HTTP {r.status_code} — {r.text[:400]}")
    try:
        return r.json()["url"]
    except (ValueError, KeyError) as exc:
        raise StorageError(
            f"blob storage returned no url: HTTP {r.status_code} — {r.text[:400]}") from exc


# ---------------------------------------------------------------------------
#  reading
# ---------------------------------------------------------------------------

def read(ref: str) -> bytes | None:
    """The stored bytes, or None if the reference no longer resolves."""
    if not ref:
        return None
    if not _is_remote(ref):
        if not os.path.exists(ref):
            return None
        with open(ref, "rb") as f:
            return f.read()
    try:
        r = httpx.get(ref, headers=_read_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


def materialise(ref: str) -> str | None:
    """A real filesystem path for these bytes, for code that opens files.

    A local reference is already one. A remote one is fetched into the
    per-request scratch directory — which on Vercel is `/tmp`, the one writable
    place, and which nothing may assume still exists on the next request.
    """
    if not ref:
        return None
    if not _is_remote(ref):
        return ref if os.path.exists(ref) else None
    raw = read(ref)
    if raw is None:
        return None
    ext = os.path.splitext(ref.split("?")[0])[1] or ".bin"
    fd, path = tempfile.mkstemp(suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def missing(ref: str) -> bool:
    """True only when the bytes CERTAINLY cannot be fetched.

    This replaces an `exists()` that answered the opposite question and answered
    it the wrong way round: anything it was unsure of, it called absent. Nothing
    called it, so that was harmless — right up until the merge preflight did,
    and every uncertainty became a refusal to merge. Deleted rather than kept
    beside this one, because the two read as interchangeable and only one of
    them is safe to refuse work on.

    A HEAD is the specific worry. It is not a GET, a store can decline it while
    serving the object perfectly well, and an object this app reads successfully
    every time would then be reported missing. So a HEAD that does not clearly
    succeed settles nothing, and the question is put again as the one-byte range
    request — a real GET, the same verb `read` uses, for one byte instead of a
    two-megabyte photograph. A store that ignores the Range header sends the
    whole object and is still answering the question asked.

    Only a local path is authoritative on its own: os.path.exists IS the answer
    there, and it is the case running on a warehouse PC.
    """
    if not ref:
        return True
    if not _is_remote(ref):
        return not os.path.exists(ref)
    try:
        if httpx.head(ref, headers=_read_headers(), timeout=_TIMEOUT).status_code < 400:
            return False
    except httpx.HTTPError:
        pass
    try:
        r = httpx.get(ref, headers={**_read_headers(), "range": "bytes=0-0"},
                      timeout=_TIMEOUT)
        return r.status_code >= 400
    except httpx.HTTPError:
        # Unreachable is not the same as absent — the network is down, or the
        # store is having a moment. Reported as present, because the caller's
        # response to "missing" is to refuse, and refusing the operator's work
        # over a timeout is worse than attempting it and reporting what happened.
        return False


def delete(ref: str) -> None:
    """Best effort. A blob that outlives its row costs a fraction of a cent and
    is not worth failing a delete over — the row going is what the user asked
    for, and an orphaned object is invisible to them."""
    if not ref:
        return
    if not _is_remote(ref):
        try:
            os.remove(ref)
        except OSError:
            pass
        return
    # POST, not DELETE — the endpoint is /delete and it takes a list, so the
    # verb is the SDK's and not the one the name suggests.
    try:
        httpx.post(
            f"{BLOB_API}/delete",
            json={"urls": [ref]},
            headers={"authorization": f"Bearer {BLOB_TOKEN}",
                     "x-api-version": BLOB_API_VERSION,
                     "content-type": "application/json"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        pass


def _is_remote(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")


def mime_for(ref: str) -> str:
    """The stored object's content type, from its extension.

    Worth getting right rather than sending octet-stream for everything: it is
    what comes back on the GET, and the invoice viewer decides whether it has an
    image or a PDF from that.

    Public because the image route needs it too. A document carries ONE `mime`,
    recorded from the first page — and a bill photographed page by page and then
    merged can perfectly well be a JPEG followed by a PNG, so page two has to be
    typed from its own reference rather than from the document's.

    The query string goes first: a blob reference is a URL, and `?download=1`
    on the end of it is not a file extension.
    """
    return mimetypes.guess_type(ref.split("?")[0])[0] or "application/octet-stream"
