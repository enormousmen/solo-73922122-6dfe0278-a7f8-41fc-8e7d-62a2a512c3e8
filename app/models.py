"""Pydantic request/response models (also drive the /docs schema)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import config


class SubmitOptions(BaseModel):
    cleanup_delay_ms: int | None = Field(
        default=None,
        ge=0,
        le=config.MAX_HOOK_DELAY_MS,
        description="Test hook: pause this many ms inside the post-job "
        "session cleanup (simulates slow cleanup). Defaults to the "
        "server-wide CLEANUP_DELAY_MS.",
    )
    break_connection_after: bool = Field(
        default=False,
        description="Test hook: close this job's connection right after the "
        "job (simulates a connection failure). The subsequent session "
        "cleanup then fails, so the pool retires the connection and builds "
        "a replacement.",
    )


class SubmitRequest(BaseModel):
    statements: list[str] = Field(
        min_length=1,
        description=f"1..{config.MAX_STATEMENTS} SQL statements, executed in "
        "order on one shared connection, each auto-committed. Explicit "
        "transaction control (BEGIN/COMMIT/ROLLBACK/SAVEPOINT/...) is "
        "rejected.",
    )
    options: SubmitOptions = SubmitOptions()


class CancelRequest(BaseModel):
    delay_ms: int | None = Field(
        default=None,
        ge=0,
        le=config.MAX_HOOK_DELAY_MS,
        description="Test hook: wait this many ms before the cancel is "
        "actually sent to PostgreSQL (simulates a slow cancel pipeline). "
        "The cancel itself is accepted immediately. Defaults to the "
        "server-wide CANCEL_SEND_DELAY_MS.",
    )


class StatementResult(BaseModel):
    index: int
    sql: str
    status: str  # pending|running|ok|error|cancelled|skipped
    started_at: str | None
    duration_ms: float | None
    columns: list[str] | None
    rows: list[list] | None
    rowcount: int | None
    truncated: bool
    error: str | None


class JobDetail(BaseModel):
    id: str
    status: str  # queued|running|succeeded|failed|cancelled
    created_at: str
    started_at: str | None
    finished_at: str | None
    connection_id: int | None
    cleanup_delay_ms: int | None
    error: str | None
    cancel_requested: bool
    cancel_accepted_at: str | None
    statements: list[StatementResult]


class JobSummary(BaseModel):
    id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    connection_id: int | None
    num_statements: int


class SubmitResponse(BaseModel):
    job_id: str
    status: str
    queue_depth: int
    detail: str


class CancelResponse(BaseModel):
    job_id: str
    accepted: bool
    job_status: str
    detail: str


class ConnectionStatus(BaseModel):
    id: int
    state: str  # idle|busy|cleaning|reconnecting
    current_job_id: str | None
    generation: int
    jobs_executed: int
    cleanups_failed: int


class PoolStatus(BaseModel):
    size: int
    queue_depth: int
    connections: list[ConnectionStatus]


# --------------------------------------------------------------------------
# logical replication: publisher-side UPDATE/DELETE check
# (see app/logrepl.py; GET /logical-replication/check/{schema}/{table})
# --------------------------------------------------------------------------


class OperationAssessment(BaseModel):
    verdict: str = Field(
        description="ok: the publisher can emit the operation via this "
        "publication | blocked: the publication publishes the operation but "
        "PostgreSQL would reject it on the publisher (see reasons) | "
        "disabled: the publication does not publish this operation | "
        "unknown: cannot be determined (see reasons)"
    )
    reasons: list[str]


class PublicationAssessment(BaseModel):
    publication: str
    coverage: list[str] = Field(
        description="how this publication covers the table: 'table' "
        "(FOR TABLE), 'schema' (FOR TABLES IN SCHEMA) and/or 'all_tables' "
        "(FOR ALL TABLES)"
    )
    publish_update: bool
    publish_delete: bool
    column_list: list[str] | None = Field(
        description="per-table column list of the publication; null means "
        "all columns are published"
    )
    row_filter: str | None = Field(
        description="row filter expression of the per-table membership; "
        "null means all rows are covered"
    )
    update: OperationAssessment
    delete: OperationAssessment
    notes: list[str]


class ReplicaIdentityAssessment(BaseModel):
    mode: str = Field(description="default | nothing | full | index")
    status: str = Field(
        description="ok: usable for UPDATE/DELETE | unusable: PostgreSQL "
        "rejects UPDATE/DELETE on published tables | unknown: cannot be "
        "determined (see detail)"
    )
    key_columns: list[str] | None = Field(
        description="identity key columns; null means 'all columns' "
        "(REPLICA IDENTITY FULL) or undetermined"
    )
    detail: str


class LogicalReplicationCheck(BaseModel):
    table_schema: str = Field(description="schema (namespace) of the checked table")
    table: str
    table_oid: int
    replica_identity: ReplicaIdentityAssessment
    publications: list[PublicationAssessment] = Field(
        description="one entry per publication covering the table; each "
        "publication is assessed independently -- a satisfied publication "
        "never masks the shortcomings of another one"
    )
    notes: list[str]
    assessment_scope: str = Field(
        description="reminder that this is a publisher-side assessment "
        "only; it does not verify that any subscriber received anything"
    )
    catalog_snapshot: str = Field(
        description="how the catalog data was read (single REPEATABLE READ "
        "snapshot on a dedicated connection)"
    )
