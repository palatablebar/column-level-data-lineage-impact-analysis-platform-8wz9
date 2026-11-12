/**
 * 集合运算（UNION / EXCEPT / INTERSECT）逐分支列位对齐面板。
 * 数据来自后端 report_json.set_ops，与 parser 侧 setops.py 的 describe_setop 对应。
 */

export interface SetopPosition {
  position: number;
  target_column: string;
  branch_column: string;
  sources: string[]; // "asset_id:column"
}

export interface SetopBranch {
  branch: number;
  operator: string | null;
  distinct: boolean;
  column_count: number;
  unconfirmed: boolean;
  reason: string | null;
  covered_positions: SetopPosition[];
}

export interface SetOpsReport {
  arity: number;
  result_columns: string[];
  missing_positions: Record<string, number[]>;
  branches: SetopBranch[];
}

interface Props {
  setOps: SetOpsReport;
  /** asset_id → 展示名，来自页面已加载的资产清单（查不到时回退原值） */
  assetNames?: Record<string, string>;
}

function sourceLabel(ref: string, assetNames?: Record<string, string>): string {
  const [assetId, ...rest] = ref.split(':');
  const column = rest.join(':');
  return `${assetNames?.[assetId] || assetId}.${column}`;
}

export function SetOpsPanel({ setOps, assetNames }: Props) {
  const hasGap = Object.keys(setOps.missing_positions || {}).length > 0;
  const hasUnconfirmed = setOps.branches.some((b) => b.unconfirmed);

  return (
    <div style={{ marginTop: 10 }}>
      <div className="eyebrow">
        集合运算列位对齐 · {setOps.branches.length} 个分支 / {setOps.arity} 个结果列
      </div>
      {(hasGap || hasUnconfirmed) && (
        <div className="notice warn small" style={{ marginTop: 6 }}>
          {hasGap && (
            <div>
              存在列位缺口：
              {Object.entries(setOps.missing_positions).map(([pos, branches]) => (
                <span key={pos} className="badge" style={{ marginLeft: 6 }}>
                  第 {pos} 列缺分支 {branches.join('、')}
                </span>
              ))}
              ；已知分支来源已保留，缺口分支的下游贡献无法确认。
            </div>
          )}
          {hasUnconfirmed && (
            <div style={{ marginTop: 4 }}>
              {setOps.branches
                .filter((b) => b.unconfirmed)
                .map((b) => (
                  <span key={b.branch}>
                    分支 {b.branch} 来源无法确认{b.reason ? `（${b.reason}）` : ''}；该分支未计入下列来源，
                    请勿把当前结果当作完整清单。
                  </span>
                ))}
            </div>
          )}
        </div>
      )}
      <table className="data-table dark" style={{ marginTop: 6 }}>
        <thead>
          <tr>
            <th>分支</th>
            <th>运算符</th>
            <th>列位 → 结果列</th>
            <th>分支列</th>
            <th>该分支追踪到的来源</th>
          </tr>
        </thead>
        <tbody>
          {setOps.branches.map((branch) => {
            const span = Math.max(1, branch.covered_positions.length);
            if (branch.covered_positions.length === 0) {
              return (
                <tr key={branch.branch}>
                  <td>
                    分支 {branch.branch}
                    {branch.unconfirmed && (
                      <span className="badge" style={{ marginLeft: 6 }}>
                        无法确认
                      </span>
                    )}
                  </td>
                  <td className="mono small">{branch.operator || '— 首分支'}</td>
                  <td colSpan={3} className="muted small">
                    无可溯源列位
                  </td>
                </tr>
              );
            }
            return branch.covered_positions.map((p, rowIndex) => (
              <tr key={`${branch.branch}-${p.position}`}>
                {rowIndex === 0 && (
                  <>
                    <td rowSpan={span}>
                      分支 {branch.branch}
                      {branch.unconfirmed && (
                        <span className="badge" style={{ marginLeft: 6 }}>
                          无法确认
                        </span>
                      )}
                    </td>
                    <td rowSpan={span} className="mono small">
                      {branch.operator || '— 首分支'}
                    </td>
                  </>
                )}
                <td className="mono small">
                  #{p.position} → <strong>{p.target_column}</strong>
                </td>
                <td className="mono small">{p.branch_column}</td>
                <td className="mono small">
                  {p.sources.length === 0 ? (
                    <span className="muted">无已确认来源</span>
                  ) : (
                    p.sources.map((ref) => (
                      <span key={ref} className="badge" style={{ marginRight: 4 }}>
                        {sourceLabel(ref, assetNames)}
                      </span>
                    ))
                  )}
                </td>
              </tr>
            ));
          })}
        </tbody>
      </table>
      <div className="small muted" style={{ marginTop: 6 }}>
        结果列按 SQL 标准以列位对齐，列名取最左分支；分支内部的聚合/派生边类型见上方列映射。
      </div>
    </div>
  );
}
