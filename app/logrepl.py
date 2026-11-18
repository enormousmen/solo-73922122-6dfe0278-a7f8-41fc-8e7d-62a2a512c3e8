"""Publisher-side logical-replication check for UPDATE/DELETE (PostgreSQL 16).

Given a regular table (schema, name), report for every publication that
covers it -- directly (``FOR TABLE``), via its schema (``FOR TABLES IN
SCHEMA``) or database-wide (``FOR ALL TABLES``) -- whether UPDATE and DELETE
can be emitted from the publisher, and if not, why not.

The verdict rules mirror PostgreSQL 16's own publisher-side checks
(``CheckCmdReplicaIdentity`` in src/backend/executor/execReplication.c plus
``pub_rf_contains_invalid_column`` / ``pub_collist_contains_invalid_column``
in src/backend/commands/publicationcmds.c; verified against a live
PostgreSQL 16 server):

* the publication must publish the operation (``pubupdate`` / ``pubdelete``);
  otherwise the operation is simply not emitted via that publication
  (verdict ``disabled``);
* the table needs a usable replica identity: REPLICA IDENTITY FULL, a
  replica identity index (USING INDEX), or -- for the default -- a primary
  key; otherwise PostgreSQL rejects UPDATE/DELETE on the publisher
  ("cannot update/delete ... does not have a replica identity");
* a per-table column list must cover all replica identity columns, and is
  never allowed together with REPLICA IDENTITY FULL ("Column list used by
  the publication does not cover the replica identity");
* a row filter may only reference replica identity columns (unless the
  identity is FULL) ("Column used in the publication WHERE expression is
  not part of the replica identity").

Guarantees:

* one snapshot: every catalog row is read inside a single REPEATABLE READ
  READ ONLY transaction on a dedicated short-lived connection -- the two
  pooled diagnostic connections are never used for this;
* no partial conclusions: any query failure aborts the whole check (the API
  layer maps it to 502); either a full assessment or an error is returned;
* publisher-side only: the result never claims that any subscriber received
  or applied anything.
"""
from __future__ import annotations

import logging
import re

import psycopg
from psycopg import IsolationLevel

from . import config

log = logging.getLogger("diag.logrepl")


class TableNotFound(Exception):
    """The requested schema.table does not exist."""


class UnsupportedRelation(Exception):
    """The relation exists but this check does not apply to it."""


RELKIND_TEXT = {
    "r": "regular table",
    "p": "partitioned table",
    "i": "index",
    "S": "sequence",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
    "c": "composite type",
    "t": "TOAST table",
}

REPLIDENT_MODE = {"d": "default", "n": "nothing", "f": "full", "i": "index"}

# --------------------------------------------------------------------------
# catalog queries (all executed inside one REPEATABLE READ READ ONLY
# transaction, i.e. against one consistent snapshot)
# --------------------------------------------------------------------------

SQL_TABLE = """
SELECT c.oid, c.relkind, c.relispartition, c.relreplident, c.relnamespace
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

SQL_PK_COLUMNS = """
SELECT a.attname
FROM pg_index i
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %s AND i.indisprimary
ORDER BY k.ord
"""

SQL_RI_INDEX = """
SELECT i.indexrelid::regclass::text AS index_name, i.indisvalid,
       array_agg(a.attname ORDER BY k.ord) AS columns
FROM pg_index i
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = %s AND i.indisreplident
GROUP BY 1, 2
"""

SQL_COLUMNS = """
SELECT attnum, attname
FROM pg_attribute
WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped
ORDER BY attnum
"""

SQL_PUBLICATIONS = """
SELECT oid, pubname, puballtables, pubinsert, pubupdate, pubdelete,
       pubtruncate, pubviaroot
FROM pg_publication
ORDER BY pubname
"""

SQL_TABLE_MEMBERSHIPS = """
SELECT prpubid,
       pg_get_expr(prqual, prrelid) AS row_filter,
       prqual::text AS row_filter_node,
       prattrs::int2[] AS column_attnums
