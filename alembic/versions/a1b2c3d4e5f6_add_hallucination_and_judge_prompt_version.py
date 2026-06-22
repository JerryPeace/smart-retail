"""add hallucination_score and judge_prompt_version to evaluation

Revision ID: a1b2c3d4e5f6
Revises: 58ea04b7bc37
Create Date: 2026-05-06 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '58ea04b7bc37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # hallucination_score: backfill existing rows with 0.5; then NOT NULL
    op.add_column(
        'evaluation',
        sa.Column(
            'hallucination_score',
            sa.Float(),
            nullable=False,
            server_default=sa.text('0.5'),
        ),
    )
    op.alter_column('evaluation', 'hallucination_score', server_default=None)

    # judge_prompt_version: backfill existing rows with 'judge/v1.0'
    op.add_column(
        'evaluation',
        sa.Column(
            'judge_prompt_version',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default=sa.text("'judge/v1.0'"),
        ),
    )
    op.alter_column('evaluation', 'judge_prompt_version', server_default=None)
    op.create_index(
        op.f('ix_evaluation_judge_prompt_version'),
        'evaluation',
        ['judge_prompt_version'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_evaluation_judge_prompt_version'), table_name='evaluation')
    op.drop_column('evaluation', 'judge_prompt_version')
    op.drop_column('evaluation', 'hallucination_score')
