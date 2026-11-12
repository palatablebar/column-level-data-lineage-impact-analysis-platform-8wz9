"""0002：lineage_edges 唯一约束纳入 source_asset_id

Revision ID: 0002_edge_unique_with_asset
Revises: 0001_initial
Create Date: 2026-11-12

背景：集合运算（UNION / UNION ALL / EXCEPT / INTERSECT）按列位对齐后，
同一个结果列（target_column）会汇聚多个输入资产的来源列。旧约束
``(transformation_id, source_column, target_column)`` 无法区分“不同资产的
同名列”（例如 orders.user_id 与 refunds.user_id 同时流向 settle.user_id），
会把并集两侧的合法边误判成重复。新约束把 ``source_asset_id`` 纳入去重键，
与 parse_service._replace_edges 的内存去重键保持一致。
"""
from __future__ import annotations

from alembic import op

revision = "0002_edge_unique_with_asset"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_edge_transform_columns"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "lineage_edges", type_="unique")
    op.create_unique_constraint(
        _CONSTRAINT,
        "lineage_edges",
        ["transformation_id", "source_asset_id", "source_column", "target_column"],
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "lineage_edges", type_="unique")
    op.create_unique_constraint(
        _CONSTRAINT,
        "lineage_edges",
        ["transformation_id", "source_column", "target_column"],
    )
