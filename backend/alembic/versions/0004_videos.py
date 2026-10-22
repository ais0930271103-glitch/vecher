"""Private video choices and explicitly shared video topics; existing rows are untouched."""
from alembic import op
import sqlalchemy as sa

revision = "0004_videos"
down_revision = "0003_evenings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "video_preferences",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topics", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "shared_video_preferences",
        sa.Column("pair_id", sa.String(36), sa.ForeignKey("pairs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topics", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "video_states",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("video_id", sa.String(64), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Video downgrade would discard private choices; use a compatible repair build or a verified backup")
