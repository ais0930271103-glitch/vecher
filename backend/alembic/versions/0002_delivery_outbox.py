"""Durable, encrypted delivery intents and per-device acknowledgements."""
from alembic import op
import sqlalchemy as sa

revision = "0002_outbox"
down_revision = "0001_legacy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pair_id", sa.String(36), sa.ForeignKey("pairs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("dedupe_key", sa.String(64), unique=True, nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("force", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delivery_outbox_user_id", "delivery_outbox", ["user_id"])
    op.create_index("ix_delivery_outbox_pair_id", "delivery_outbox", ["pair_id"])
    op.create_index("ix_delivery_outbox_due", "delivery_outbox", ["state", "available_at", "lease_until"])
    op.create_table(
        "delivery_receipts",
        sa.Column("outbox_id", sa.String(36), sa.ForeignKey("delivery_outbox.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("token_fingerprint", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Delivery outbox downgrade would discard pending notifications; restore a verified backup explicitly")