FROM pg_publication_rel
WHERE prrelid = %s
"""

SQL_SCHEMA_MEMBERSHIPS = """
SELECT pnpubid FROM pg_publication_namespace WHERE pnnspid = %s
"""


def fetch_snapshot(schema: str, table: str, dsn: str | None = None) -> dict:
    """Read every catalog row the assessment needs from ONE snapshot.

    Uses a dedicated short-lived connection (never one of the two pooled
    diagnostic connections).  All queries run inside a single REPEATABLE
    READ READ ONLY transaction, so they all see the same catalog snapshot.
    Any error propagates to the caller; no partial snapshot is returned.
    """
    conn = psycopg.connect(
        dsn or config.DATABASE_URL,
        autocommit=False,
        connect_timeout=config.CONNECT_TIMEOUT_S,
        # our own queries never use server-side prepared statements
        prepare_threshold=None,
        options=f"-c statement_timeout={config.LOGREPL_STATEMENT_TIMEOUT_MS}",
    )
    try:
        # Must be set before the first statement: one snapshot for all reads.
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(SQL_TABLE, (schema, table))
            row = cur.fetchone()
            if row is None:
                return {"table": None}
            oid, relkind, relispartition, relreplident, nsp = row
            snap: dict = {
                "table": {
                    "oid": oid,
                    "relkind": relkind,
                    "relispartition": relispartition,
                    "relreplident": relreplident,
                }
            }
            cur.execute(SQL_PK_COLUMNS, (oid,))
            snap["pk_columns"] = [r[0] for r in cur.fetchall()]
            cur.execute(SQL_RI_INDEX, (oid,))
            ri = cur.fetchone()
            snap["ri_index"] = (
                {"name": ri[0], "indisvalid": ri[1], "columns": list(ri[2])}
                if ri is not None
                else None
            )
            cur.execute(SQL_COLUMNS, (oid,))
            snap["columns"] = {attnum: name for attnum, name in cur.fetchall()}
            cur.execute(SQL_PUBLICATIONS)
            snap["publications"] = [
                {
                    "oid": r[0],
                    "pubname": r[1],
                    "puballtables": r[2],
                    "pubinsert": r[3],
                    "pubupdate": r[4],
                    "pubdelete": r[5],
                    "pubtruncate": r[6],
                    "pubviaroot": r[7],
                }
                for r in cur.fetchall()
            ]
            cur.execute(SQL_TABLE_MEMBERSHIPS, (oid,))
            snap["table_memberships"] = {
                r[0]: {
                    "row_filter": r[1],
                    "row_filter_node": r[2],
                    "column_attnums": list(r[3]) if r[3] is not None else None,
                }
                for r in cur.fetchall()
            }
            cur.execute(SQL_SCHEMA_MEMBERSHIPS, (nsp,))
            snap["schema_pubids"] = {r[0] for r in cur.fetchall()}
        conn.commit()  # release the snapshot (read-only transaction)
        return snap
    finally:
        conn.close()


# --------------------------------------------------------------------------
# assessment (pure functions over the snapshot; no database access)
# --------------------------------------------------------------------------

# A column reference inside a stored row-filter expression is serialized as
# {VAR :varno 1 :varattno N ...} in the pg_node_tree text.  PostgreSQL's own
# row-filter validity check walks exactly these Var nodes.
_VARATTNO_RE = re.compile(r"\{VAR\b[^}]*?:varattno\s+(-?\d+)")


def _row_filter_attnums(node_tree: str | None) -> set[int]:
    """Attribute numbers of every column referenced by a row filter."""
    if not node_tree:
        return set()
    return {int(m.group(1)) for m in _VARATTNO_RE.finditer(node_tree)}


def _replica_identity(tbl: dict, snap: dict) -> dict:
    """Resolve the table's replica identity to a status + key columns."""
    mode = REPLIDENT_MODE.get(tbl["relreplident"])
    if mode == "default":
        pk = snap["pk_columns"]
        if pk:
            return {
                "mode": mode,
                "status": "ok",
                "key_columns": pk,
                "detail": "REPLICA IDENTITY DEFAULT; primary key column(s): "
                + ", ".join(pk),
            }
        return {
            "mode": mode,
            "status": "unusable",
            "key_columns": [],
            "detail": "REPLICA IDENTITY DEFAULT but the table has no primary key",
        }
    if mode == "nothing":
        return {
            "mode": mode,
            "status": "unusable",
            "key_columns": [],
            "detail": "REPLICA IDENTITY NOTHING",
        }
    if mode == "full":
        return {
            "mode": mode,
            "status": "ok",
            "key_columns": None,  # all columns
            "detail": "REPLICA IDENTITY FULL; the whole old row serves as identity",
        }
    if mode == "index":
        idx = snap["ri_index"]
        if idx is None:
            return {
                "mode": mode,
                "status": "unusable",
                "key_columns": [],
                "detail": "REPLICA IDENTITY USING INDEX but no index with "
                "indisreplident exists (the index was dropped); PostgreSQL "
                "treats the table as having no replica identity",
            }
        if not idx["indisvalid"]:
            return {
                "mode": mode,
                "status": "unknown",
                "key_columns": None,
                "detail": f"replica identity index {idx['name']} has "
                "indisvalid=false; cannot determine the identity columns",
            }
        return {
            "mode": mode,
            "status": "ok",
            "key_columns": idx["columns"],
            "detail": f"REPLICA IDENTITY USING INDEX {idx['name']} "
            f"({', '.join(idx['columns'])})",
        }
    return {
        "mode": mode or f"unknown({tbl['relreplident']!r})",
        "status": "unknown",
        "key_columns": None,
        "detail": f"unrecognized relreplident {tbl['relreplident']!r}",
    }


