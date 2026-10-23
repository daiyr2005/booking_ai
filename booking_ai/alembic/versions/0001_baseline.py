"""baseline: все таблицы Booking AI

Базовая миграция строится из metadata моделей, поэтому точно совпадает с ai_models.py.
Все последующие изменения схемы делайте обычными миграциями:
    alembic revision --autogenerate -m "описание"

Revision ID: 0001
Revises:
"""
from alembic import op

from mysite.database.ai_models import AIBase

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    AIBase.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # только таблицы Booking AI; чужие таблицы в базе не затрагиваются
    AIBase.metadata.drop_all(bind=op.get_bind())
