# WAREXX Live: the real backend for HR, Finance and the CRM

The public demo (`/demo/`) replays recordings and saves nothing. **WAREXX Live** (`/live/`)
is the same set of screens on a real server and a real database, for staff with an account.

| | Public demo | WAREXX Live |
|---|---|---|
| Address | `/demo/` | `/live/` |
| Who | Anyone | Signed-in staff only |
| Data | Recordings in the browser | Postgres |
| Saves | Discarded | Kept |

The two never mix. The demo's service worker only answers `/api` and `/pos`; live calls go to
`/live/api` and `/live/pos`, which it leaves alone (`npm test` checks this).

## What is here

- `index.py`: the Vercel entry point (the `live` service in `../vercel.json`).
- `backend/app/`: ESSA's HR, Finance, sign-in and Users code, with a small `main.py` around it.
- `Textile Retail Shop/`: ESSA's Store app, which holds the CRM, mounted at `/live/pos`.
- `../frontend/live/`: the signed-in app, reusing the WAREXX sign-in, sidebar and screens.

Differences from ESSA, all on purpose: no known default passwords or secret keys; no
ESSA company name by default; the Store has no login of its own and sends anyone without a
session to `/live/`; WAREXX's Store stylesheet; API docs off.

## Set it up on Vercel

1. **Attach the database.** Project > Storage > Create Database > Neon. This sets `POSTGRES_URL`.
2. **Set four environment variables**, each a long random value of your own:
   `ESSA_AUTH_SECRET` (at least 32 characters), `ESSA_SUPERADMIN_PASSWORD`, `ESSA_ADMIN_PASSWORD`,
   `ESSA_USER_PASSWORD`. Until all are set the backend refuses every request and says which are
   missing; `/live/api/status` lists the names.
3. **Deploy.**
4. **Create the tables and fill them**, once, from your own computer, with the same values set in your
   shell, from a folder with a short path (Windows' 260-character limit breaks the Store otherwise):

   ```bash
   pip install -r live-api/requirements.txt httpx
   python demo-data/tools/seed_live.py
   ```

   This adds the fictional sample company. It refuses to run on a database that already has
   employees, vouchers or customers. To start empty instead of with sample data, skip it and call:

   ```bash
   curl -X POST https://YOUR-SITE/live/api/admin/boot -H "x-essa-boot-key: YOUR_ESSA_AUTH_SECRET"
   ```

5. **Sign in** at `/live/` as `superadmin`, and create each person's own account under Users & Access.

Optional: `BLOB_READ_WRITE_TOKEN` (Vercel Blob) stores employee documents and photos, since a
function's own disk does not last. `ANTHROPIC_API_KEY` turns on reading details from uploaded documents.

## Before real data goes in

The sample data and the sample accounts are fictional; clear them before using this for real. Also
review these, which come from ESSA's design:

- Signing out clears the browser's cookie, but a copy of a session token stays valid until it expires
  (up to 30 days). Changing the account's password is the way to cut one off.
- Everyone with an account sees one shared company. There is no per-customer separation.

## Run it locally

Keep the repo in a short path. With the four variables set in your shell:

```bash
cd live-api && python -m uvicorn index:app --port 8010
```

```bash
cd frontend && npx vite --config vite.live.config.js
```

Then open http://localhost:8004/live/. With no database variable set it uses a local SQLite file.
