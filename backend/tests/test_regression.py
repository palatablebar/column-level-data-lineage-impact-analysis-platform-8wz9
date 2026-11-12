"""回归测试：seed_demo 原有 6 段非集合运算 SQL 的解析结果必须保持不变。"""
from __future__ import annotations

from app.parsers.context import AssetColumn, AssetInfo
from app.parsers.engine import parse_sql

DB = "lanzo_dw"


def _asset(schema, name, columns, produced=None):
    return AssetInfo(
        id=f"id-{schema}-{name}",
        database_name=DB,
        schema_name=schema,
        name=name,
        columns=[AssetColumn(name=c, data_type="varchar") for c in columns],
        produced_columns=produced or set(),
    )


orders = _asset("ods", "orders", ["order_id", "user_id", "amount", "channel", "region"])
order_detail = _asset("dwd", "order_detail", ["order_id", "user_id", "pay_amount", "channel_region"], produced={"pay_method"})
user_gmv = _asset("dws", "user_gmv", ["user_id", "gmv_amount", "pay_cnt"])
gmv_daily = _asset("ads", "gmv_daily", ["user_id", "gmv", "peak_gmv", "region", "order_cnt"])
cte_result = _asset("dwd", "cte_result", ["order_id", "user_id", "pay_amount", "channel_region", "region"])

CASES = [
    ("t01", """SELECT o.order_id AS order_id, o.user_id AS user_id,
       CAST(o.amount AS decimal(18,2)) AS pay_amount,
       CONCAT(o.channel, '-', o.region) AS channel_region,
       CAST(o.user_id % 10 AS int) AS pay_method FROM ods.orders AS o""",
     order_detail, [orders, order_detail], "parsed", 6),
    ("t02", "SELECT user_id, SUM(pay_amount) AS gmv_amount, COUNT(pay_amount) AS pay_cnt FROM dwd.order_detail GROUP BY user_id",
     user_gmv, [order_detail, user_gmv], "parsed", 3),
    ("t03", "SELECT g.user_id, SUM(g.gmv_amount) AS gmv, MAX(g.gmv_amount) AS peak_gmv FROM (SELECT user_id, gmv_amount FROM dws.user_gmv) AS g GROUP BY g.user_id",
     gmv_daily, [user_gmv, gmv_daily], "parsed", 3),
    ("t04", "WITH base AS (SELECT order_id, region FROM ods.orders), per_region AS (SELECT region, COUNT(*) AS order_cnt FROM base GROUP BY region) SELECT region, order_cnt FROM per_region",
     gmv_daily, [orders, gmv_daily], "parsed", 1),
    ("t05", "SELECT d.order_id AS order_id, o.region AS region FROM dwd.order_detail AS d JOIN ods.orders AS o ON d.order_id = o.order_id",
     cte_result, [order_detail, orders, cte_result], "parsed", 2),
    ("t06", "SELECT * FROM dwd.order_detail", cte_result, [order_detail, cte_result], "partial", 4),
]


def main() -> int:
    failures = 0
    for name, sql, output, inputs, want_status, want_edges in CASES:
        result = parse_sql(sql, "postgres", output, inputs)
        edge_count = sum(len(refs) for refs in result.columns.values())
        ok = result.status == want_status and edge_count == want_edges and result.set_ops is None
        print(f"[{'PASS' if ok else 'FAIL'}] {name} status={result.status} edges={edge_count} set_ops={result.set_ops is not None}")
        failures += not ok
    r06 = parse_sql(CASES[-1][1], "postgres", CASES[-1][3][0], CASES[-1][3])
    ok = "star_unexpanded" in {u["reason"] for u in r06.unresolved}
    print(f"[{'PASS' if ok else 'FAIL'}] t06 partial 原因仍是 star_unexpanded")
    failures += not ok
    print(f"\n=== 回归测试：{failures} 个失败 ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
