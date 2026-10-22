"""Encrypted publication preferences and states; existing videos and couples remain unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0005_content"
down_revision = "0004_videos"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "content_preferences",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "shared_content_preferences",
        sa.Column("pair_id", sa.String(36), sa.ForeignKey("pairs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "content_states",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content_id", sa.String(64), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Content downgrade would discard private choices; use a compatible recovery build or verified backup")