def _assess_op(
    pub: dict,
    op: str,
    ri: dict,
    collist: list[str] | None,
    row_filter: str | None,
    rf_columns: list[str],
) -> dict:
    """Verdict for one publication and one operation (update/delete)."""
    if not pub[f"pub{op}"]:
        return {
            "verdict": "disabled",
            "reasons": [
                f"publication does not publish {op}s (pub{op}=off); "
                f"{op.upper()} changes are not emitted via this publication"
            ],
        }
    if ri["status"] == "unknown":
        return {
            "verdict": "unknown",
            "reasons": [f"replica identity cannot be evaluated: {ri['detail']}"],
        }
    reasons: list[str] = []
    if ri["status"] == "unusable":
        reasons.append(
            f"table has no usable replica identity ({ri['detail']}); "
            f"PostgreSQL rejects {op.upper()} on the publisher while the "
            f"table is published (\"... does not have a replica identity and "
            f"publishes {op}s\")"
        )
    if collist is not None:
        if ri["mode"] == "full":
            reasons.append(
                "a publication column list is not allowed with REPLICA "
                "IDENTITY FULL (PostgreSQL: \"Column list used by the "
                "publication does not cover the replica identity.\")"
            )
        else:
            missing = [c for c in ri["key_columns"] if c not in collist]
            if missing:
                reasons.append(
                    "column list does not cover replica identity column(s): "
                    + ", ".join(missing)
                    + " (PostgreSQL: \"Column list used by the publication "
                    "does not cover the replica identity.\")"
                )
    if row_filter is not None and ri["mode"] != "full":
        key = ri["key_columns"] or []
        bad = [c for c in rf_columns if c not in key]
        if bad:
            reasons.append(
                "row filter references column(s) outside the replica "
                "identity: "
                + ", ".join(bad)
                + " (PostgreSQL: \"Column used in the publication WHERE "
                "expression is not part of the replica identity.\")"
            )
    if reasons:
        return {"verdict": "blocked", "reasons": reasons}
    return {"verdict": "ok", "reasons": []}


