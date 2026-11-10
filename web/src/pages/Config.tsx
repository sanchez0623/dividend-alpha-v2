import React from "react";
import { api } from "../api";
import { Card, useFetch, useDatasources } from "../components/ui";

const DS_BADGE: Record<string, [string, string]> = {
  connected: ["g", "已连通"],
  configured: ["a", "已配置"],
  missing: ["m", "未配置"],
  failed: ["r", "失败"],
  offline: ["m", "后端未启动"],
};

export default function Config() {
  const { data } = useFetch(api.config, []);
  const ds = useDatasources();
  const sources = ds.loading
    ? [{ name: "—", role: "探活中", status: "offline" }]
    : ds.offline
      ? [{ name: "—", role: "后端未启动", status: "offline" }]
      : ds.sources;
  return (
    <>
      <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", marginBottom: 14 }}>
        <Card title="参数只读展示" tag="config/strategy.yaml">
          <pre>{data?.strategy_yaml ?? "加载中…"}</pre>
          <div className="callout">参数只读，禁止手改；修改需改文件并重启 worker。配置哈希：
            <code> {data?.config_hash ?? "—"}</code></div>
        </Card>
        <div>
          <Card title="数据源状态" tag="实时探活">
            <table>
              <tbody>
                {sources.map((s) => {
                  const [cls, label] = DS_BADGE[s.status] ?? DS_BADGE.offline;
                  return (
                    <tr key={s.name}>
                      <td>{s.name}</td><td>{s.role}</td>
                      <td><span className={"badge " + cls}>{label}</span>
                        {s.bars != null && (
                          <span style={{ color: "var(--muted)", fontSize: 10, marginLeft: 6 }}>
                            {s.bars}根K线
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
          <div className="kpis" style={{ gridTemplateColumns: "1fr", marginTop: 14 }}>
            <div className="card kpi"><div className="lbl">基线校验</div>
              <div className="v" style={{ fontSize: 15 }}><span className="badge g">通过</span></div>
              <div className="dc">test_dividend_annual</div></div>
            <div className="card kpi"><div className="lbl">配置哈希</div>
              <div className="v" style={{ fontSize: 13 }}>{data?.config_hash ?? "—"}</div></div>
          </div>
        </div>
      </div>
      <Card title="已知限制（8 条）">
        <table>
          <tbody>
            {(data?.limitations ?? []).map((l, i) => (
              <tr key={i}><td style={{ width: 30 }}>{i + 1}</td><td>{l}</td></tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
