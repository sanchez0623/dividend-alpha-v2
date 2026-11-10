import React from "react";
import { api } from "../api";
import { Card, Num, ProgressBar, fmtMoney, fmtPct, useFetch, useDatasources } from "../components/ui";

export default function Overview() {
  const { data, err } = useFetch(api.overview, []);
  const { data: sigs } = useFetch(api.signals, []);
  const { data: pos } = useFetch(api.positions, []);
  const ds = useDatasources();
  const prim = ds.sources.find((s) => s.name === "ths_fuyao");
  const today = new Date().toISOString().slice(0, 10);
  const todaySignals = (sigs ?? []).filter((s) => s.trade_date === today);
  const top5 = (pos ?? []).slice(0, 5);
  const banner = (
    <div className="banner">
      生产系统自 <b>2026-11-05</b> 起前瞻运行。历史基线数字仅供参照 →{" "}
      <a className="link" onClick={() => window.dispatchEvent(new CustomEvent("da:navigate", { detail: { page: "archive" } }))}>
        历史归档
      </a>
    </div>
  );
  if (err) return <>{banner}<div className="callout amber">后端接口不可达（{String(err.message || err)}）——请确认服务已启动。</div></>;
  if (!data) return <>{banner}<div className="skel">加载中…</div></>;
  const k = data;
  return (
    <>
      {banner}
      <div className="kpis" style={{ marginBottom: 14 }}>
        <div className="card kpi"><div className="lbl">总资产</div><div className="v"><Num n={k.total_equity} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">可用现金</div><div className="v"><Num n={k.cash} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">持仓市值</div><div className="v"><Num n={k.positions_value} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">当日盈亏</div><div className="v" style={{ color: (k.daily_pnl.value ?? 0) >= 0 ? "var(--acc)" : "var(--red)" }}><Num n={k.daily_pnl} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">持仓浮盈</div><div className="v" style={{ color: (k.unrealized_total.value ?? 0) >= 0 ? "var(--acc)" : "var(--red)" }}><Num n={k.unrealized_total} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">持仓只数</div><div className="v"><Num n={k.position_count} fmt={(v) => String(Math.round(v))} /></div></div>
        <div className="card kpi"><div className="lbl">数据源健康</div><div className="v" style={{ fontSize: 15 }}>
          {ds.loading ? <span className="badge a">探活中</span>
            : ds.offline ? <span className="badge a">后端未启动</span>
            : prim?.status === "connected" ? <span className="badge g">已连通</span>
            : <span className="badge r">主源失败</span>}
        </div><div className="dc">{prim?.status === "connected" ? `主源 fuyao · ${prim.bars ?? 0} 根K线` : "启动后端显示真值"}</div></div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", marginBottom: 14 }}>
        <Card title="前瞻运行进度" tag="实时">
          {k.progress.map((p) => (
            <ProgressBar key={p.label} label={p.label} value={p.value} target={p.target}
              neutral={p.target === null} neg={p.target === null && p.value < 0} />
          ))}
        </Card>
        <Card title="研究基线" tag="归档">
          <div className="callout amber">
            历史样本年化 <b>10.61%</b>、样本外 15.61% —— 仅为 2015-2026 历史样本里的一个数，
            不是预期收益。<a className="link" onClick={() => window.dispatchEvent(new CustomEvent("da:navigate", { detail: { page: "archive" } }))}>查看归档</a>
          </div>
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card title="今日信号" tag="实时">
          <table>
            <thead><tr><th>标的</th><th>强度</th><th>预期净收益</th><th>成本闸</th></tr></thead>
            <tbody>
              {todaySignals.length === 0 && (
                <tr><td colSpan={4} className="skel">今日无信号（触发：20日跌幅≥12% 且 RSI(14)＜25）</td></tr>
              )}
              {todaySignals.map((r) => (
                <tr key={r.symbol}>
                  <td>{r.symbol}</td>
                  <td className="num"><Num n={r.strength} fmt={(v) => v.toFixed(2)} /></td>
                  <td className="num"><Num n={r.expected_net} fmt={fmtPct} /></td>
                  <td>{r.cost_gate}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="持仓概览（前 5）" tag="实时">
          <table>
            <thead><tr><th>代码</th><th>名称</th><th>行业</th><th className="num">股息率</th></tr></thead>
            <tbody>
              {top5.length === 0 && (
                <tr><td colSpan={4} className="skel">暂无持仓（等待月度调仓建仓）</td></tr>
              )}
              {top5.map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td><td>{p.name ?? "—"}</td><td>{p.industry_l1 ?? "—"}</td>
                  <td className="num"><Num n={p.div_yield_ttm} fmt={(v) => fmtPct(v * 100)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  );
}