def _assess_publication(
    pub: dict,
    coverage: list[str],
    member: dict | None,
    ri: dict,
    columns: dict[int, str],
) -> dict:
    collist: list[str] | None = None
    row_filter: str | None = None
    rf_columns: list[str] = []
    if member is not None:
        if member["column_attnums"] is not None:
            collist = [columns.get(a, f"attnum {a}") for a in member["column_attnums"]]
        row_filter = member["row_filter"]
        if row_filter is not None:
            attnums = _row_filter_attnums(member["row_filter_node"])
            rf_columns = sorted(columns.get(a, f"attnum {a}") for a in attnums)

    notes: list[str] = []
    if row_filter is not None:
        notes.append(
            f"row filter {row_filter}: only rows matching the filter are "
            "covered; an UPDATE moving a row across the filter boundary is "
            "published as INSERT/DELETE instead"
        )
    if collist is not None:
        notes.append(
            "column list: only the listed column(s) are published "
            "(" + ", ".join(collist) + ")"
        )

    return {
        "publication": pub["pubname"],
        "coverage": coverage,
        "publish_update": pub["pubupdate"],
        "publish_delete": pub["pubdelete"],
        "column_list": collist,
        "row_filter": row_filter,
        "update": _assess_op(pub, "update", ri, collist, row_filter, rf_columns),
        "delete": _assess_op(pub, "delete", ri, collist, row_filter, rf_columns),
        "notes": notes,
    }


def build_assessment(schema: str, table: str, snap: dict) -> dict:
    """Build the full per-publication assessment from a catalog snapshot."""
    tbl = snap["table"]
    if tbl is None:
        raise TableNotFound(f"table {schema}.{table} does not exist")
    if tbl["relkind"] != "r":
        kind = RELKIND_TEXT.get(tbl["relkind"], f"relkind={tbl['relkind']!r}")
        raise UnsupportedRelation(
            f"{schema}.{table} is a {kind}, not a regular table; "
            "this check only assesses regular tables"
        )
    if tbl["relispartition"]:
        raise UnsupportedRelation(
            f"{schema}.{table} is a partition; publications on its ancestor "
            "tables and publish_via_partition_root also apply and are not "
            "evaluated, so no reliable verdict can be given"
        )

    ri = _replica_identity(tbl, snap)
    columns = snap["columns"]
    memberships = snap["table_memberships"]
    schema_pubids = snap["schema_pubids"]

    publications = []
    for pub in snap["publications"]:
        coverage: list[str] = []
        if pub["puballtables"]:
            coverage.append("all_tables")
        if pub["oid"] in schema_pubids:
            coverage.append("schema")
        member = memberships.get(pub["oid"])
        if member is not None:
            coverage.append("table")
        if not coverage:
            continue
        publications.append(_assess_publication(pub, coverage, member, ri, columns))

    notes: list[str] = []
    if not publications:
        notes.append(
            "no publication covers this table; UPDATE/DELETE run normally "
            "but are not replicated anywhere"
        )
    return {
        "table_schema": schema,
        "table": table,
        "table_oid": tbl["oid"],
        "replica_identity": ri,
        "publications": publications,
        "notes": notes,
        "assessment_scope": (
            "publisher-side catalog assessment only: whether PostgreSQL 16 "
            "would emit UPDATE/DELETE for this table via each publication. "
            "It says nothing about any subscription or subscriber actually "
            "receiving or applying changes."
        ),
        "catalog_snapshot": (
            "all catalog rows were read in a single REPEATABLE READ READ "
            "ONLY transaction on a dedicated short-lived connection; the "
            "two pooled diagnostic connections were not used"
        ),
    }


def check_table(schema: str, table: str, dsn: str | None = None) -> dict:
    """Fetch the catalog snapshot and build the assessment for it."""
    return build_assessment(schema, table, fetch_snapshot(schema, table, dsn))
