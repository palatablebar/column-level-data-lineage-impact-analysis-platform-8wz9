"""集合运算（UNION / EXCEPT / INTERSECT）列级血缘解析测试。

不依赖数据库与 Web 栈，仅使用解析引擎。运行：

    cd backend
    python3 -m tests.test_setops        # sqlglot 在 requirements.txt 中，容器内可直接运行
"""
from __future__ import annotations

from app.parsers.context import AssetColumn, AssetInfo
from app.parsers.engine import parse_sql

DIALECT = "postgres"
DB = "lanzo_dw"


def _asset(schema: str, name: str, columns: list[str]) -> AssetInfo:
    return AssetInfo(
        id=f"id-{schema}-{name}",
        database_name=DB,
        schema_name=schema,
        name=name,
        columns=[AssetColumn(name=c, data_type="varchar") for c in columns],
    )


def _edge_names(result):
    return {target: {(r.asset_id, r.column, r.kind) for r in refs} for target, refs in result.columns.items()}


def _check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def main() -> int:
    failures = 0
    orders = _asset("ods", "orders", ["order_id", "user_id", "amount", "channel", "region"])
    refunds = _asset("ods", "refunds", ["refund_id", "user_id", "refund_amount", "reason"])
    settle = _asset("dwd", "settle_union", ["biz_id", "user_id", "amt", "biz_type"])
    report = _asset("ads", "settle_report", ["user_id", "amt"])

    # 1) 基本 UNION ALL：列位对齐、列名取左分支、来源并集
    r1 = parse_sql(
        """
        SELECT order_id AS biz_id, user_id, amount AS amt, 'order' AS biz_type
        FROM ods.orders
        UNION ALL
        SELECT refund_id, user_id, refund_amount, 'refund'
        FROM ods.refunds
        """,
        DIALECT, settle, [orders, refunds, settle],
    )
    e = _edge_names(r1)
    failures += not _check("1.1 UNION ALL 状态为 parsed", r1.status == "parsed", r1.status)
    failures += not _check("1.2 结果列按左分支命名", set(e) == {"biz_id", "user_id", "amt", "biz_type"}, str(set(e)))
    failures += not _check(
        "1.3 biz_id 并集来自两侧列位 1",
        e.get("biz_id") == {(orders.id, "order_id", "direct"), (refunds.id, "refund_id", "direct")},
        str(e.get("biz_id")),
    )
    failures += not _check(
        "1.4 amt 按列位（非列名）汇聚 amount 与 refund_amount",
        e.get("amt") == {(orders.id, "amount", "direct"), (refunds.id, "refund_amount", "direct")},
        str(e.get("amt")),
    )
    failures += not _check("1.5 字面量列不产生脏边", e.get("biz_type") == set())
    failures += not _check(
        "1.6 set_ops 记录两个分支四个列位，首分支无前置运算符",
        r1.set_ops and r1.set_ops["arity"] == 4
        and [b["operator"] for b in r1.set_ops["branches"]] == [None, "UNION ALL"],
    )

    # 2) 右分支多一列：partial + 缺口定位，已知来源保留
    r2 = parse_sql(
        """
        SELECT order_id AS biz_id, amount AS amt FROM ods.orders
        UNION ALL
        SELECT refund_id, refund_amount, reason FROM ods.refunds
        """,
        DIALECT, settle, [orders, refunds, settle],
    )
    e2 = _edge_names(r2)
    failures += not _check("2.1 列数不一致为 partial", r2.status == "partial", r2.status)
    failures += not _check("2.2 已知列位保留两侧来源", len(e2["biz_id"]) == 2 and len(e2["amt"]) == 2)
    mismatch = [u for u in r2.unresolved if u["reason"] == "union_arity_mismatch"]
    failures += not _check("2.3 缺口定位到分支 1", bool(mismatch) and mismatch[0]["missing"] == ["branch_1"], str(mismatch))
    failures += not _check("2.4 set_ops.missing_positions 同步标注", r2.set_ops["missing_positions"].get("3") == [1])

    # 3) 右分支少一列：缺口指向分支 2
    r3 = parse_sql(
        """
        SELECT order_id AS biz_id, amount AS amt, channel AS ch FROM ods.orders
        UNION ALL
        SELECT refund_id, refund_amount FROM ods.refunds
        """,
        DIALECT, settle, [orders, refunds, settle],
    )
    mismatch3 = [u for u in r3.unresolved if u["reason"] == "union_arity_mismatch"]
    failures += not _check("3.1 右分支缺列为 partial 且缺口指向分支 2", r3.status == "partial" and mismatch3 and mismatch3[0]["missing"] == ["branch_2"])
    failures += not _check("3.2 已知来源不丢失", len(_edge_names(r3)["biz_id"]) == 2)

    # 4) 分支表不在输入资产：保留已知来源 + 显式声明无法确认
    r4 = parse_sql(
        """
        SELECT order_id AS biz_id, amount AS amt FROM ods.orders
        UNION ALL
        SELECT refund_id, refund_amount FROM ods.refunds
        """,
        DIALECT, settle, [orders, settle],
    )
    e4 = _edge_names(r4)
    reasons4 = {u["reason"] for u in r4.unresolved}
    failures += not _check("4.1 失联分支 → partial", r4.status == "partial")
    failures += not _check("4.2 订单侧来源保留", e4.get("biz_id") == {(orders.id, "order_id", "direct")})
    failures += not _check(
        "4.3 记录 unknown_input_asset + union_branch_unconfirmed",
        {"unknown_input_asset", "union_branch_unconfirmed"} <= reasons4, str(reasons4),
    )
    failures += not _check("4.4 set_ops 标注分支 2 unconfirmed", r4.set_ops["branches"][1]["unconfirmed"] is True)

    # 5) 三分支压平 + CAST 派生边 + 运算符序列
    payments = _asset("ods", "payments", ["pay_id", "user_id", "pay_amount"])
    r5 = parse_sql(
        """
        SELECT order_id AS biz_id, CAST(amount AS decimal(18,2)) AS amt FROM ods.orders
        UNION ALL
        SELECT refund_id, refund_amount FROM ods.refunds
        UNION
        SELECT pay_id, pay_amount FROM ods.payments
        """,
        DIALECT, settle, [orders, refunds, payments, settle],
    )
    e5 = _edge_names(r5)
    failures += not _check("5.1 三分支压平为 parsed", r5.status == "parsed", r5.status)
    failures += not _check("5.2 三分支来源全部进 amt", len(e5["amt"]) == 3)
    failures += not _check("5.3 CAST 分支保持 derived", (orders.id, "amount", "derived") in e5["amt"])
    failures += not _check(
        "5.4 运算符序列 首分支/UNION ALL/UNION",
        [b["operator"] for b in r5.set_ops["branches"]] == [None, "UNION ALL", "UNION"],
    )

    # 6) EXCEPT / INTERSECT 同机制
    r6 = parse_sql(
        "SELECT order_id AS biz_id, amount AS amt FROM ods.orders EXCEPT SELECT refund_id, refund_amount FROM ods.refunds",
        DIALECT, settle, [orders, refunds, settle],
    )
    failures += not _check("6.1 EXCEPT 可解析且按列位出边", r6.status == "parsed" and len(r6.columns["amt"]) == 2)
    failures += not _check("6.2 运算符标注 EXCEPT", r6.set_ops["branches"][1]["operator"] == "EXCEPT")
    r6b = parse_sql(
        "SELECT order_id, amount FROM ods.orders INTERSECT SELECT refund_id, refund_amount FROM ods.refunds",
        DIALECT, settle, [orders, refunds, settle],
    )
    failures += not _check("6.3 INTERSECT 可解析", r6b.status == "parsed" and r6b.set_ops["branches"][1]["operator"] == "INTERSECT")

    # 7) UNION 作为派生表：下游报表穿透追踪
    r7 = parse_sql(
        """
        SELECT u.user_id AS user_id, SUM(u.amt) AS amt
        FROM (
            SELECT user_id, amount AS amt FROM ods.orders
            UNION ALL
            SELECT user_id, refund_amount AS amt FROM ods.refunds
        ) u
        GROUP BY u.user_id
        """,
        DIALECT, report, [orders, refunds, report],
    )
    e7 = _edge_names(r7)
    failures += not _check("7.1 UNION 派生表整体 parsed", r7.status == "parsed", r7.status)
    failures += not _check(
        "7.2 聚合边穿透 UNION 到 amount 与 refund_amount",
        e7.get("amt") == {(orders.id, "amount", "aggregate"), (refunds.id, "refund_amount", "aggregate")},
        str(e7.get("amt")),
    )

    # 8) SELECT * 与列位对齐协同（等宽 parsed / 不等宽 partial）
    refunds_wide = _asset("ods", "refunds_wide", ["refund_id", "user_id", "refund_amount", "refund_channel", "refund_region"])
    wide = _asset("dwd", "wide_union", ["order_id", "user_id", "amount", "channel", "region"])
    r8 = parse_sql("SELECT * FROM ods.orders UNION ALL SELECT * FROM ods.refunds_wide", DIALECT, wide, [orders, refunds_wide, wide])
    failures += not _check("8.1 等宽 SELECT * 并集 parsed", r8.status == "parsed", r8.status)
    failures += not _check(
        "8.2 第 3 列位追踪到 amount 与 refund_amount",
        _edge_names(r8).get("amount") == {(orders.id, "amount", "direct"), (refunds_wide.id, "refund_amount", "direct")},
    )
    r8b = parse_sql("SELECT * FROM ods.orders UNION ALL SELECT * FROM ods.refunds", DIALECT, wide, [orders, refunds, wide])
    failures += not _check(
        "8.3 不等宽星号并集 partial 且保留已知来源",
        r8b.status == "partial"
        and "union_arity_mismatch" in {u["reason"] for u in r8b.unresolved}
        and _edge_names(r8b).get("region") == {(orders.id, "region", "direct")},
    )

    # 9) 括号分支 + 顶层 WITH
    r9 = parse_sql(
        """
        WITH base AS (SELECT order_id, amount FROM ods.orders)
        (SELECT order_id AS biz_id, amount AS amt FROM base)
        UNION ALL
        (SELECT refund_id, refund_amount FROM ods.refunds)
        """,
        DIALECT, settle, [orders, refunds, settle],
    )
    failures += not _check("9.1 括号分支 + 顶层 WITH 可解析", r9.status == "parsed", r9.status)
    failures += not _check(
        "9.2 CTE 分支溯源到真实资产",
        _edge_names(r9).get("amt") == {(orders.id, "amount", "direct"), (refunds.id, "refund_amount", "direct")},
    )

    # 10) CTE 自身是 UNION
    r10 = parse_sql(
        """
        WITH u AS (
            SELECT user_id, amount AS amt FROM ods.orders
            UNION ALL
            SELECT user_id, refund_amount AS amt FROM ods.refunds
        )
        SELECT user_id, SUM(amt) AS amt FROM u GROUP BY user_id
        """,
        DIALECT, report, [orders, refunds, report],
    )
    failures += not _check("10.1 CTE 内 UNION + 外层聚合 parsed", r10.status == "parsed", r10.status)
    failures += not _check(
        "10.2 穿透 CTE+UNION 追踪两源且为 aggregate",
        len(_edge_names(r10).get("amt", set())) == 2 and all(k == "aggregate" for _, _, k in _edge_names(r10)["amt"]),
    )

    # 11) INSERT ... SELECT ... UNION
    r11 = parse_sql(
        """
        INSERT INTO dwd.settle_union (biz_id, user_id, amt, biz_type)
        SELECT order_id, user_id, amount, 'order' FROM ods.orders
        UNION ALL
        SELECT refund_id, user_id, refund_amount, 'refund' FROM ods.refunds
        """,
        DIALECT, settle, [orders, refunds, settle],
    )
    failures += not _check("11.1 INSERT...UNION 可解析且四列对齐", r11.status == "parsed" and len(r11.columns) == 4)

    print(f"\n=== 集合运解析测试：{failures} 个失败 ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
