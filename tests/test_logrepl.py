"""Unit tests for the publisher-side UPDATE/DELETE assessment logic.

These exercise app.logrepl.build_assessment over fabricated catalog
snapshots -- no database needed.  The verdict rules mirror PostgreSQL 16's
CheckCmdReplicaIdentity (verified against a live PostgreSQL 16 server).

Run:  pytest tests/test_logrepl.py
"""
from __future__ import annotations

import pytest

from app.logrepl import (
    TableNotFound,
    UnsupportedRelation,
    _row_filter_attnums,
    build_assessment,
)


# ---------------------------------------------------------------- helpers

def make_snap(
    *,
    relkind="r",
    relispartition=False,
    relreplident="d",
    pk_columns=(),
    ri_index=None,
    column_names=("id", "v"),
    publications=(),
    memberships=None,
    schema_pubids=(),
):
    return {
        "table": {
            "oid": 100,
            "relkind": relkind,
            "relispartition": relispartition,
            "relreplident": relreplident,
        },
        "pk_columns": list(pk_columns),
        "ri_index": ri_index,
        "columns": {i + 1: n for i, n in enumerate(column_names)},
        "publications": list(publications),
        "table_memberships": memberships or {},
        "schema_pubids": set(schema_pubids),
    }


def pub(oid, name, *, all_tables=False, insert=True, update=True, delete=True,
        truncate=True, viaroot=False):
    return {
        "oid": oid,
        "pubname": name,
        "puballtables": all_tables,
        "pubinsert": insert,
        "pubupdate": update,
        "pubdelete": delete,
        "pubtruncate": truncate,
        "pubviaroot": viaroot,
    }


def membership(*, row_filter=None, row_filter_node=None, column_attnums=None):
    return {
        "row_filter": row_filter,
        "row_filter_node": row_filter_node,
        "column_attnums": column_attnums,
    }


def var(attnum):
    """A {VAR ...} node fragment as serialized by PostgreSQL 16."""
    return (
        "{VAR :varno 1 :varattno %d :vartype 23 :vartypmod -1 :varcollid 0 "
        ":varnullingrels (b) :varlevelsup 0 :varnosyn 1 :varattnosyn %d "
        ":location 42}" % (attnum, attnum)
    )


def opexpr(*args):
    return "{OPEXPR :opno 96 :opfuncid 65 :opresulttype 16 :opretset false " \
           ":opcollid 0 :inputcollid 23 :args (%s) :location -1}" % " ".join(args)


def const():
    return "{CONST :consttype 23 :consttypmod -1 :constcollid 0 :constlen 4 " \
           ":constbyval true :constisnull false :location 8 :constvalue 4 [ 10 0 0 0 0 0 0 0 ]}"


# ---------------------------------------------------------------- tests

class TestRelationKinds:
    def test_table_not_found(self):
        with pytest.raises(TableNotFound):
            build_assessment("public", "nope", {"table": None})

    def test_partitioned_table_rejected(self):
        snap = make_snap(relkind="p")
        with pytest.raises(UnsupportedRelation, match="partitioned table"):
            build_assessment("public", "t", snap)

    def test_view_rejected(self):
        snap = make_snap(relkind="v")
        with pytest.raises(UnsupportedRelation, match="view"):
            build_assessment("public", "t", snap)

    def test_partition_rejected(self):
        snap = make_snap(relispartition=True)
        with pytest.raises(UnsupportedRelation, match="partition"):
            build_assessment("public", "t", snap)


