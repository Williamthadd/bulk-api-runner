"""auth profiles (OAuth2 client-credentials token endpoints)

Revision ID: 0002_auth_profiles
Revises: 0001_initial
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0002_auth_profiles"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

DT = mysql.DATETIME(fsp=3)
NOW = sa.text("CURRENT_TIMESTAMP(3)")
TBL = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci", "mysql_engine": "InnoDB"}


def upgrade() -> None:
    op.create_table(
        "auth_profiles",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("client_id", sa.String(500), nullable=False, server_default=""),
        sa.Column("client_secret", sa.Text, nullable=True),          # Fernet-encrypted
        sa.Column("grant_type", sa.String(50), nullable=False, server_default="client_credentials"),
        sa.Column("scope", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", DT, nullable=False, server_default=NOW),
        sa.Column("updated_at", DT, nullable=False, server_default=NOW),
        sa.UniqueConstraint("name", name="uq_auth_profiles_name"),
        **TBL,
    )


def downgrade() -> None:
    op.drop_table("auth_profiles")
