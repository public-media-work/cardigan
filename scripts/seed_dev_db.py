#!/usr/bin/env python3
"""Seed a local dev database from a production jobs export.

Stands up a testable instance with real job data, without touching prod.

**Do not copy prod's `dashboard.db`.** That file is ~3GB — almost entirely the
mmingest index and its FTS5 tables — and cardigan01 has 2GB of RAM. Snapshotting
it in place wedged the box on 2026-08-14 (thrash, not disk-full: 22GB was free
and nothing was OOM-killed). A dev instance needs the ~40 job rows and their
OUTPUT directories; together those are under 3MB.

## Standing up a dev instance

Export the two small things from prod (read-only, negligible IO)::

    ssh cardigan01 'docker exec cardigan-api-1 python -c "
    import sqlite3, json
    con = sqlite3.connect(\"file:/data/db/dashboard.db?mode=ro\", uri=True)
    con.row_factory = sqlite3.Row
    print(json.dumps([dict(r) for r in con.execute(\"SELECT * FROM jobs\")], default=str))
    "' > .devdata/jobs.json

    ssh cardigan01 'ionice -c3 nice -n19 tar czf /tmp/out.tgz \
        -C /var/lib/docker/volumes/cardigan_output-data/_data .'
    scp cardigan01:/tmp/out.tgz .devdata/ && ssh cardigan01 'rm /tmp/out.tgz'
    mkdir -p .devdata/output && tar xzf .devdata/out.tgz -C .devdata/output

Then build the DB and seed it::

    DATABASE_PATH=.devdata/dashboard.db alembic upgrade head
    python scripts/seed_dev_db.py

Turn item extraction on for the dev instance only (never commit this)::

    python -c "import json; c=json.load(open('config/llm-config.json')); \
        c['routing']['items']['enabled']=True; \
        json.dump(c, open('.devdata/llm-config.json','w'), indent=2)"

Run it::

    export AIRTABLE_API_KEY=...        # reads only; see the safety note below
    DATABASE_PATH=.devdata/dashboard.db \
    LLM_CONFIG_PATH=.devdata/llm-config.json \
        uvicorn api.main:app --host 127.0.0.1 --port 8100

`config/instances.json` already carries a `dev` entry pointing at
`localhost:8100`, so the web app can switch to it with no config change.

## Airtable safety

The dev instance reads the **same** base as prod (`appZ2HGwhiifQToB6`) — there is
no sandbox base. That is safe for the REST API by construction: there is no
Airtable-write endpoint, and SST writes exist only through the MCP server's
`propose -> review -> commit` path. **Do not run the MCP server against a dev
instance**, and prefer a read-only Airtable PAT.

`.devdata/` is gitignored: it holds real production job data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = REPO_ROOT / ".devdata"

PROD_OUTPUT_PREFIX = "/data/output/"


def seed(db_path: Path, jobs_json: Path, output_dir: Path) -> int:
    rows = json.loads(jobs_json.read_text())

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
    if not columns:
        print(f"ERROR: no jobs table in {db_path}.", file=sys.stderr)
        print(f"Run: DATABASE_PATH={db_path} alembic upgrade head", file=sys.stderr)
        return 1

    inserted = skipped = remapped = 0
    for row in rows:
        # Prod may be a migration or two behind (or ahead); only carry columns
        # this schema actually has.
        payload = {key: value for key, value in row.items() if key in columns}

        # Repoint project_path at the local OUTPUT copy so the item extractor
        # can read the phase outputs.
        path = payload.get("project_path") or ""
        if path.startswith(PROD_OUTPUT_PREFIX):
            payload["project_path"] = str(output_dir / path[len(PROD_OUTPUT_PREFIX) :])
            remapped += 1

        placeholders = ", ".join("?" for _ in payload)
        try:
            con.execute(
                f"INSERT INTO jobs ({', '.join(payload)}) VALUES ({placeholders})",
                list(payload.values()),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1  # already seeded; re-running is harmless

    con.commit()

    total = con.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    with_output = sum(
        1
        for row in con.execute("SELECT project_path FROM jobs")
        if (Path(row["project_path"]) / "seo_output.md").is_file()
    )
    con.close()

    print(f"inserted={inserted} skipped={skipped} path_remapped={remapped}")
    print(f"jobs in dev db: {total} ({with_output} with seo_output.md on disk)")
    print("\nNext: backfill items with  python scripts/backfill_job_items.py --all")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEV_DIR / "dashboard.db")
    parser.add_argument("--jobs-json", type=Path, default=DEV_DIR / "jobs.json")
    parser.add_argument("--output-dir", type=Path, default=DEV_DIR / "output")
    args = parser.parse_args()

    if not args.jobs_json.is_file():
        print(f"ERROR: {args.jobs_json} not found — see this script's docstring.", file=sys.stderr)
        return 1

    return seed(args.db, args.jobs_json, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
