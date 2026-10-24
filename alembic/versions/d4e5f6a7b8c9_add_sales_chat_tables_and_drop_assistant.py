"""add_sales_chat_tables_and_drop_assistant

Revision ID: d4e5f6a7b8c9
Revises: 547d7acb8cc4
Create Date: 2026-10-19 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = '547d7acb8cc4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: drop unused assistant_* tables and create sales_chat_* tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'assistant_traces' in existing_tables:
        with op.batch_alter_table('assistant_traces', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_assistant_traces_id'))
            batch_op.drop_index(batch_op.f('ix_assistant_traces_conversation_id'))
        op.drop_table('assistant_traces')

    if 'assistant_messages' in existing_tables:
        with op.batch_alter_table('assistant_messages', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_assistant_messages_id'))
            batch_op.drop_index(batch_op.f('ix_assistant_messages_conversation_id'))
        op.drop_table('assistant_messages')

    if 'assistant_conversations' in existing_tables:
        with op.batch_alter_table('assistant_conversations', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_assistant_conversations_id'))
        op.drop_table('assistant_conversations')

    if 'sales_chat_sessions' not in existing_tables:
        op.create_table(
            'sales_chat_sessions',
            sa.Column('title', sa.String(length=255), nullable=False),
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_sales_chat_sessions_id'), ['id'], unique=False)

    if 'sales_chat_messages' not in existing_tables:
        op.create_table(
            'sales_chat_messages',
            sa.Column('session_id', sa.String(length=36), nullable=False),
            sa.Column('role', sa.String(length=32), nullable=False),
            sa.Column('content', sa.String(), nullable=False),
            sa.Column('context_date', sa.String(length=32), nullable=True),
            sa.Column('query_plan', sa.JSON(), nullable=True),
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['sales_chat_sessions.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('sales_chat_messages', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_sales_chat_messages_id'), ['id'], unique=False)
            batch_op.create_index(batch_op.f('ix_sales_chat_messages_session_id'), ['session_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales_chat_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_chat_messages_session_id'))
        batch_op.drop_index(batch_op.f('ix_sales_chat_messages_id'))
    op.drop_table('sales_chat_messages')

    with op.batch_alter_table('sales_chat_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_chat_sessions_id'))
    op.drop_table('sales_chat_sessions')

    op.create_table(
        'assistant_conversations',
        sa.Column('last_spec', sa.JSON(), nullable=True),
        sa.Column('pending_clarification', sa.JSON(), nullable=True),
        sa.Column('scope', sa.String(length=50), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('assistant_conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_conversations_id'), ['id'], unique=False)

    op.create_table(
        'assistant_messages',
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('result_ids', sa.JSON(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('assistant_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_messages_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assistant_messages_id'), ['id'], unique=False)

    op.create_table(
        'assistant_traces',
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('message_id', sa.String(length=36), nullable=True),
        sa.Column('question', sa.String(), nullable=True),
        sa.Column('spec', sa.JSON(), nullable=True),
        sa.Column('grounding', sa.JSON(), nullable=True),
        sa.Column('executor', sa.String(length=50), nullable=True),
        sa.Column('sql_text', sa.String(), nullable=True),
        sa.Column('rows', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.JSON(), nullable=True),
        sa.Column('tokens', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('feedback', sa.String(length=20), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id']),
        sa.ForeignKeyConstraint(['message_id'], ['assistant_messages.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('assistant_traces', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_traces_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assistant_traces_id'), ['id'], unique=False)
