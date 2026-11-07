"""web address (slug) for each company

Revision ID: a1b2c3d4e5f6
Revises: 3d053072cdee
Create Date: 2026-03-22 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '3d053072cdee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('slug', sa.String(length=100), nullable=True))
    op.create_unique_constraint('uq_companies_slug', 'companies', ['slug'])
    # Fill slugs for existing companies from their names; add the id when two names clash.
    op.execute("""
        UPDATE companies
        SET slug = lower(regexp_replace(
            regexp_replace(trim(company_name), '[^a-zA-Z0-9]+', '-', 'g'),
            '^-|-$', '', 'g'
        ))
        WHERE slug IS NULL
    """)
    op.execute("""
        UPDATE companies c
        SET slug = c.slug || '-' || c.id
        WHERE c.slug IN (SELECT slug FROM companies GROUP BY slug HAVING count(*) > 1)
    """)


def downgrade() -> None:
    op.drop_constraint('uq_companies_slug', 'companies', type_='unique')
    op.drop_column('companies', 'slug')
