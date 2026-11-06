"""Fill the LIVE database with the fictional sample company, once.

    set POSTGRES_URL=<the Neon connection string from Vercel>
    set ESSA_AUTH_SECRET=<same value as on Vercel>
    set ESSA_SUPERADMIN_PASSWORD=<same value as on Vercel>
    set ESSA_ADMIN_PASSWORD=<same value as on Vercel>
    set ESSA_USER_PASSWORD=<same value as on Vercel>
    pip install -r live-api/requirements.txt fastapi[standard] httpx
    python demo-data/tools/seed_live.py

Runs the same fictional seeding as the demo's recorders (CRM, then HR, then
Finance), through the real backend in live-api/, but writes to the database you
name and records nothing. It refuses to touch a database that already has
employees or customers, so it cannot mix sample records into real ones.

For a local trial, --sqlite-dir DIR uses two SQLite files in DIR instead.
"""
import argparse
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--sqlite-dir", help="use SQLite files in this folder (a local trial) instead of POSTGRES_URL")
ap.add_argument("--api", default=str(HERE.parent.parent / "live-api"), help="the live-api folder")
args = ap.parse_args()

env = dict(os.environ)
for need in ("ESSA_AUTH_SECRET", "ESSA_SUPERADMIN_PASSWORD", "ESSA_ADMIN_PASSWORD", "ESSA_USER_PASSWORD"):
    if not (env.get(need) or "").strip():
        sys.exit(f"Set {need} first: the same value the Vercel project uses.")

if args.sqlite_dir:
    d = pathlib.Path(args.sqlite_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    wh_url = "sqlite:///" + (d / "warehouse.db").as_posix()
    shop_url = "sqlite:///" + (d / "shop.db").as_posix()
else:
    wh_url = (env.get("POSTGRES_URL") or "").strip().replace("postgres://", "postgresql://", 1)
    if not wh_url:
        sys.exit("Set POSTGRES_URL, or pass --sqlite-dir for a local trial.")
    shop_url = wh_url

from sqlalchemy import create_engine, inspect, text  # noqa: E402

schema = None if args.sqlite_dir else "shop"
wh = create_engine(wh_url)
shop = create_engine(shop_url)
if schema:
    with wh.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def count(engine, table, sch=None):
    if not inspect(engine).has_table(table, schema=sch):
        return 0
    name = f'"{sch}"."{table}"' if sch else f'"{table}"'
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()


existing = {"employees": count(wh, "employees"), "vouchers": count(wh, "vouchers"), "customers": count(shop, "customers", schema)}
if any(existing.values()):
    sys.exit(f"This database already has records {existing}: nothing was changed.")
wh.dispose()
shop.dispose()

env.update({"WAREXX_SEED_LIVE": "1", "WAREXX_SEED_WH_URL": wh_url, "WAREXX_SEED_SHOP_URL": shop_url})
env.pop("VERCEL", None)
for script in ("record_crm.py", "record_hr.py", "record_finance.py"):
    print(f"== {script}", flush=True)
    r = subprocess.run([sys.executable, str(HERE / script), "--essa", args.api], env=env)
    if r.returncode:
        sys.exit(f"{script} failed (exit {r.returncode}). The database may be part-filled: check it before retrying.")
print("Done: the live database holds the fictional sample company.")
