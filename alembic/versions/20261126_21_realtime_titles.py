"""Single-use WebSocket tickets and durable Gemini title jobs."""

from alembic import op
import sqlalchemy as sa

revision = "20261126_21"
down_revision = "20261126_20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_chats", sa.Column("title_status", sa.String(16), nullable=False, server_default="pending"))
    op.add_column("platform_chats", sa.Column("title_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("platform_chats", sa.Column("title_attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_platform_chats_title_status", "platform_chats", ["title_status"])
    op.create_table(
        "platform_socket_tickets",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("platform_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_platform_socket_tickets_session_id", "platform_socket_tickets", ["session_id"])
    op.create_index("ix_platform_socket_tickets_expires_at", "platform_socket_tickets", ["expires_at"])
    op.execute("ALTER TABLE platform_socket_tickets ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.drop_table("platform_socket_tickets")
    op.drop_index("ix_platform_chats_title_status", table_name="platform_chats")
    for column in ("title_attempted_at", "title_attempts", "title_status"):
        op.drop_column("platform_chats", column)
