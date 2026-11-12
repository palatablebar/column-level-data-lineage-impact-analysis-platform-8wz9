"""seed_demo 场景测试：锁定 9 段演示 SQL 的状态分布与并集链关键边。

seed_demo 会导入完整应用栈（含数据库引擎配置），本测试不连接数据库，
仅复用其中的 ASSETS / TRANSFORMATIONS 常量驱动纯解析。
"""
from __future__ import annotations

import sys
import types


def _install_stubs() -> None:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.__version__ = "2.9.10-stub"
    psycopg2.apilevel = "2.0"
    psycopg2.threadsafety = 2
    psycopg2.paramstyle = "pyformat"
    for sub in ("extensions", "sql", "extras"):
        module = types.ModuleType(f"psycopg2.{sub}")
        setattr(psycopg2, sub, module)
        sys.modules[f"psycopg2.{sub}"] = module
    sys.modules["psycopg2"] = psycopg2

    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")

    class CryptContext:
        def __init__(self, *args, **kwargs):
            pass

        def hash(self, raw):
            return f"stub:{raw}"

        def verify(self, raw, hashed):
            return False

    context.CryptContext = CryptContext
    passlib.context = context
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context


def main() -> int:
    _install_stubs()

    from app.parsers.context import AssetColumn, AssetInfo
    from app.parsers.engine import parse_sql
    from scripts.seed_demo import ASSETS, DATABASE, TRANSFORMATIONS

    infos = {}
    for spec in ASSETS:
        display = f"{spec['schema_name']}.{spec['name']}"
        produced = {"pay_method"} if display == "dwd.order_detail" else set()
        infos[display] = AssetInfo(
            id=f"id-{display}",
            database_name=DATABASE,
            schema_name=spec["schema_name"],
            name=spec["name"],
            columns=[AssetColumn(name=c["name"], data_type=c.get("data_type")) for c in spec["columns"]],
            produced_columns=produced,
        )

    failures = 0
    statuses = {}
    for spec in TRANSFORMATIONS:
        output = infos[spec["output"]]
        inputs = [infos[name] for name in spec["inputs"]]
        result = parse_sql(spec["sql"], spec["dialect"], output, inputs + [output])
        statuses[spec["name"]] = result.status
        print(f"{spec['name']:40s} {result.status:8s} edges={sum(len(v) for v in result.columns.values())}")

    parsed = [n for n, s in statuses.items() if s == "parsed"]
    partial = [n for n, s in statuses.items() if s == "partial"]
    ok = len(parsed) == 7 and len(partial) == 2
    print(f"[{'PASS' if ok else 'FAIL'}] 状态分布 7 parsed / 2 partial（实际 {len(parsed)} / {len(partial)}）")
    failures += not ok
    ok = {"t06_star_partial_from_order_detail", "t09_union_arity_mismatch_partial"} <= set(partial)
    print(f"[{'PASS' if ok else 'FAIL'}] partial 为 t06(star) 与 t09(arity)")
    failures += not ok

    t07 = next(s for s in TRANSFORMATIONS if s["name"] == "t07_month_end_settle_union")
    r07 = parse_sql(t07["sql"], "postgres", infos["dwd.settle_union"],
                    [infos["ods.orders"], infos["ods.refunds"], infos["dwd.settle_union"]])
    ok = sum(len(v) for v in r07.columns.values()) == 6 and len(r07.set_ops["branches"]) == 2
    print(f"[{'PASS' if ok else 'FAIL'}] t07 并集 6 条边 + 双分支 set_ops")
    failures += not ok

    t08 = next(s for s in TRANSFORMATIONS if s["name"] == "t08_settle_report_rollup")
    r08 = parse_sql(t08["sql"], "postgres", infos["ads.settle_report"],
                    [infos["dwd.settle_union"], infos["ads.settle_report"]])
    ok = r08.status == "parsed" and any(r.kind == "aggregate" and r.column == "amt" for r in r08.columns.get("net_amt", []))
    print(f"[{'PASS' if ok else 'FAIL'}] t08 net_amt 聚合边来自 settle_union.amt")
    failures += not ok

    t09 = next(s for s in TRANSFORMATIONS if s["name"] == "t09_union_arity_mismatch_partial")
    r09 = parse_sql(t09["sql"], "postgres", infos["dwd.settle_union"],
                    [infos["ods.orders"], infos["ods.refunds"], infos["dwd.settle_union"]])
    ok = "union_arity_mismatch" in {u["reason"] for u in r09.unresolved}
    print(f"[{'PASS' if ok else 'FAIL'}] t09 记录 union_arity_mismatch")
    failures += not ok

    print(f"\n=== seed 场景测试：{failures} 个失败 ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
