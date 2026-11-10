import React from "react";
import { api } from "../api";
import { Card, ProgressBar, useFetch } from "../components/ui";

const KPI_LABEL: Record<string, string> = {
  ann: "年化", oos: "样本外", nav: "期末净值", sharpe: "夏普", mdd: "最大回撤",
};

const REPORTS = [
  ["REPORT3/5/6/9/10/11", "轮动 / 条件换仓 / 强制换仓 证伪", "已证伪"],
  ["REPORT7/8", "增强层（做T）B6 安慰剂未通过", "不显著"],
  ["REPORT2/4", "分红是收益主体", "确认"],
  ["REPORT10", "取现方式对比（年底取最优）", "确认"],
  ["REPORT1", "长期年化 15% 下修", "已下修"],
];

export default function Archive() {
  const { data } = useFetch(api.archive, []);
  return (
    <>
      <div className="banner">
        ⚠️ 以下数字来自 <b>2015-01 ~ 2026-10 历史样本</b>，不是已验证的将来收益；
        生产系统自 <b>2026-11-05</b> 起前瞻运行。
      </div>
      {!data ? (
        <div className="skel">加载归档…</div>
      ) : (
        <>
          <div className="kpis" style={{ gridTemplateColumns: "repeat(5,1fr)", marginBottom: 14 }}>
            {Object.entries(KPI_LABEL).map(([k, label]) => (
              <div className="card kpi" key={k}>
                <div className="lbl">{label}</div>
                <div className="v">{fmtKpi(k, data.kpi[k]?.value)}</div>
                <div className="dc"><span className="tag amber">历史样本</span></div>
              </div>
            ))}
          </div>

          <Card title="历史净值曲线" tag="归档快照 · 只读" style={{ marginBottom: 14 }}>
            <NavChart s={data.s} b2={data.b2} hs300={data.hs300} months={data.months} />
            <div style={{ fontSize: 11, color: "var(--dim)", marginTop: 8 }}>
              S*（青绿）/ B2（灰蓝）/ 沪深300价格指数 · 2015-2026 · <span className="tag amber">估算 · 只读</span>
            </div>
          </Card>

          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 14 }}>
            <Card title="模块贡献" tag="归档">
              {(data.modules ?? []).map((m: any) => (
                <ProgressBar key={m.name} label={m.name} value={m.v} target={null}
                  neutral={m.v >= 0} neg={m.v < 0} />
              ))}
            </Card>
            <Card title="分年度收益 S* vs B2" tag="归档">
              <YearlyBars yearly={data.yearly} />
            </Card>
          </div>

          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <Card title="逐年取现流水" tag="归档">
              <table>
                <thead><tr><th>年份</th><th className="num">分红</th><th className="num">应取</th><th className="num">实取</th><th className="num">兑现率</th></tr></thead>
                <tbody>
                  {(data.withdraw ?? []).map((w: any) => (
                    <tr key={w.y}><td>{w.y}</td><td className="num">{w.div.toLocaleString("zh-CN")}</td>
                      <td className="num">{w.want.toLocaleString("zh-CN")}</td>
                      <td className="num">{w.got.toLocaleString("zh-CN")}</td>
                      <td className="num">{w.rate}%</td></tr>
                  ))}
                </tbody>
              </table>
            </Card>
            <div>
              <Card title="10 轮实证结论摘要" tag="归档">
                <ul style={{ fontSize: 12, color: "var(--muted)", paddingLeft: 18, margin: 0 }}>
                  <li>轮动 / 条件换仓 / 强制换仓：多次验证均为负贡献，永久关闭</li>
                  <li>增强层（做T）：B6 安慰剂未通过，组合层不显著</li>
                  <li>分红是收益主体（累计≈本金 90.5%）</li>
                  <li>取现方式：年底取 30% 最优</li>
                  <li>长期年化 15% 已下修</li>
                </ul>
              </Card>
              <Card title="REPORT 索引" tag="归档">
                <table>
                  <thead><tr><th>报告</th><th>主题</th><th>状态</th></tr></thead>
                  <tbody>
                    {REPORTS.map(([n, t, s]) => (
                      <tr key={n}><td>{n}</td><td>{t}</td>
                        <td><span className={"badge " + (s === "已证伪" ? "r" : "g")}>{s}</span></td></tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </div>
          </div>
          <div className="footer" style={{ paddingLeft: 0 }}>
            研究脚本与缓存归档于 <code>research/</code>，不进生产镜像。
          </div>
        </>
      )}
    </>
  );
}

function fmtKpi(k: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (k === "nav") return (v / 10000).toFixed(1) + " 万";
  if (k === "mdd") return v + "%";
  if (k === "ann" || k === "oos") return v + "%";
  return String(v);
}

function NavChart({ s, b2, hs300, months }: any) {
  const w = 900, h = 260, pad = 30;
  const series = [
    { d: s, c: "#2DD4A7" }, { d: b2, c: "#7C9CC4" }, { d: hs300, c: "#5C6E8C" },
  ];
  const all = series.flatMap((x) => x.d);
  const min = Math.min(...all), max = Math.max(...all);
  const x = (i: number, n: number) => pad + (i / (n - 1)) * (w - pad * 2);
  const y = (v: number) => h - pad - ((v - min) / (max - min)) * (h - pad * 2);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: 260 }}>
      <line x1={pad} y1={y(3000000)} x2={w - pad} y2={y(3000000)} stroke="#22304A" strokeDasharray="4 4" />
      {series.map((sr) => (
        <polyline key={sr.c} fill="none" stroke={sr.c} strokeWidth="1.6"
          points={sr.d.map((v: number, i: number) => `${x(i, sr.d.length)},${y(v)}`).join(" ")} />
      ))}
      <title>S* vs B2 vs 沪深300（2015-2026 历史样本，只读归档）</title>
    </svg>
  );
}

function YearlyBars({ yearly }: any) {
  const max = Math.max(...yearly.map((r: any) => Math.abs(r.s), 1));
  return (
    <table>
      <thead><tr><th>年份</th><th className="num">S*</th><th className="num">B2</th><th style={{ width: "40%" }}>对比</th></tr></thead>
      <tbody>
        {yearly.map((r: any) => (
          <tr key={r.y}><td>{r.y}</td>
            <td className="num" style={{ color: r.s >= 0 ? "var(--acc)" : "var(--red)" }}>{r.s}%</td>
            <td className="num" style={{ color: r.b2 >= 0 ? "var(--blue)" : "var(--red)" }}>{r.b2}%</td>
            <td>
              <div style={{ display: "flex", height: 8, background: "var(--card2)", borderRadius: 3 }}>
                <div style={{ width: `${(Math.abs(r.s) / max) * 100}%`, background: r.s >= 0 ? "var(--acc)" : "var(--red)", borderRadius: 3 }} />
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
