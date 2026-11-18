"""Integration tests for GET /logical-replication/check/{schema}/{table}.

Require a running API (API_BASE) backed by PostgreSQL 16.  All DDL/DML is
submitted through the diagnostic /jobs endpoint itself; the check endpoint
is then compared against PostgreSQL's real behavior (ground truth): when
the assessment says "blocked", the actual UPDATE/DELETE must fail on the
publisher, and when it says "ok", it must succeed.

Run:
    API_BASE=http://127.0.0.1:8000 pytest tests/test_logical_replication.py -v
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")


def _api_up() -> bool:
    try:
        return httpx.get(f"{API_BASE}/health", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="API not reachable")

SCHEMA = f"lrchk_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def client():
    with httpx.Client(base_url=API_BASE, timeout=30) as c:
        yield c


# ---------------------------------------------------------------- helpers

def submit(client, statements):
    r = client.post("/jobs", json={"statements": statements})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def get_job(client, job_id):
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


def run_sql(client, statements, timeout=40.0):
    """Submit statements and wait for the job to finish."""
    job_id = submit(client, statements)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(client, job_id)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.15)
    raise AssertionError(f"job {job_id} did not finish: {get_job(client, job_id)}")


def wait_pool_idle(client, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = client.get("/pool").json()
        if p["queue_depth"] == 0 and all(
            c["state"] == "idle" for c in p["connections"]
        ):
            return p
        time.sleep(0.2)
    raise AssertionError("pool did not become idle")


def check(client, table, schema=None, expect=200):
    r = client.get(f"/logical-replication/check/{schema or SCHEMA}/{table}")
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def pubs_by_name(result):
    return {p["publication"]: p for p in result["publications"]}


@pytest.fixture(scope="module", autouse=True)
def sandbox_schema():
    """Isolated schema for this test module; dropped afterwards."""
    with httpx.Client(base_url=API_BASE, timeout=30) as c:
        job = run_sql(c, [f"CREATE SCHEMA {SCHEMA}"])
        assert job["status"] == "succeeded", job
        yield
        pubs = run_sql(
            c,
            [f"SELECT pubname FROM pg_publication WHERE pubname LIKE '{SCHEMA}%'"],
        )
        for row in pubs["statements"][0]["rows"] or []:
            run_sql(c, [f"DROP PUBLICATION IF EXISTS {row[0]}"])
        run_sql(c, [f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"])


@pytest.fixture(autouse=True)
def _drop_test_publications(client):
    """Publications (incl. FOR ALL TABLES / FOR TABLES IN SCHEMA ones) must
    not leak into the next test."""
    yield
    pubs = run_sql(
        client,
        [f"SELECT pubname FROM pg_publication WHERE pubname LIKE '{SCHEMA}%'"],
    )
    for row in pubs["statements"][0]["rows"] or []:
        job = run_sql(client, [f"DROP PUBLICATION IF EXISTS {row[0]}"])
        assert job["status"] == "succeeded", job


def make_table(client, name, ddl):
    job = run_sql(client, [f"CREATE TABLE {SCHEMA}.{name} {ddl}"])
    assert job["status"] == "succeeded", job


def make_pub(client, name, definition):
    job = run_sql(client, [f"CREATE PUBLICATION {SCHEMA}_{name} {definition}"])
    assert job["status"] == "succeeded", job
    return f"{SCHEMA}_{name}"


def ground_truth_update(client, table):
    """Run a real UPDATE and a real DELETE; return (update_err, delete_err)."""
    job = run_sql(client, [f"UPDATE {SCHEMA}.{table} SET lr_x = lr_x"])
    upd_err = job["statements"][0]["error"]
    job = run_sql(client, [f"DELETE FROM {SCHEMA}.{table}"])
    del_err = job["statements"][0]["error"]
    return upd_err, del_err


# ---------------------------------------------------------------- tests

class TestHappyPath:
    def test_pk_table_plain_publication_ok(self, client):
        make_table(client, "t_ok", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_ok", f"FOR TABLE {SCHEMA}.t_ok")
        out = check(client, "t_ok")
        assert out["table_schema"] == SCHEMA
        assert out["table"] == "t_ok"
        ri = out["replica_identity"]
        assert ri["mode"] == "default" and ri["status"] == "ok"
        assert ri["key_columns"] == ["id"]
        p = pubs_by_name(out)[f"{SCHEMA}_p_ok"]
        assert p["coverage"] == ["table"]
        assert p["update"]["verdict"] == "ok"
        assert p["delete"]["verdict"] == "ok"
        # ground truth: UPDATE/DELETE really run on the publisher
        assert ground_truth_update(client, "t_ok") == (None, None)

    def test_insert_only_publication_disabled(self, client):
        make_table(client, "t_ins", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_ins", f"FOR TABLE {SCHEMA}.t_ins WITH (publish = 'insert')")
        out = check(client, "t_ins")
        p = pubs_by_name(out)[f"{SCHEMA}_p_ins"]
        assert p["update"]["verdict"] == "disabled"
        assert p["delete"]["verdict"] == "disabled"
        assert any("pubupdate" in r for r in p["update"]["reasons"])
        # DML itself is fine (the publication simply does not emit it)
        assert ground_truth_update(client, "t_ins") == (None, None)

    def test_full_replica_identity_ok(self, client):
        make_table(client, "t_full", "(lr_x int, y int)")
        job = run_sql(client, [f"ALTER TABLE {SCHEMA}.t_full REPLICA IDENTITY FULL"])
        assert job["status"] == "succeeded"
        make_pub(client, "p_full", f"FOR TABLE {SCHEMA}.t_full")
        out = check(client, "t_full")
        ri = out["replica_identity"]
        assert ri["mode"] == "full" and ri["status"] == "ok"
        p = pubs_by_name(out)[f"{SCHEMA}_p_full"]
        assert p["update"]["verdict"] == "ok"
        assert ground_truth_update(client, "t_full") == (None, None)

    def test_replica_identity_using_index_ok(self, client):
        make_table(client, "t_ri", "(k1 int NOT NULL, k2 int NOT NULL, lr_x int)")
        job = run_sql(
            client,
            [
                f"CREATE UNIQUE INDEX t_ri_key ON {SCHEMA}.t_ri (k1, k2)",
                f"ALTER TABLE {SCHEMA}.t_ri REPLICA IDENTITY USING INDEX t_ri_key",
            ],
        )
        assert job["status"] == "succeeded", job
        make_pub(client, "p_ri", f"FOR TABLE {SCHEMA}.t_ri")
        out = check(client, "t_ri")
        ri = out["replica_identity"]
        assert ri["mode"] == "index" and ri["status"] == "ok"
        assert ri["key_columns"] == ["k1", "k2"]
        p = pubs_by_name(out)[f"{SCHEMA}_p_ri"]
        assert p["update"]["verdict"] == "ok"
        assert ground_truth_update(client, "t_ri") == (None, None)


class TestBlocked:
    def test_no_primary_key_blocked(self, client):
        make_table(client, "t_nopk", "(lr_x int, y int)")
        make_pub(client, "p_nopk", f"FOR TABLE {SCHEMA}.t_nopk")
        out = check(client, "t_nopk")
        assert out["replica_identity"]["status"] == "unusable"
        p = pubs_by_name(out)[f"{SCHEMA}_p_nopk"]
        for op in ("update", "delete"):
            assert p[op]["verdict"] == "blocked"
            assert any("replica identity" in r for r in p[op]["reasons"])
        # ground truth: PostgreSQL rejects both operations
        upd_err, del_err = ground_truth_update(client, "t_nopk")
        assert "does not have a replica identity" in upd_err
        assert "does not have a replica identity" in del_err

    def test_replica_identity_nothing_blocked(self, client):
        make_table(client, "t_nothing", "(id int PRIMARY KEY, lr_x int)")
        job = run_sql(client, [f"ALTER TABLE {SCHEMA}.t_nothing REPLICA IDENTITY NOTHING"])
        assert job["status"] == "succeeded"
        make_pub(client, "p_nothing", f"FOR TABLE {SCHEMA}.t_nothing")
        out = check(client, "t_nothing")
        assert out["replica_identity"]["mode"] == "nothing"
        p = pubs_by_name(out)[f"{SCHEMA}_p_nothing"]
        assert p["update"]["verdict"] == "blocked"
        upd_err, del_err = ground_truth_update(client, "t_nothing")
        assert "does not have a replica identity" in upd_err

    def test_column_list_missing_key_blocked(self, client):
        make_table(client, "t_cols", "(id int PRIMARY KEY, lr_x int, w text)")
        make_pub(client, "p_cols", f"FOR TABLE {SCHEMA}.t_cols (lr_x, w)")
        out = check(client, "t_cols")
        p = pubs_by_name(out)[f"{SCHEMA}_p_cols"]
        assert p["column_list"] == ["lr_x", "w"]
        for op in ("update", "delete"):
            assert p[op]["verdict"] == "blocked"
            assert any("column list" in r and "id" in r for r in p[op]["reasons"])
        upd_err, del_err = ground_truth_update(client, "t_cols")
        assert "does not cover the replica identity" in upd_err
        assert "does not cover the replica identity" in del_err

    def test_column_list_with_key_ok(self, client):
        make_table(client, "t_colsok", "(id int PRIMARY KEY, lr_x int, w text)")
        make_pub(client, "p_colsok", f"FOR TABLE {SCHEMA}.t_colsok (id, lr_x)")
        out = check(client, "t_colsok")
        p = pubs_by_name(out)[f"{SCHEMA}_p_colsok"]
        assert p["column_list"] == ["id", "lr_x"]
        assert p["update"]["verdict"] == "ok"
        assert p["delete"]["verdict"] == "ok"
        assert ground_truth_update(client, "t_colsok") == (None, None)

    def test_column_list_with_full_identity_blocked(self, client):
        make_table(client, "t_fcols", "(id int, lr_x int)")
        job = run_sql(client, [f"ALTER TABLE {SCHEMA}.t_fcols REPLICA IDENTITY FULL"])
        assert job["status"] == "succeeded"
        make_pub(client, "p_fcols", f"FOR TABLE {SCHEMA}.t_fcols (id, lr_x)")
        out = check(client, "t_fcols")
        p = pubs_by_name(out)[f"{SCHEMA}_p_fcols"]
        assert p["update"]["verdict"] == "blocked"
        assert any("REPLICA IDENTITY FULL" in r for r in p["update"]["reasons"])
        upd_err, _ = ground_truth_update(client, "t_fcols")
        assert "does not cover the replica identity" in upd_err

    def test_row_filter_on_non_identity_column_blocked(self, client):
        make_table(client, "t_rf", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_rf", f"FOR TABLE {SCHEMA}.t_rf WHERE (lr_x > 10)")
        out = check(client, "t_rf")
        p = pubs_by_name(out)[f"{SCHEMA}_p_rf"]
        assert p["row_filter"] == "(lr_x > 10)"
        for op in ("update", "delete"):
            assert p[op]["verdict"] == "blocked"
            assert any("row filter" in r and "lr_x" in r for r in p[op]["reasons"])
        assert any("only rows matching" in n for n in p["notes"])
        upd_err, del_err = ground_truth_update(client, "t_rf")
        assert "WHERE expression is not part of the replica identity" in upd_err
        assert "WHERE expression is not part of the replica identity" in del_err

    def test_row_filter_on_identity_column_ok(self, client):
        make_table(client, "t_rfok", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_rfok", f"FOR TABLE {SCHEMA}.t_rfok WHERE (id > 5)")
        out = check(client, "t_rfok")
        p = pubs_by_name(out)[f"{SCHEMA}_p_rfok"]
        assert p["update"]["verdict"] == "ok"
        assert p["delete"]["verdict"] == "ok"
        assert any("only rows matching" in n for n in p["notes"])
        assert ground_truth_update(client, "t_rfok") == (None, None)


class TestCoverage:
    def test_schema_and_all_tables_coverage(self, client):
        make_table(client, "t_cov", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_schema", f"FOR TABLES IN SCHEMA {SCHEMA}")
        make_pub(client, "p_all", "FOR ALL TABLES")
        out = check(client, "t_cov")
        by_name = pubs_by_name(out)
        assert by_name[f"{SCHEMA}_p_schema"]["coverage"] == ["schema"]
        assert by_name[f"{SCHEMA}_p_all"]["coverage"] == ["all_tables"]
        assert by_name[f"{SCHEMA}_p_schema"]["update"]["verdict"] == "ok"
        assert by_name[f"{SCHEMA}_p_all"]["update"]["verdict"] == "ok"

    def test_satisfied_publication_does_not_mask_broken_one(self, client):
        make_table(client, "t_mix", "(id int PRIMARY KEY, lr_x int)")
        make_pub(client, "p_good", f"FOR TABLE {SCHEMA}.t_mix")
        make_pub(client, "p_bad", f"FOR TABLE {SCHEMA}.t_mix (lr_x)")
        out = check(client, "t_mix")
        by_name = pubs_by_name(out)
        assert by_name[f"{SCHEMA}_p_good"]["update"]["verdict"] == "ok"
        assert by_name[f"{SCHEMA}_p_bad"]["update"]["verdict"] == "blocked"
        assert by_name[f"{SCHEMA}_p_bad"]["delete"]["verdict"] == "blocked"
        # ground truth: the broken publication makes the UPDATE fail even
        # though another publication is fine
        upd_err, _ = ground_truth_update(client, "t_mix")
        assert "does not cover the replica identity" in upd_err

    def test_no_publication_covers_table(self, client):
        make_table(client, "t_lonely", "(id int PRIMARY KEY, lr_x int)")
        out = check(client, "t_lonely")
        assert out["publications"] == []
        assert any("no publication covers" in n for n in out["notes"])

    def test_scope_and_snapshot_disclaimers(self, client):
        make_table(client, "t_scope", "(id int PRIMARY KEY, lr_x int)")
        out = check(client, "t_scope")
        assert "publisher-side" in out["assessment_scope"]
        assert "subscriber" in out["assessment_scope"]
        assert "REPEATABLE READ" in out["catalog_snapshot"]


class TestErrorsAndIsolation:
    def test_table_not_found_404(self, client):
        r = check(client, "no_such_table_xyz", expect=404)
        assert "does not exist" in r.json()["detail"]

    def test_not_a_regular_table_422(self, client):
        job = run_sql(client, [f"CREATE VIEW {SCHEMA}.v_check AS SELECT 1 AS lr_x"])
        assert job["status"] == "succeeded"
        r = check(client, "v_check", expect=422)
        assert "not a regular table" in r.json()["detail"]

    def test_check_does_not_use_pooled_connections(self, client):
        make_table(client, "t_pool", "(id int PRIMARY KEY, lr_x int)")
        wait_pool_idle(client)  # jobs_executed settled after cleanup
        before = client.get("/pool").json()
        out = check(client, "t_pool")
        assert out["table"] == "t_pool"
        after = client.get("/pool").json()
        executed_before = sum(c["jobs_executed"] for c in before["connections"])
        executed_after = sum(c["jobs_executed"] for c in after["connections"])
        assert executed_after == executed_before
        assert all(c["state"] == "idle" for c in after["connections"])

    def test_endpoint_listed_in_openapi(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        assert "/logical-replication/check/{schema}/{table}" in r.json()["paths"]
