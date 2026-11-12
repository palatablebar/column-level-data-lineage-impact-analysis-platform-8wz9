"""集合运算（UNION / UNION ALL / EXCEPT / INTERSECT）的列级展开。

核心规则（与 README「解析能力矩阵」同步）：

1. 集合运算**按列位（ordinal position）对齐**，而不是按列名对齐——这是 SQL
   标准对 UNION / EXCEPT / INTERSECT 的列合并语义。结果列名取**最左分支**的
   输出名（左分支无别名时回退为 ``col_{index+1}``）。
2. 每个结果列的来源 = 各分支同一列位投影来源的并集；分支缺失该列位（两侧列数
   不一致）时不丢弃已知分支，保留已解析来源并把缺失分支记为 partial。
3. 分支内部仍可能是 SELECT * / 未知表 / 裸列歧义，这些分支自己产生的 partial
   标记照常保留；本模块只负责“分支间对齐”这一层的完整性。
4. 分支产出为零（例如整条分支引用了不在输入清单里的表）时，该分支无法为任何
   结果列提供来源：保留其它分支的已知来源，并显式记录该分支无法确认，**不会**
   把不完整结果伪装成完整清单。

对外入口：:func:`process_setop`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from app.parsers.context import ParseContext
from app.parsers.projection import output_name
from app.parsers.scope import Ref, Scope

# 注意：engine.process_select 在函数内延迟导入，避免 engine <-> setops 循环导入。

# sqlglot 节点类型 → 报告里使用的稳定运算符名称。
# 注意 Union / Except / Intersect 的共同基类是 exp.SetOperation，
# 不能只判 exp.Union（Except/Intersect 不是它的子类）。
_SETOP_KIND = {
    exp.Union: "union",
    exp.Except: "except",
    exp.Intersect: "intersect",
}


@dataclass
class SetopBranch:
    """集合运算的一个分支的解析快照。"""

    index: int
    operator: str  # 该分支与其左侧分支之间的运算符（首个分支为 ""）
    distinct: bool
    columns: dict[str, list[Ref]] = field(default_factory=dict)
    # 分支投影失败 / 无任何可溯源列时为 True（来源无法确认）
    unconfirmed: bool = False
    reason: str | None = None


@dataclass
class SetopResult:
    columns: dict[str, list[Ref]]
    branches: list[SetopBranch]
    # 结果列位 → 该列位缺失来源的分支下标（列数不一致场景）
    missing_positions: dict[int, list[int]] = field(default_factory=dict)


def _unwrap(node: exp.Expression) -> exp.Expression:
    """剥掉括号子查询：``(SELECT ...)`` 在 AST 里是 Subquery 包裹 Select。"""
    while isinstance(node, exp.Subquery):
        node = node.this
    return node


def _operator(node: exp.Expression) -> tuple[str, bool]:
    """返回 (运算符名, 是否去重)。UNION ALL → distinct=False。"""
    kind = _SETOP_KIND.get(type(node), "union")
    return kind, bool(node.args.get("distinct"))


def _flatten(node: exp.Expression, operator: str = "", distinct: bool = False) -> list[tuple[exp.Expression, str, bool]]:
    """中序展开集合运算树，返回 [(Select/SetOperation 节点, 运算符, distinct), ...]。

    右结合/左结合的多分支写法（``a UNION b UNION c``）都被压平成有序分支列表，
    分支下标即 SQL 书写顺序（也是列位对齐顺序）。
    """
    node = _unwrap(node)
    if isinstance(node, exp.SetOperation):
        kind, node_distinct = _operator(node)
        left = _flatten(node.this, operator, distinct)
        right = _flatten(node.expression, kind, node_distinct)
        return left + right
    return [(node, operator, distinct)]


def _branch_output_names(branch: exp.Expression) -> list[str]:
    """取分支 SELECT 的投影输出名（含 Star 占位），用于确定结果列名。"""
    select = _unwrap(branch)
    if not isinstance(select, exp.Select):
        return []
    names: list[str] = []
    for index, projection in enumerate(select.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star) or (
            isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)
        ):
            # SELECT * 的实际列名在 process_select 展开后才知道，这里先占位
            names.append("*")
            continue
        names.append(output_name(projection, index))
    return names


def process_setop(
    node: exp.SetOperation,
    outer_scope: Scope,
    ctx: ParseContext,
) -> SetopResult:
    """解析一个集合运算节点，返回按列位对齐后的合并结果。

    - 每个分支递归走 :func:`process_select`（CTE / 子查询 / JOIN / 星号展开复用
      既有逻辑），分支自身的 partial 标记会直接累积到 ``ctx``。
    - 合并时按列位取并集；结果列名以最左分支为准。
    - ``outer_scope`` 已包含挂在 UNION 节点上的 WITH CTE（由 engine 注册）。
    """
    # 延迟导入：engine 顶部也会导入本模块，函数内导入可避开循环初始化
    from app.parsers.engine import _register_union_ctes, process_select

    # 嵌套场景（派生表 / CTE 内的集合运算）下，WITH 挂在集合节点本身，
    # 需要在进入分支前注册到父作用域之上，保证每个分支都能查到这些 CTE。
    outer_scope = _register_union_ctes(node, outer_scope, ctx)

    raw_branches = _flatten(node)
    branches: list[SetopBranch] = []
    branch_columns: list[dict[str, list[Ref]]] = []
    max_arity = 0

    for index, (branch_node, operator, distinct) in enumerate(raw_branches):
        select = _unwrap(branch_node)
        branch = SetopBranch(index=index, operator=operator or "union", distinct=distinct)
        if not isinstance(select, exp.Select):
            branch.unconfirmed = True
            branch.reason = f"分支 {index + 1} 不是 SELECT，无法展开列级来源"
            ctx.mark_partial(
                "union_branch_not_select",
                detail=branch.reason,
                warning=f"集合运算第 {index + 1} 个分支不是 SELECT，该分支来源无法确认",
            )
            branches.append(branch)
            branch_columns.append({})
            continue

        columns = process_select(select, outer_scope, ctx)
        branch.columns = columns
        branch_columns.append(columns)
        # columns 非空但所有列位都没有可溯源 Ref（典型：分支表不在输入资产清单），
        # 该分支对结果的贡献无法确认
        has_refs = any(refs for refs in columns.values())
        if not columns or not has_refs:
            branch.unconfirmed = True
            if columns:
                branch.reason = f"分支 {index + 1} 的投影列均无法溯源到输入资产"
            else:
                branch.reason = f"分支 {index + 1} 未推导出任何可溯源列"
            ctx.mark_partial(
                "union_branch_unconfirmed",
                detail=(
                    f"集合运算第 {index + 1} 个分支（{_operator_label(operator, distinct)}）"
                    "没有可溯源的输出列，该分支对结果列的贡献无法确认；其它分支的已知来源保留"
                ),
                warning=f"集合运算第 {index + 1} 个分支来源无法确认，已保留其它分支的已知来源",
            )
        max_arity = max(max_arity, len(columns))
        branches.append(branch)

    # 最左分支的输出名决定结果列名；星号列以展开后的实际列名为准
    left_names = _branch_output_names(raw_branches[0][0])
    merged: dict[str, list[Ref]] = {}
    merged_order: list[str] = []
    missing_positions: dict[int, list[int]] = {}

    for position in range(max_arity):
        target_name = _result_name(position, left_names, branch_columns)
        if target_name not in merged:
            merged[target_name] = []
            merged_order.append(target_name)
        store = {(ref.asset_id, ref.column): ref for ref in merged[target_name]}
        for index, columns in enumerate(branch_columns):
            refs = _refs_at_position(columns, position)
            if refs is None:
                # 该分支列数不足 / 无法确认：不阻塞其它分支，只记录缺口
                missing_positions.setdefault(position, []).append(index + 1)
                continue
            for ref in refs:
                key = (ref.asset_id, ref.column)
                existing = store.get(key)
                if existing is None:
                    store[key] = ref
                else:
                    store[key] = _higher_priority(existing, ref)
        merged[target_name] = list(store.values())

    if missing_positions:
        for position, missing_branches in missing_positions.items():
            target_name = merged_order[position] if position < len(merged_order) else f"col_{position + 1}"
            ctx.mark_partial(
                "union_arity_mismatch",
                column=target_name,
                missing=[f"branch_{b}" for b in missing_branches],
                detail=(
                    f"结果列 {target_name}（第 {position + 1} 列）在分支 "
                    f"{', '.join(map(str, missing_branches))} 中没有对应列位，"
                    "这些分支对该列的来源无法确认；其余分支的已知来源已保留"
                ),
                warning=(
                    f"集合运算分支列数不一致：{target_name} 缺少分支 "
                    f"{', '.join(map(str, missing_branches))} 的来源，已标记 partial"
                ),
            )

    ordered_columns = {name: merged[name] for name in merged_order}
    return SetopResult(columns=ordered_columns, branches=branches, missing_positions=missing_positions)


def _refs_at_position(columns: dict[str, list[Ref]], position: int) -> list[Ref] | None:
    """按列位取分支投影的 refs；列数不足返回 None。"""
    names = list(columns.keys())
    if position >= len(names):
        return None
    return columns[names[position]]


def _result_name(
    position: int,
    left_names: list[str],
    branch_columns: list[dict[str, list[Ref]]],
) -> str:
    """结果列名：优先最左分支该列位的名字，星号/缺名时回退到实际展开列名。"""
    if position < len(left_names) and left_names[position] not in ("", "*"):
        return left_names[position]
    left_columns = branch_columns[0] if branch_columns else {}
    left_actual = list(left_columns.keys())
    if position < len(left_actual):
        return left_actual[position]
    for columns in branch_columns[1:]:
        names = list(columns.keys())
        if position < len(names):
            return names[position]
    return f"col_{position + 1}"


def _higher_priority(existing: Ref, candidate: Ref) -> Ref:
    from app.constants.edge import EDGE_KIND_PRIORITY, EdgeKind

    def priority(ref: Ref) -> int:
        try:
            return EDGE_KIND_PRIORITY[EdgeKind(ref.kind)]
        except ValueError:
            return 0

    return candidate if priority(candidate) > priority(existing) else existing


def _operator_label(operator: str, distinct: bool) -> str:
    base = (operator or "union").upper()
    if base == "UNION" and not distinct:
        return "UNION ALL"
    return base


def describe_setop(result: SetopResult) -> dict:
    """生成报告中的 ``set_ops`` 片段：逐分支、逐列位说明来源覆盖情况。"""
    target_names = list(result.columns.keys())
    branch_reports = []
    for branch in result.branches:
        names = list(branch.columns.keys())
        covered = []
        for position, target in enumerate(target_names):
            if position < len(names):
                covered.append(
                    {
                        "position": position + 1,
                        "target_column": target,
                        "branch_column": names[position],
                        "sources": [
                            f"{ref.asset_id}:{ref.column}" for ref in branch.columns[names[position]]
                        ],
                    }
                )
        branch_reports.append(
            {
                "branch": branch.index + 1,
                # 首个分支没有前置运算符，用 None 表达（前端渲染为“—/首分支”）
                "operator": None if branch.index == 0 else _operator_label(branch.operator, branch.distinct),
                "distinct": branch.distinct,
                "column_count": len(names),
                "unconfirmed": branch.unconfirmed,
                "reason": branch.reason,
                "covered_positions": covered,
            }
        )
    return {
        "arity": len(target_names),
        "result_columns": target_names,
        "missing_positions": {
            str(position + 1): branches for position, branches in result.missing_positions.items()
        },
        "branches": branch_reports,
    }
