"""端到端集成测试：解析服务落边 + 影响分析穿透 UNION 到最终结算报表。

为不依赖 PostgreSQL，用 SQLite 内存库并把 PG 专有类型（UUID/JSONB/BigInteger）
编译为等价物；被测代码（parse_service / impact_service / parser）不改一行。
运行环境需要 requirements.txt 中的 SQLAlchemy 与 sqlglot：

    cd backend
    python3 -m tests.test_integration_union
"""
from __future__ import annotations

import tempfile
import types

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

# --- 应用默认引擎是 postgresql+psycopg2，测试不连接它，注入最小 dbapi 桩 ---
_psycopg2 = types.ModuleType("psycopg2")
_psycopg2.__version__ = "2.9.10-stub"
_psycopg2.apilevel = "2.0"
_psycopg2.threadsafety = 2
_psycopg2.paramstyle = "pyformat"
for _sub in ("extensions", "sql", "extras"):
    _module = types.ModuleType(f"psycopg2.{_sub}")
    setattr(_psycopg2, _sub, _module)
    globals()[f"_psycopg2_{_sub}"] = _module

# passlib 仅在用户密码工具里使用，测试不校验密码
_passlib = types.ModuleType("passlib")
_passlib_ctx = types.ModuleType("passlib.context")


class _CryptContext:
    def __init__(self, *args, **kwargs):
        pass

    def hash(self, raw: str) -> str:
        return f"stub-hash:{raw}"

    def verify(self, raw: str, hashed: str) -> bool:
        return False


_passlib_ctx.CryptContext = _CryptContext
_passlib.context = _passlib_ctx


def _install_stubs() -> None:
    import sys

    for _sub in ("extensions", "sql", "extras"):
        sys.modules.setdefault(f"psycopg2.{_sub}", globals()[f"_psycopg2_{_sub}"])
    sys.modules.setdefault("psycopg2", _psycopg2)
    sys.modules.setdefault("passlib", _passlib)
    sys.modules.setdefault("passlib.context", _passlib_ctx)


