"""phase4_part1_crm_otp_and_excel_identification

Revision ID: e4f5a6b7c8d9
Revises: 9d7c5a7b4822
Create Date: 2026-11-23 10:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "9d7c5a7b4822"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema for Phase 4 Part 1 (CRM OTP, identification state, negative-price returns)."""
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("redemption_otp_code", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("redemption_rule_code", sa.String(length=64), nullable=True))

    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "identification_status",
                sa.Enum(
                    "IDENTIFICATION_PENDING",
                    "IDENTIFICATION_CONFIRMED",
                    name="customeridentificationstatus",
                ),
                server_default=sa.text("'IDENTIFICATION_CONFIRMED'"),
                nullable=False,
            )
        )

    with op.batch_alter_table("excel_sales", schema=None) as batch_op:
        batch_op.add_column(sa.Column("customer_phone", sa.String(length=20), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_excel_sales_customer_phone"),
            ["customer_phone"],
            unique=False,
        )

    # Backfill historical returns encoded with negative prices
    op.execute(
        sa.text(
            "UPDATE excel_sales SET is_exchange = 1 "
            "WHERE (selling_price < 0 OR starting_price < 0) AND is_exchange = 0"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("excel_sales", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_excel_sales_customer_phone"))
        batch_op.drop_column("customer_phone")

    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_column("identification_status")

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_column("redemption_rule_code")
        batch_op.drop_column("redemption_otp_code")
