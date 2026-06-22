"""companies: add batch column for YC/VC fund batch tracking

Revision ID: 0024_companies_batch
Revises: 0023_templates_is_default
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_companies_batch"
down_revision = "0023_templates_is_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("batch", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "batch")
