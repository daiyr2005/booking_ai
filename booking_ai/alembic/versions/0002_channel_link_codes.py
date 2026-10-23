"""коды привязки мессенджеров (ai_channel_link_codes)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from mysite.database.ai_models import ChannelLinkCode

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # baseline строится из текущих моделей, поэтому на свежей базе таблица уже есть: checkfirst
    ChannelLinkCode.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    ChannelLinkCode.__table__.drop(bind=op.get_bind(), checkfirst=True)
