"""Add job_items — the editor-facing item primitive (issue #329).

Revision ID: 023
Revises: 022
Create Date: 2026-08-14

An item is one reviewable unit of a job's output: proposed value, current
Airtable value, status. Items become authoritative on extraction; the
``*_output.md`` phase documents stay on disk as frozen provenance and are
never rewritten by an edit or an approval.

Column notes
------------
key                One per job (UNIQUE with job_id). Regeneration updates in
                   place — no per-item version history, because Airtable's own
                   revision tracking covers published values and worker.py's
                   ``{phase}_output.{timestamp}.prev.md`` snapshots cover
                   superseded proposals.
layer              'context' (never published) | 'deliverable' (1:1 SST field).
current_state      'empty' | 'unreviewed' | 'reviewed' | 'flagged' | 'unknown'.
                   Derived from field-emptiness x the record's
                   ``Single Source Status (BETA)`` position. 'unknown' is the
                   honest default while media_id is null on most jobs (#331).
status             'pending_review' | 'approved' | 'kicked_back' |
                   'awaiting_source'. No blocking state: flags are advisory
                   and an item with errors is still approvable.
source_blocked_on  Disambiguates 'awaiting_source': 'integration:<name>' means
                   no system to ask; 'structured_output:<phase>' means the
                   agent answers but not in a parseable shape.
flags              JSON array of {rule_id, severity, message, model_fixable},
                   carried over from the deterministic style engine.

This supersedes ``jobs.validation_result`` (migration 013) for QA purposes.
That column is left in place — dropping it is the publish-path ticket's call,
not this migration's.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("current_state", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("current_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("source_blocked_on", sa.Text(), nullable=True),
        sa.Column("flags", sa.Text(), nullable=True),
        sa.Column("kickback_note", sa.Text(), nullable=True),
        sa.Column("kicked_back_at", sa.DateTime(), nullable=True),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("char_limit", sa.Integer(), nullable=True),
        sa.Column("airtable_field", sa.Text(), nullable=True),
        sa.Column("airtable_field_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("job_id", "key", name="uq_job_items_job_key"),
    )

    op.create_index("idx_job_items_job", "job_items", ["job_id"])
    op.create_index("idx_job_items_status", "job_items", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_job_items_status", table_name="job_items")
    op.drop_index("idx_job_items_job", table_name="job_items")
    op.drop_table("job_items")