class TestReplicaIdentity:
    def test_default_with_pk_ok(self):
        snap = make_snap(relreplident="d", pk_columns=["id"],
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        ri = out["replica_identity"]
        assert ri["mode"] == "default" and ri["status"] == "ok"
        assert ri["key_columns"] == ["id"]
        assert out["publications"][0]["update"]["verdict"] == "ok"
        assert out["publications"][0]["delete"]["verdict"] == "ok"

    def test_default_without_pk_blocked(self):
        snap = make_snap(relreplident="d", pk_columns=[],
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        assert out["replica_identity"]["status"] == "unusable"
        for op in ("update", "delete"):
            v = out["publications"][0][op]
            assert v["verdict"] == "blocked"
            assert any("replica identity" in r for r in v["reasons"])

    def test_nothing_blocked(self):
        snap = make_snap(relreplident="n",
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        assert out["replica_identity"]["status"] == "unusable"
        assert out["publications"][0]["update"]["verdict"] == "blocked"

    def test_full_ok(self):
        snap = make_snap(relreplident="f",
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        ri = out["replica_identity"]
        assert ri["mode"] == "full" and ri["status"] == "ok"
        assert ri["key_columns"] is None
        assert out["publications"][0]["update"]["verdict"] == "ok"

    def test_index_ok(self):
        idx = {"name": "t_ri", "indisvalid": True, "columns": ["id"]}
        snap = make_snap(relreplident="i", ri_index=idx,
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        assert out["replica_identity"]["status"] == "ok"
        assert out["replica_identity"]["key_columns"] == ["id"]
        assert out["publications"][0]["delete"]["verdict"] == "ok"

    def test_index_missing_blocked(self):
        # relreplident='i' but the index was dropped: PostgreSQL treats the
        # table as having no replica identity.
        snap = make_snap(relreplident="i", ri_index=None,
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        assert out["replica_identity"]["status"] == "unusable"
        assert out["publications"][0]["update"]["verdict"] == "blocked"

    def test_index_invalid_unknown(self):
        idx = {"name": "t_ri", "indisvalid": False, "columns": ["id"]}
        snap = make_snap(relreplident="i", ri_index=idx,
                         publications=[pub(1, "p1")], memberships={1: membership()})
        out = build_assessment("public", "t", snap)
        assert out["replica_identity"]["status"] == "unknown"
        v = out["publications"][0]["update"]
        assert v["verdict"] == "unknown"
        assert v["reasons"]


class TestOperationSwitches:
    def test_insert_only_publication_disabled(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"],
            publications=[pub(1, "p_ins", update=False, delete=False)],
            memberships={1: membership()},
        )
        out = build_assessment("public", "t", snap)
        p = out["publications"][0]
        assert p["publish_update"] is False and p["publish_delete"] is False
        for op in ("update", "delete"):
            assert p[op]["verdict"] == "disabled"
            assert any("pub" + op in r for r in p[op]["reasons"])

    def test_delete_only_publication(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"],
            publications=[pub(1, "p_del", update=False, delete=True)],
            memberships={1: membership()},
        )
        out = build_assessment("public", "t", snap)
        p = out["publications"][0]
        assert p["update"]["verdict"] == "disabled"
        assert p["delete"]["verdict"] == "ok"


class TestColumnList:
    def test_column_list_covering_key_ok(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"], column_names=("id", "v", "w"),
            publications=[pub(1, "p1")],
            memberships={1: membership(column_attnums=[1, 2])},
        )
        out = build_assessment("public", "t", snap)
        p = out["publications"][0]
        assert p["column_list"] == ["id", "v"]
        assert p["update"]["verdict"] == "ok"
        assert p["delete"]["verdict"] == "ok"
        assert any("column list" in n for n in p["notes"])

    def test_column_list_missing_key_blocked(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"], column_names=("id", "v", "w"),
            publications=[pub(1, "p1")],
            memberships={1: membership(column_attnums=[2, 3])},
        )
        out = build_assessment("public", "t", snap)
        for op in ("update", "delete"):
            v = out["publications"][0][op]
            assert v["verdict"] == "blocked"
            assert any("id" in r and "column list" in r for r in v["reasons"])

    def test_column_list_with_full_identity_blocked(self):
        # PostgreSQL 16: a column list is never allowed with REPLICA
        # IDENTITY FULL -- even one listing every column.
        snap = make_snap(
            relreplident="f", column_names=("id", "v"),
            publications=[pub(1, "p1")],
            memberships={1: membership(column_attnums=[1, 2])},
        )
        out = build_assessment("public", "t", snap)
        for op in ("update", "delete"):
            v = out["publications"][0][op]
            assert v["verdict"] == "blocked"
            assert any("REPLICA IDENTITY FULL" in r for r in v["reasons"])

    def test_column_list_with_index_identity(self):
        idx = {"name": "t_ri", "indisvalid": True, "columns": ["id", "v"]}
        snap = make_snap(
            relreplident="i", ri_index=idx, column_names=("id", "v", "w"),
            publications=[pub(1, "p1")],
            memberships={1: membership(column_attnums=[1, 3])},
        )
        out = build_assessment("public", "t", snap)
        v = out["publications"][0]["update"]
        assert v["verdict"] == "blocked"
        assert any("v" in r for r in v["reasons"])


class TestRowFilter:
    def test_row_filter_on_non_identity_column_blocked(self):
        node = opexpr(var(2), const())  # WHERE (v > 10); identity is id
        snap = make_snap(
            relreplident="d", pk_columns=["id"], column_names=("id", "v"),
            publications=[pub(1, "p1")],
            memberships={1: membership(row_filter="(v > 10)", row_filter_node=node)},
        )
        out = build_assessment("public", "t", snap)
        p = out["publications"][0]
        assert p["row_filter"] == "(v > 10)"
        for op in ("update", "delete"):
            v = p[op]
            assert v["verdict"] == "blocked"
            assert any("row filter" in r and "v" in r for r in v["reasons"])
        assert any("only rows matching" in n for n in p["notes"])

    def test_row_filter_on_identity_column_ok(self):
        node = opexpr(var(1), const())  # WHERE (id > 10)
        snap = make_snap(
            relreplident="d", pk_columns=["id"], column_names=("id", "v"),
            publications=[pub(1, "p1")],
            memberships={1: membership(row_filter="(id > 10)", row_filter_node=node)},
        )
        out = build_assessment("public", "t", snap)
        p = out["publications"][0]
        assert p["update"]["verdict"] == "ok"
        assert p["delete"]["verdict"] == "ok"
        assert any("only rows matching" in n for n in p["notes"])

    def test_row_filter_with_full_identity_ok(self):
        # the row-filter column check is skipped for REPLICA IDENTITY FULL
        node = opexpr(var(2), const())
        snap = make_snap(
            relreplident="f", column_names=("id", "v"),
            publications=[pub(1, "p1")],
            memberships={1: membership(row_filter="(v > 10)", row_filter_node=node)},
        )
        out = build_assessment("public", "t", snap)
        assert out["publications"][0]["update"]["verdict"] == "ok"

    def test_row_filter_with_nothing_identity_blocked(self):
        # empty identity bitmap: any referenced column is invalid
        node = opexpr(var(1), const())
        snap = make_snap(
            relreplident="n", column_names=("id", "v"),
            publications=[pub(1, "p1")],
            memberships={1: membership(row_filter="(id > 10)", row_filter_node=node)},
        )
        out = build_assessment("public", "t", snap)
        reasons = out["publications"][0]["update"]["reasons"]
        assert any("row filter" in r for r in reasons)
        assert any("replica identity" in r for r in reasons)


class TestCoverageAndIndependence:
    def test_all_three_coverage_kinds(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"],
            publications=[
                pub(1, "p_table"),
                pub(2, "p_schema"),
                pub(3, "p_all", all_tables=True),
                pub(4, "p_elsewhere"),  # does not cover the table
            ],
            memberships={1: membership()},
            schema_pubids={2},
        )
        out = build_assessment("public", "t", snap)
        by_name = {p["publication"]: p for p in out["publications"]}
        assert set(by_name) == {"p_table", "p_schema", "p_all"}
        assert by_name["p_table"]["coverage"] == ["table"]
        assert by_name["p_schema"]["coverage"] == ["schema"]
        assert by_name["p_all"]["coverage"] == ["all_tables"]

    def test_table_and_schema_coverage_combined(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"],
            publications=[pub(1, "p_both")],
            memberships={1: membership()},
            schema_pubids={1},
        )
        out = build_assessment("public", "t", snap)
        assert out["publications"][0]["coverage"] == ["schema", "table"]

    def test_satisfied_publication_does_not_mask_broken_one(self):
        snap = make_snap(
            relreplident="d", pk_columns=["id"], column_names=("id", "v"),
            publications=[
                pub(1, "p_good"),
                pub(2, "p_bad_collist"),
            ],
            memberships={
                1: membership(),
                2: membership(column_attnums=[2]),  # misses pk column id
            },
        )
        out = build_assessment("public", "t", snap)
        by_name = {p["publication"]: p for p in out["publications"]}
        assert by_name["p_good"]["update"]["verdict"] == "ok"
        assert by_name["p_bad_collist"]["update"]["verdict"] == "blocked"
        assert by_name["p_bad_collist"]["delete"]["verdict"] == "blocked"

    def test_no_publication_covers_table(self):
        snap = make_snap(relreplident="d", pk_columns=["id"],
                         publications=[pub(1, "p_elsewhere")])
        out = build_assessment("public", "t", snap)
        assert out["publications"] == []
        assert any("no publication covers" in n for n in out["notes"])

    def test_scope_and_snapshot_disclaimers(self):
        snap = make_snap(relreplident="d", pk_columns=["id"])
        out = build_assessment("public", "t", snap)
        assert "publisher-side" in out["assessment_scope"]
        assert "subscriber" in out["assessment_scope"]
        assert "REPEATABLE READ" in out["catalog_snapshot"]


class TestRowFilterAttnums:
    def test_extracts_all_var_attnums(self):
        node = (
            "{BOOLEXPR :boolop and :args ("
            + opexpr(var(2), const())
            + " "
            + opexpr(var(1), const())
            + ") :location -1}"
        )
        assert _row_filter_attnums(node) == {1, 2}

    def test_empty_and_none(self):
        assert _row_filter_attnums(None) == set()
        assert _row_filter_attnums("") == set()

    def test_ignores_const_nodes(self):
        node = opexpr(var(3), const())
        assert _row_filter_attnums(node) == {3}

    def test_real_pg16_node_tree(self):
        # captured from PostgreSQL 16.16:
        # CREATE PUBLICATION ... WHERE (v > 10 AND (w = 'x' OR id < 5))
        node = (
            "{BOOLEXPR :boolop and :args ({OPEXPR :opno 521 :opfuncid 147 "
            ":opresulttype 16 :opretset false :opcollid 0 :inputcollid 0 "
            ":args ({VAR :varno 1 :varattno 2 :vartype 23 :vartypmod -1 "
            ":varcollid 0 :varnullingrels (b) :varlevelsup 0 :varnosyn 1 "
            ":varattnosyn 2 :location 42} {CONST :consttype 23 :consttypmod -1 "
            ":constcollid 0 :constlen 4 :constbyval true :constisnull false "
            ":location 46 :constvalue 4 [ 10 0 0 0 0 0 0 0 ]}) :location 44} "
            "{BOOLEXPR :boolop or :args ({OPEXPR :opno 98 :opfuncid 67 "
            ":opresulttype 16 :opretset false :opcollid 0 :inputcollid 100 "
            ":args ({VAR :varno 1 :varattno 3 :vartype 25 :vartypmod -1 "
            ":varcollid 100 :varnullingrels (b) :varlevelsup 0 :varnosyn 1 "
            ":varattnosyn 3 :location 54} {CONST :consttype 25 :consttypmod -1 "
            ":constcollid 100 :constlen -1 :constbyval false :constisnull "
            "false :location 58 :constvalue 5 [ 20 0 0 0 120 ]}) :location 56} "
            "{OPEXPR :opno 97 :opfuncid 66 :opresulttype 16 :opretset false "
            ":opcollid 0 :inputcollid 0 :args ({VAR :varno 1 :varattno 1 "
            ":vartype 23 :vartypmod -1 :varcollid 0 :varnullingrels (b) "
            ":varlevelsup 0 :varnosyn 1 :varattnosyn 1 :location 65} {CONST "
            ":consttype 23 :consttypmod -1 :constcollid 0 :constlen 4 "
            ":constbyval true :constisnull false :location 70 :constvalue 4 "
            "[ 5 0 0 0 0 0 0 0 ]}) :location 68}) :location 62}) :location 49}"
        )
        assert _row_filter_attnums(node) == {1, 2, 3}
