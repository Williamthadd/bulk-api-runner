"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

DT = mysql.DATETIME(fsp=3)
NOW = sa.text("CURRENT_TIMESTAMP(3)")
TBL = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci", "mysql_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("auth_config", sa.JSON, nullable=True),
        sa.Column("created_at", DT, nullable=False, server_default=NOW),
        sa.Column("updated_at", DT, nullable=False, server_default=NOW),
        **TBL,
    )
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer, sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("method", sa.String(10), nullable=False, server_default="GET"),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("headers", sa.JSON, nullable=True),
        sa.Column("params", sa.JSON, nullable=True),
        sa.Column("auth_config", sa.JSON, nullable=True),
        sa.Column("data_source", sa.JSON, nullable=True),
        sa.Column("script", mysql.MEDIUMTEXT, nullable=True),
        sa.Column("settings", sa.JSON, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", DT, nullable=False, server_default=NOW),
        sa.Column("updated_at", DT, nullable=False, server_default=NOW),
        sa.UniqueConstraint("folder_id", "name", name="uq_requests_folder_name"),
        **TBL,
    )
    op.create_table(
        "environments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.false()),
        **TBL,
    )
    op.create_table(
        "environment_variables",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("environment_id", sa.Integer, sa.ForeignKey("environments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("is_secret", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("environment_id", "key", name="uq_envvar_env_key"),
        **TBL,
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_name", sa.String(200), nullable=False),
        sa.Column("environment_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", DT, nullable=False, server_default=NOW),
        sa.Column("finished_at", DT, nullable=True),
        **TBL,
    )
    op.create_index("ix_runs_request_started", "runs", ["request_id", "started_at"])
    op.create_table(
        "run_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("label", sa.String(500), nullable=False, server_default=""),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("request_body", mysql.MEDIUMTEXT, nullable=True),
        sa.Column("response_body", mysql.MEDIUMTEXT, nullable=True),
        sa.Column("created_at", DT, nullable=False, server_default=NOW),
        **TBL,
    )
    op.create_index("ix_run_items_run_seq", "run_items", ["run_id", "seq"])
    op.create_table(
        "uploads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id", ondelete="SET NULL"), nullable=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", DT, nullable=False, server_default=NOW),
        **TBL,
    )


def downgrade() -> None:
    # Indexes are dropped together with their tables (MySQL refuses to drop an index backing a FK).
    op.drop_table("uploads")
    op.drop_table("run_items")
    op.drop_table("runs")
    op.drop_table("environment_variables")
    op.drop_table("environments")
    op.drop_table("requests")
    op.drop_table("folders")