@compiles(PG_UUID, "sqlite")
def _sqlite_uuid(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _sqlite_biginteger(element, compiler, **kw):  # noqa: ARG001
    # SQLite 只有 INTEGER PRIMARY KEY 才是自增 rowid 列
    return "INTEGER"


def _check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def main() -> int:
    _install_stubs()

    from app import models  # noqa: F401  注册模型
    from app.config.database import Base
    from app.models.data_asset import DataAsset
    from app.models.lineage_edge import LineageEdge
    from app.models.transformation import Transformation
    from app.models.user import User
    from app.services import impact_service, parse_service
    from app.utils import password as password_module
    import app.config.settings as settings_module

    password_module.hash_password = lambda raw: f"stub-hash:{raw}"  # type: ignore[attr-defined]
    settings_module.settings.artifact_dir = tempfile.mkdtemp(prefix="datatrace-test-")

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    failures = 0

    with Session(engine) as db:
        user = User(email="analyst@datatrace.local", display_name="tester",
                    password_hash="stub-hash:x", role="analyst")
        db.add(user)
        db.flush()

        def make_asset(schema, name, columns):
            asset = DataAsset(
                owner_id=user.id, name=name, asset_type="table",
                database_name="lanzo_dw", schema_name=schema,
                columns_json=[{"name": c, "data_type": "varchar", "nullable": True, "ordinal": i + 1}
                              for i, c in enumerate(columns)],
                status="active",
            )
            db.add(asset)
            db.flush()
            return asset

        orders = make_asset("ods", "orders", ["order_id", "user_id", "amount"])
        refunds = make_asset("ods", "refunds", ["refund_id", "user_id", "refund_amount"])
        settle = make_asset("dwd", "settle_union", ["biz_id", "user_id", "amt", "biz_type"])
        report = make_asset("ads", "settle_report", ["user_id", "amt"])
        names = {str(a.id): a.display_name for a in db.query(DataAsset).all()}

        def make_transformation(name, sql, inputs, output):
            t = Transformation(
                owner_id=user.id, name=name, dialect="postgres", sql_text=sql,
                input_asset_ids_json=[str(a.id) for a in inputs], output_asset_id=output.id,
                parse_status="pending", version=1, report_json={},
            )
            db.add(t)
            db.flush()
            return parse_service.parse_transformation(db, t)

        union_sql = """
        SELECT order_id AS biz_id, user_id, amount AS amt, 'order' AS biz_type
        FROM ods.orders
        UNION ALL
        SELECT refund_id, user_id, refund_amount, 'refund'
        FROM ods.refunds
        """
        t07 = make_transformation("t07_month_end_settle_union", union_sql, [orders, refunds], settle)
        failures += not _check("E2E-1 并集转换 parsed", t07.parse_status == "parsed", t07.parse_status)
        pairs = {(str(e.source_asset_id), e.source_column, e.target_column)
                 for e in db.query(LineageEdge).filter(LineageEdge.transformation_id == t07.id)}
        want = {
            (str(orders.id), "order_id", "biz_id"), (str(refunds.id), "refund_id", "biz_id"),
            (str(orders.id), "user_id", "user_id"), (str(refunds.id), "user_id", "user_id"),
            (str(orders.id), "amount", "amt"), (str(refunds.id), "refund_amount", "amt"),
        }
        failures += not _check("E2E-2 并集两侧按列位各出一条边（6 条）", pairs == want, str(pairs ^ want))
        failures += not _check(
            "E2E-3 report_json.set_ops 逐分支记录来源",
            t07.report_json.get("set_ops", {}).get("arity") == 4
            and len(t07.report_json["set_ops"]["branches"]) == 2,
        )

        t08 = make_transformation(
            "t08_settle_report_rollup",
            "SELECT s.user_id AS user_id, SUM(s.amt) AS amt FROM dwd.settle_union AS s GROUP BY s.user_id",
            [settle], report,
        )
        failures += not _check("E2E-4 下游报表转换 parsed", t08.parse_status == "parsed", t08.parse_status)

        analysis, _cached = impact_service.analyze(
            db, user, target_asset_id=refunds.id, target_column="refund_amount", change_type="drop_column",
        )
        payload = analysis.report_json
        affected = {names[node["asset_id"]] for node in payload["nodes"]}
        failures += not _check("E2E-5 影响穿透 UNION 到 dwd.settle_union", "dwd.settle_union" in affected, str(affected))
        failures += not _check("E2E-6 影响到达最终结算报表 ads.settle_report", "ads.settle_report" in affected, str(affected))
        report_nodes = [n for n in payload["nodes"] if n["asset_id"] == str(report.id)]
        failures += not _check(
            "E2E-7 报表受影响列 amt 为 aggregate 边",
            any(n["column"] == "amt" and n["edge_kind"] == "aggregate" for n in report_nodes), str(report_nodes),
        )
        checklist = {item["asset_name"] for item in payload["migration_checklist"]}
        failures += not _check("E2E-8 迁移清单包含最终结算报表", "ads.settle_report" in checklist, str(checklist))
        paths = [p for p in payload["paths"] if p["target"]["asset_id"] == str(report.id)]
        chain_ok = any(
            [step["asset_name"] for step in p["path"]] == ["ods.refunds", "dwd.settle_union", "ads.settle_report"]
            for p in paths
        )
        failures += not _check("E2E-9 传播路径完整经过并集层到报表", chain_ok)

        # 兼容性 A：退款侧新增列，重解析不破坏已知血缘
        refunds.columns_json = [*refunds.columns_json,
                                {"name": "coupon_amount", "data_type": "decimal(18,2)", "nullable": True, "ordinal": 4}]
        db.flush()
        t07_v2 = parse_service.parse_transformation(db, db.get(Transformation, t07.id))
        failures += not _check("E2E-10 退款侧新增列后仍 parsed", t07_v2.parse_status == "parsed", t07_v2.parse_status)
        failures += not _check(
            "E2E-11 已知 6 条边全部保留",
            db.query(LineageEdge).filter(LineageEdge.transformation_id == t07.id).count() == 6,
        )

        # 兼容性 B：退款资产从输入清单移除（流下线）
        t09 = Transformation(
            owner_id=user.id, name="t09_refund_removed_partial", dialect="postgres", sql_text=union_sql,
            input_asset_ids_json=[str(orders.id)], output_asset_id=settle.id,
            parse_status="pending", version=1, report_json={},
        )
        db.add(t09)
        db.flush()
        t09 = parse_service.parse_transformation(db, t09)
        reasons = {u["reason"] for u in t09.report_json["unresolved"]}
        failures += not _check("E2E-12 退款流失联为 partial", t09.parse_status == "partial", t09.parse_status)
        failures += not _check(
            "E2E-13 显式声明 unknown_input_asset + union_branch_unconfirmed",
            {"unknown_input_asset", "union_branch_unconfirmed"} <= reasons, str(reasons),
        )
        kept = {(e.source_column, e.target_column)
                for e in db.query(LineageEdge).filter(LineageEdge.transformation_id == t09.id)}
        failures += not _check(
            "E2E-14 订单侧已知来源保留",
            kept == {("order_id", "biz_id"), ("user_id", "user_id"), ("amount", "amt")}, str(kept),
        )
        failures += not _check(
            "E2E-15 set_ops 标出无法确认分支",
            len([b for b in t09.report_json["set_ops"]["branches"] if b["unconfirmed"]]) == 1,
        )

        # 兼容性 C：三分支列数不一致
        t10 = Transformation(
            owner_id=user.id, name="t10_arity_mismatch", dialect="postgres",
            sql_text="""
            SELECT order_id AS biz_id, user_id, amount AS amt FROM ods.orders
            UNION ALL
            SELECT refund_id, user_id, refund_amount FROM ods.refunds
            UNION ALL
            SELECT order_id, user_id FROM ods.orders
            """,
            input_asset_ids_json=[str(orders.id), str(refunds.id)], output_asset_id=settle.id,
            parse_status="pending", version=1, report_json={},
        )
        db.add(t10)
        db.flush()
        t10 = parse_service.parse_transformation(db, t10)
        reasons10 = {u["reason"] for u in t10.report_json["unresolved"]}
        failures += not _check(
            "E2E-16 列数不一致 partial + union_arity_mismatch",
            t10.parse_status == "partial" and "union_arity_mismatch" in reasons10, str(reasons10),
        )
        failures += not _check(
            "E2E-17 缺口列位之外的已知来源仍落边",
            db.query(LineageEdge).filter(
                LineageEdge.transformation_id == t10.id, LineageEdge.target_column == "biz_id").count() == 2,
        )

    print(f"\n=== 端到端集成测试：{failures} 个失败 ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
