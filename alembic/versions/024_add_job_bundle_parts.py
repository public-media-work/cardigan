"""Add job_bundle_parts, item input revisions and a per-job copy mode (#427).

Revision ID: 024
Revises: 023
Create Date: 2026-10-22

The context bundle is assembled once per job and read by every generator.
Each slice is one row, labelled deterministic or inferred like an item.

job_bundle_parts column notes
-----------------------------
slice            'job' | 'captions' | 'sst' | 'house_rules' | 'outline' | ...
                 UNIQUE with job_id.
state, reason    'ready' | 'failed' | 'unavailable', with a reason whenever it
                 isn't ready. A generator that reads a slice that isn't ready
                 is itself unavailable ('slice:<name>').
content          JSON. What the builder produced.
edited_content   JSON. The editor's correction. Readers get it when present;
                 a rebuild never overwrites it.
revision         Bumped whenever the effective content (edited ?? content)
                 changes. Items record the revisions they read, so a
                 correction marks the items built on the old one as stale.
producer, model, Provenance and spend, as on job_items.
*_tokens, cost_usd

job_items.input_revisions  JSON {"slices": {name: revision}, "items": {key:
                           generated_at}} — what the item was built from.
jobs.copy_generation       Per-job override of the instance copy mode:
                           'off' | 'review' | 'suggest', or NULL to inherit.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_bundle_parts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slice", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("edited_content", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("producer", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("built_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("job_id", "slice", name="uq_job_bundle_parts_job_slice"),
    )
    op.create_index("idx_job_bundle_parts_job", "job_bundle_parts", ["job_id"])

    with op.batch_alter_table("job_items") as batch:
        batch.add_column(sa.Column("input_revisions", sa.Text(), nullable=True))

    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("copy_generation", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("copy_generation")

    with op.batch_alter_table("job_items") as batch:
        batch.drop_column("input_revisions")

    op.drop_index("idx_job_bundle_parts_job", table_name="job_bundle_parts")
    op.drop_table("job_bundle_parts")
