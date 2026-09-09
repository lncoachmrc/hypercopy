"""Bind jobs and executions to immutable execution destination epochs.

Revision ID: 0012_destination_fence
Revises: 0011_execution_epochs
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0012_destination_fence'
down_revision = '0011_execution_epochs'
branch_labels = None
depends_on = None


def _add_destination_columns(table: str) -> None:
    op.add_column(table, sa.Column('execution_epoch_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(table, sa.Column('execution_provider', sa.String(length=24), nullable=True))
    op.add_column(table, sa.Column('execution_network', sa.String(length=16), nullable=True))
    op.create_foreign_key(
        f'fk_{table}_execution_epoch_id',
        table,
        'execution_epochs',
        ['execution_epoch_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_check_constraint(
        f'ck_{table}_execution_provider',
        table,
        "execution_provider IS NULL OR execution_provider IN ('hyperliquid','risex')",
    )
    op.create_check_constraint(
        f'ck_{table}_execution_network',
        table,
        "execution_network IS NULL OR execution_network IN ('mainnet','testnet')",
    )
    op.create_index(f'ix_{table}_execution_epoch_id', table, ['execution_epoch_id'])
    op.create_index(
        f'ix_{table}_execution_destination',
        table,
        ['execution_provider', 'execution_network'],
    )


def _drop_destination_columns(table: str) -> None:
    op.drop_index(f'ix_{table}_execution_destination', table_name=table)
    op.drop_index(f'ix_{table}_execution_epoch_id', table_name=table)
    op.drop_constraint(f'ck_{table}_execution_network', table, type_='check')
    op.drop_constraint(f'ck_{table}_execution_provider', table, type_='check')
    op.drop_constraint(f'fk_{table}_execution_epoch_id', table, type_='foreignkey')
    op.drop_column(table, 'execution_network')
    op.drop_column(table, 'execution_provider')
    op.drop_column(table, 'execution_epoch_id')


def upgrade() -> None:
    # Historical rows deliberately remain NULL. Assigning them to the user's current
    # epoch would invent destination evidence that did not exist when they were made.
    _add_destination_columns('copy_jobs')
    _add_destination_columns('executions')


def downgrade() -> None:
    _drop_destination_columns('executions')
    _drop_destination_columns('copy_jobs')
