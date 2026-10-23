"""Explicit public identities, exact-revision approvals and isolated media."""
from alembic import op
import sqlalchemy as sa

revision = "0006_public_blogs"
down_revision = "0005_content"
branch_labels = None
depends_on = None


def timestamp(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def owner(name, table, *, primary_key=False, nullable=False):
    return sa.Column(name, sa.String(36), sa.ForeignKey(table + ".id", ondelete="CASCADE"),
                     primary_key=primary_key, nullable=nullable)


def upgrade():
    op.create_table(
        "public_blogs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        owner("owner_user_id", "users", nullable=True),
        owner("pair_id", "pairs", nullable=True),
        owner("created_by_user_id", "users"),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("creation_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        timestamp("created_at"), timestamp("updated_at"),
        sa.UniqueConstraint("owner_user_id"), sa.UniqueConstraint("pair_id"),
        sa.UniqueConstraint("created_by_user_id", "client_id", name="uq_public_blog_create"),
        sa.CheckConstraint("(kind = 'individual' AND owner_user_id IS NOT NULL AND pair_id IS NULL) OR "
                           "(kind = 'couple' AND owner_user_id IS NULL AND pair_id IS NOT NULL)",
                           name="ck_public_blog_owner"),
    )
    op.create_index("ix_public_blogs_status", "public_blogs", ["status"])
    op.create_table(
        "public_blog_consents",
        owner("blog_id", "public_blogs", primary_key=True),
        owner("user_id", "users", primary_key=True),
        sa.Column("blog_revision", sa.Integer(), nullable=False),
        sa.Column("consent_version", sa.String(32), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False), timestamp("updated_at"),
    )
    op.create_table(
        "blog_media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        owner("blog_id", "public_blogs"), owner("uploader_id", "users"),
        sa.Column("client_upload_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(32), nullable=False, unique=True),
        sa.Column("output_key", sa.String(32), nullable=True, unique=True),
        sa.Column("thumbnail_key", sa.String(32), nullable=True, unique=True),
        sa.Column("source_bytes", sa.BigInteger(), nullable=False),
        sa.Column("output_bytes", sa.BigInteger(), nullable=False),
        sa.Column("thumbnail_bytes", sa.Integer(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("mime", sa.String(32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        timestamp("created_at"), timestamp("updated_at"),
        sa.Column("lease_token", sa.String(36), nullable=True), timestamp("lease_until", nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.UniqueConstraint("blog_id", "uploader_id", "client_upload_id", name="uq_blog_media_upload"),
    )
    for name in ("blog_id", "uploader_id", "status", "lease_until"):
        op.create_index("ix_blog_media_assets_" + name, "blog_media_assets", [name])
    op.create_table(
        "public_posts",
        sa.Column("id", sa.String(36), primary_key=True),
        owner("blog_id", "public_blogs"), owner("created_by_user_id", "users"),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("creation_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("topics", sa.Text(), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        owner("media_id", "blog_media_assets"),
        sa.Column("media_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reviewed_revision", sa.Integer(), nullable=True),
        sa.Column("reviewed_media_sha256", sa.String(64), nullable=True),
        timestamp("reviewed_at", nullable=True), sa.Column("review_note", sa.Text(), nullable=True),
        timestamp("published_at", nullable=True), timestamp("created_at"), timestamp("updated_at"),
        sa.UniqueConstraint("blog_id", "client_id", name="uq_public_post_create"),
    )
    for name in ("blog_id", "media_id", "status", "published_at"):
        op.create_index("ix_public_posts_" + name, "public_posts", [name])
    op.create_table(
        "public_post_approvals",
        owner("post_id", "public_posts", primary_key=True), owner("user_id", "users", primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("blog_revision", sa.Integer(), nullable=False),
        sa.Column("media_sha256", sa.String(64), nullable=False),
        sa.Column("consent_version", sa.String(32), nullable=False), timestamp("approved_at"),
    )
    op.create_table(
        "public_post_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        owner("post_id", "public_posts"), owner("user_id", "users"),
        sa.Column("reason", sa.String(16), nullable=False), timestamp("created_at"),
        sa.UniqueConstraint("post_id", "user_id", name="uq_public_post_report"),
    )
    op.create_index("ix_public_post_reports_post_id", "public_post_reports", ["post_id"])


def downgrade():
    raise RuntimeError("Public-blog downgrade would discard consent and publication history; use a compatible recovery build or verified backup")
