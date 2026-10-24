"""add_users_and_chat_user_id

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-10-24 11:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('hashed_password', sa.String(), nullable=False),
        sa.Column('role', sa.String(length=16), server_default='base', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('token_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_users_id', 'users', ['id'], unique=False)
    op.create_index('ix_users_username', 'users', ['username'], unique=True)

    # 2. Add user_id column to sales_chat_sessions
    with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('user_id', sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            'fk_sales_chat_sessions_user_id_users',
            'users',
            ['user_id'],
            ['id']
        )
        batch_op.create_index('ix_sales_chat_sessions_user_id', ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
        batch_op.drop_index('ix_sales_chat_sessions_user_id')
        batch_op.drop_constraint('fk_sales_chat_sessions_user_id_users', type_='foreignkey')
        batch_op.drop_column('user_id')

    op.drop_index('ix_users_username', table_name='users')
    op.drop_index('ix_users_id', table_name='users')
    op.drop_table('users')
