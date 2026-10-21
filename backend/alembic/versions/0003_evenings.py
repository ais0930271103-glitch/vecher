"""Tracked evening occurrences and private, optional feedback."""
from alembic import op
import sqlalchemy as sa

revision = "0003_evenings"
down_revision = "0002_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evenings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pair_id", sa.String(36), sa.ForeignKey("pairs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active_pair_id", sa.String(36), sa.ForeignKey("pairs.id", ondelete="CASCADE"), unique=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("card_id", sa.Integer(), sa.ForeignKey("wish_cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_by", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("option_index", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("progress", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("pair_id", "client_id", name="uq_evenings_pair_client"),
    )
    op.create_index("ix_evenings_history", "evenings", ["pair_id", "started_at", "id"])
    op.create_table(
        "evening_feedback",
        sa.Column("evening_id", sa.String(36), sa.ForeignKey("evenings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Evening downgrade would discard shared history; use a compatible repair build or a verified backup")
