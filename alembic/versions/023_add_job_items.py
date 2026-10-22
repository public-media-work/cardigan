"""Add job_items and item_pushes — one output of one job (#426, from #329).

Revision ID: 023
Revises: 022
Create Date: 2026-08-14 (rewritten 2026-10-21 for #426, before ever shipping)

An item is one reviewable output of a job. Items become authoritative on
extraction; the ``*_output.md`` phase documents stay on disk as frozen
provenance and are never rewritten by an edit or a review.

job_items column notes
----------------------
key                Mirrors the SST field (``short_description``). UNIQUE with
                   job_id. Regeneration updates in place — no per-item version
                   history.
origin             'deterministic' | 'inferred'. If a model touched the value
                   it is inferred. A hand edit never changes it.
generation_state   'ready' | 'failed' | 'unavailable' — the system's axis,
generation_reason  with a reason whenever it isn't ready. A ready item with
                   reason 'no_change_needed' and no proposed_value is an
                   affirmation: the generator kept the basis as written.
review_state       'proposed' | 'accepted' | 'dismissed' — the editor's axis.
                   Written on insert only; re-extraction never touches it.
basis_value        The target's value when the item was generated. A non-null
                   basis makes the proposal a suggestion (rendered as a diff);
                   the push guard compares the live value against it.
current_state      'empty' | 'unreviewed' | 'reviewed' | 'flagged' | 'unknown'.
                   Read-only context derived from the field and the record's
                   ``Single Source Status``.
proposed_value     Always the full text, never a patch.
edited_value       The editor's hand edit. Push/copy use it when present.
                   Written on insert only, like review_state.
flags              JSON array of {check, rule_id, severity, message,
                   model_fixable, origin, target}. origin is model | code;
                   target is basis | proposal | alternative. Advisory;
                   nothing blocks.
alternatives       JSON array of {value, angle, flags} — other candidates.
                   Choosing one copies it into edited_value; proposed_value
                   stays what the generator said.
generator, model,  Provenance and spend. The item records spend; caps belong
*_tokens, cost_usd to the generator contract (#427).

item_pushes holds one row per (item, target) — the push record. It stays
empty until the push endpoint exists (#428).

This supersedes ``jobs.validation_result`` (migration 013) for QA purposes.
That column is left in place — dropping it is a separate decision.
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
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("generation_state", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("generation_reason", sa.Text(), nullable=True),
        sa.Column("review_state", sa.Text(), nullable=False, server_default="proposed"),
        sa.Column("basis_value", sa.Text(), nullable=True),
        sa.Column("basis_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("current_state", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("edited_value", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("flags", sa.Text(), nullable=True),
        sa.Column("alternatives", sa.Text(), nullable=True),
        sa.Column("char_limit", sa.Integer(), nullable=True),
        sa.Column("generator", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("job_id", "key", name="uq_job_items_job_key"),
    )
    op.create_index("idx_job_items_job", "job_items", ["job_id"])
    op.create_index("idx_job_items_review", "job_items", ["job_id", "review_state"])

    op.create_table(
        "item_pushes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("job_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("target_record_id", sa.Text(), nullable=True),
        sa.Column("target_field_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="not_pushed"),
        sa.Column("pushed_value", sa.Text(), nullable=True),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.Column("pushed_by", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("item_id", "target", name="uq_item_pushes_item_target"),
    )
    op.create_index("idx_item_pushes_item", "item_pushes", ["item_id"])


def downgrade() -> None:
    op.drop_index("idx_item_pushes_item", table_name="item_pushes")
    op.drop_table("item_pushes")
    op.drop_index("idx_job_items_review", table_name="job_items")
    op.drop_index("idx_job_items_job", table_name="job_items")
    op.drop_table("job_items")
