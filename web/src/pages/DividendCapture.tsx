import React from "react";
import { api } from "../api";
import type {
  DcBacktest,
  DcCandidate,
  DcCycle,
  DcNav,
  DcNextAction,
  DcOverview,
  NumberWithClass,
} from "../api";
import { Card, Num, fmtMoney, fmtPct, useFetch } from "../components/ui";

// ★ DC 页面所有数字经 Num 渲染并携带 data_class：
//   前瞻账户数据 = 实时（不额外打标）；历史回测 = 归档（显式"归档 · 只读"）。
const live = (v: number | null | undefined): NumberWithClass =>
  ({ value: v ?? null, data_class: "实时" });
const arch = (v: number | null | undefined): NumberWithClass =>
  ({ value: v ?? null, data_class: "归档" });

// 接口约定比率用小数（0.067 = 6.7%）；若上游误传百分数（|v| > 3）按原值渲染，避免 ×100 放大 100 倍
function toPct(v: number): number {
  return Math.abs(v) > 3 ? v : v * 100;
}
function ratioPct(v: number | null | undefined, signed = true): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (signed ? fmtPct(toPct(v)) : toPct(v).toFixed(2) + "%");
}
const fmtYi = (v: number) =>
  (v / 1e8).toLocaleString("zh-CN", { maximumFractionDigits: 0 }) + " 亿";
const fmtInt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
// 每股价格/分红需要保留小数（fmtMoney 取整会把 27.5 显示成 ¥28）
const fmtPrice = (v: number) =>
  "¥" + v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const TREND_META: Record<string, { label: string; cls: string }> = {
  up_pull: { label: "上涨+回调", cls: "g" },
  up: { label: "上涨", cls: "g" },
  nondown: { label: "非下跌", cls: "a" },
  down: { label: "下跌", cls: "r" },
  insufficient: { label: "数据不足", cls: "d" },
};

function TrendBadge({ trend }: { trend?: string | null }) {
  const m = TREND_META[String(trend ?? "")] ?? { label: String(trend ?? "—"), cls: "d" };
  return <span className={"badge " + m.cls}>{m.label}</span>;
}

function StatusBadge({ status }: { status: string }) {
  const m: Record<string, { label: string; cls: string }> = {
    CLOSED: { label: "已闭环", cls: "g" },
    HOLDING: { label: "持仓中", cls: "a" },
    PLANNED: { label: "待买入", cls: "d" },
    CANCELLED: { label: "已放弃", cls: "r" },
  };
  const x = m[status] ?? { label: status, cls: "d" };
  return <span className={"badge " + x.cls}>{x.label}</span>;
}

// 兼容 next_action 里 div_yield / market_cap 既可能是裸 number、也可能是 NumberWithClass
function numVal(v: unknown): number | null {
  if (typeof v === "number" && !Number.isNaN(v)) return v;
  if (v && typeof v === "object" && "value" in (v as any)) {
    const x = (v as any).value;
    return typeof x === "number" && !Number.isNaN(x) ? x : null;
  }
  return null;
}

export default function DividendCapture() {
  // ★ 五个接口独立拉取：任一失败/缺失都不影响其它区块渲染（历史回测可能早期 available=false）
  const ovF = useFetch(api.dcOverview, []);
  const cyclesF = useFetch(() => api.dcCycles(100), []);
  const candF = useFetch(() => api.dcCandidates(60), []);
  const navF = useFetch(api.dcNav, []);
  const btF = useFetch(api.dcBacktest, []);

  const ov = ovF.data;
  const holding = ov?.holding ?? null;

  return (
    <>
      <div className="banner">
        <b>分红捕获（DC）</b>与 S* 并行运行的<b>独立模拟账户（初始 ¥300 万）</b>：除权前第 5 个交易日买入，
        除权后持有 30 个自然日卖出，捕获现金分红（净分红 30% 取现、余额滚入下一只）。
        历史回测<b>仅为研究归档</b>，不代表未来收益，不构成投资建议。
      </div>

      {ovF.err && (
        <div className="callout amber">
          DC 接口不可达（{String((ovF.err as any)?.message || ovF.err)}）——请确认后端已启动且已挂载
          <code> /dc</code> 路由；页面其余区块仍会尝试独立加载。
        </div>
      )}

      {/* KPI */}
      <div className="kpis k4" style={{ marginBottom: 14 }}>
        <Kpi label="总资产" n={ov?.total_equity} fmt={fmtMoney} />
        <Kpi label="可用现金" n={ov?.cash} fmt={fmtMoney} />
        <Kpi label="持仓市值" n={ov?.positions_value} fmt={fmtMoney} />
        <Kpi label="累计税前分红" n={ov?.dividend_gross} fmt={fmtMoney} />
        <Kpi label="累计净分红" n={ov?.dividend_net} fmt={fmtMoney} />
        <Kpi label="已取现（净分红 30%）" n={ov?.withdrawn} fmt={fmtMoney} />
        <Kpi label="捕获次数" n={ov?.captures} fmt={fmtInt} />
        <Kpi label="运行天数" n={ov?.running_days} fmt={fmtInt} />
      </div>

      {/* 当前动作 + 净值对比 */}
      <div className="grid" style={{ gridTemplateColumns: "1fr 1.35fr", marginBottom: 14 }}>
        <Card title="当前动作" tag={holding ? "持仓中" : "空仓"}>
          <CurrentAction ov={ov} err={ovF.err} />
        </Card>
        <Card title="净值对比（DC vs S* vs B2）" tag="实时">
          <NavChart nav={navF.data} err={navF.err} />
        </Card>
      </div>

      {/* 未来候选 */}
      <Card title="未来 60 天候选（除权窗口）" tag="实时" style={{ marginBottom: 14 }}>
        <CandidateTable rows={candF.data} loading={candF.loading} err={candF.err} />
        <div style={{ fontSize: 11, color: "var(--dim)", marginTop: 8 }}>
          资格线：TTM 股息率 ≥ 6% · 总市值 ≥ 500 亿 · 趋势非下跌（close &gt; MA120）。
          买点 = 除权前第 5 个交易日；卖出 = 除权日 +30 自然日后首个交易日。
        </div>
      </Card>

      {/* 捕获流水 */}
      <Card title="捕获流水" tag="实时" style={{ marginBottom: 14 }}>
        <CycleTable rows={cyclesF.data} loading={cyclesF.loading} err={cyclesF.err} />
      </Card>

      {/* 历史回测归档 */}
      <Card title="历史回测（研究口径）" tag="归档 · 只读 · 不代表未来">
        <BacktestPanel bt={btF.data} err={btF.err} loading={btF.loading} />
      </Card>
    </>
  );
}

function Kpi({ label, n, fmt }: { label: string; n?: NumberWithClass | null; fmt?: (v: number) => string }) {
  return (
    <div className="card kpi">
      <div className="lbl">{label}</div>
      <div className="v"><Num n={n} fmt={fmt} /></div>
    </div>
  );
}

function Item({ l, v }: { l: string; v: React.ReactNode }) {
  return (
    <div className="it">
      <div className="l">{l}</div>
      <div className="v">{v}</div>
    </div>
  );
}

function CurrentAction({ ov, err }: { ov: DcOverview | null; err: any }) {
  if (!ov) {
    return (
      <div className="skel" style={{ padding: "18px 0" }}>
        {err ? "DC 概览接口不可达（见顶部提示），无法读取持仓/下一动作。" : "加载中…"}
      </div>
    );
  }
  const h = ov.holding;
  const na = ov.next_action;
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
        {h ? (
          <>
            <b style={{ fontFamily: "var(--mono)" }}>{h.symbol}</b>
            <span>{h.name ?? "—"}</span>
            <span className="badge a">持仓中</span>
          </>
        ) : (
          <>
            <span className="badge d">空仓</span>
            <span style={{ color: "var(--muted)", fontSize: 12 }}>无持仓，等待下一个买点</span>
          </>
        )}
        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--muted)" }}>
          当日盈亏 <Num n={ov.daily_pnl} fmt={fmtMoney} />
        </span>
      </div>

      {h ? (
        <div className="dc-hold">
          <Item l="买入日" v={h.entry_date ?? "—"} />
          <Item l="除权日" v={h.ex_date} />
          <Item l="计划卖出日" v={h.planned_exit_date ?? "—"} />
          <Item l="股数" v={<Num n={live(h.shares)} fmt={fmtInt} />} />
          <Item l="成本价/股" v={<Num n={live(h.buy_price)} fmt={fmtPrice} />} />
          <Item l="市值" v={<Num n={live(h.market_value)} fmt={fmtMoney} />} />
          <Item l="税前分红" v={<Num n={live(h.gross_dividend)} fmt={fmtMoney} />} />
          <Item l="红利税" v={<Num n={live(h.tax)} fmt={fmtMoney} />} />
          <Item l="已取现" v={<Num n={live(h.withdrawn)} fmt={fmtMoney} />} />
          <Item l="每股分红" v={<Num n={live(h.dps)} fmt={fmtPrice} />} />
        </div>
      ) : na ? (
        <NextAction na={na} />
      ) : (
        <div className="skel" style={{ padding: "18px 0" }}>
          暂无候选：未来窗口内无满足「TTM 股息率 ≥ 6% · 市值 ≥ 500 亿 · 趋势非下跌」的除权事件。
        </div>
      )}
    </>
  );
}

function NextAction({ na }: { na: DcNextAction }) {
  const yieldV = numVal((na as any).div_yield);
  const capV = numVal((na as any).market_cap);
  return (
    <>
      <div className="callout g" style={{ marginTop: 0 }}>
        <b>{na.kind === "BUY" ? "下一买点（计划买入）" : "下一动作（计划卖出）"}</b>：
        <span style={{ fontFamily: "var(--mono)" }}>{na.symbol}</span> {na.name ?? ""} · 日期{" "}
        <b>{na.date ?? "—"}</b>
        {na.ex_date ? <> · 除权日 {na.ex_date}</> : null}
        {na.reason ? <span style={{ color: "var(--muted)" }}> · {na.reason}</span> : null}
      </div>
      <div className="dc-hold">
        <Item
          l="TTM 股息率"
          v={yieldV !== null ? <Num n={live(yieldV)} fmt={(v) => ratioPct(v, false)} /> : "—"}
        />
        <Item
          l="总市值"
          v={capV !== null ? <Num n={live(capV)} fmt={fmtYi} /> : "—"}
        />
        <Item l="趋势" v={<TrendBadge trend={na.trend ?? null} />} />
        <Item
          l="账户"
          v={<span style={{ fontFamily: "var(--mono)", fontSize: 11.5 }}>{na.kind === "BUY" ? "等待买点" : "等待卖出"}</span>}
        />
      </div>
    </>
  );
}

function NavChart({ nav, err }: { nav: DcNav | null; err: any }) {
  const dates = nav?.dates ?? [];
  const series = [
    { name: "DC", color: "#2DD4A7", data: nav?.dc ?? [] },
    { name: "S*", color: "#7C9CC4", data: nav?.s ?? [] },
    { name: "B2", color: "#5C6E8C", data: nav?.b2 ?? [] },
  ];
  const vals = series
    .flatMap((s) => s.data)
    .filter((v): v is number => typeof v === "number" && !Number.isNaN(v));

  if (err) {
    return <div className="skel" style={{ padding: "30px 0", textAlign: "center" }}>净值接口暂不可用（{String(err?.message || err)}）</div>;
  }
  if (dates.length < 2 || vals.length < 2) {
    return (
      <div className="skel" style={{ padding: "34px 0", textAlign: "center" }}>
        净值数据尚未生成（DC 独立账户自 2026-11-05 起前瞻运行，样本 &lt;2 个交易日时不出图）
      </div>
    );
  }

  const w = 900, h = 240, pad = 34;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || Math.max(Math.abs(max) * 0.001, 1);
  const lo = min - span * 0.08, hi = max + span * 0.08;
  const n = dates.length;
  const X = (i: number) => pad + (n <= 1 ? 0 : (i / (n - 1)) * (w - pad * 2));
  const Y = (v: number) => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);

  const segments: { i: number; v: number }[][][] = series.map((s) => {
    const out: { i: number; v: number }[][] = [];
    let cur: { i: number; v: number }[] = [];
    s.data.forEach((v, i) => {
      if (typeof v === "number" && !Number.isNaN(v)) cur.push({ i, v });
      else if (cur.length) { out.push(cur); cur = []; }
    });
    if (cur.length) out.push(cur);
    return out;
  });

  const latest = series
    .map((s) => {
      for (let i = s.data.length - 1; i >= 0; i--) {
        const v = s.data[i];
        if (typeof v === "number" && !Number.isNaN(v)) return { name: s.name, color: s.color, v };
      }
      return null;
    })
    .filter((x): x is { name: string; color: string; v: number } => x !== null);

  const yLabels = [hi, (hi + lo) / 2, lo];

  return (
    <>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: 240, display: "block" }} role="img"
        aria-label="DC / S* / B2 净值对比（前瞻实盘）">
        {yLabels.map((v, i) => (
          <g key={i}>
            <line x1={pad} y1={Y(v)} x2={w - pad} y2={Y(v)} stroke="#22304A"
              strokeDasharray={i === 1 ? "2 6" : "4 6"} />
            <text x={4} y={Y(v) + 3} fill="#5C6E8C" fontSize="10">{fmtWan(v)}</text>
          </g>
        ))}
        {series.map((s, si) =>
          segments[si].map((seg, gi) =>
            seg.length >= 2 ? (
              <polyline key={`${s.name}-${gi}`} fill="none" stroke={s.color} strokeWidth="1.6"
                points={seg.map((p) => `${X(p.i).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" ")} />
            ) : (
              <circle key={`${s.name}-${gi}`} cx={X(seg[0].i)} cy={Y(seg[0].v)} r="2" fill={s.color} />
            )
          )
        )}
        <text x={pad} y={h - 8} fill="#5C6E8C" fontSize="10">{dates[0]}</text>
        <text x={w - pad} y={h - 8} fill="#5C6E8C" fontSize="10" textAnchor="end">{dates[n - 1]}</text>
        <title>DC vs S* vs B2 净值（DC 账户前瞻运行，实时口径）</title>
      </svg>
      <div className="legend" style={{ marginTop: 8 }}>
        {latest.map((l) => (
          <span key={l.name}>
            <i style={{ background: l.color }} />
            {l.name} {fmtMoney(l.v)}
          </span>
        ))}
        <span style={{ marginLeft: "auto", color: "var(--dim)" }}>
          共 {n} 个交易日 · {dates[0]} ~ {dates[n - 1]}
        </span>
      </div>
    </>
  );
}

function fmtWan(v: number): string {
  return (v / 10000).toLocaleString("zh-CN", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }) + " 万";
}

function CandidateTable({ rows, loading, err }: { rows: DcCandidate[] | null; loading: boolean; err?: any }) {
  if (loading) return <div className="skel">加载候选…</div>;
  if (err && !rows) {
    return <div className="skel" style={{ padding: "18px 0" }}>候选接口不可达（见顶部提示），候选列表暂不可用。</div>;
  }
  const list = rows ?? [];
  if (list.length === 0) {
    return <div className="skel" style={{ padding: "18px 0" }}>未来 60 天暂无除权事件（候选为空）。</div>;
  }
  return (
    <div style={{ maxHeight: 380, overflow: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>除权日</th><th>买点日</th><th>代码</th><th>名称</th>
            <th className="num">TTM 股息率</th><th className="num">总市值</th>
            <th>趋势</th><th>是否合格</th><th className="num">排名</th>
          </tr>
        </thead>
        <tbody>
          {list.map((c) => (
            <tr key={`${c.symbol}-${c.ex_date}`}>
              <td>{c.ex_date}</td>
              <td>{c.entry_date}</td>
              <td style={{ fontFamily: "var(--mono)" }}>{c.symbol}</td>
              <td>{c.name ?? "—"}</td>
              <td className="num"><Num n={c.div_yield} fmt={(v) => ratioPct(v, false)} /></td>
              <td className="num"><Num n={c.market_cap} fmt={fmtYi} /></td>
              <td><TrendBadge trend={c.trend} /></td>
              <td>
                {c.eligible ? (
                  <span className="badge g">合格</span>
                ) : (
                  <>
                    <span className="badge r">不合格</span>
                    {c.reason ? (
                      <div style={{ fontSize: 10, color: "var(--dim)", marginTop: 2 }}>{c.reason}</div>
                    ) : null}
                  </>
                )}
              </td>
              <td className="num">
                {c.rank > 0
                  ? <Num n={live(c.rank)} fmt={(v) => "#" + Math.round(v)} />
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CycleTable({ rows, loading, err }: { rows: DcCycle[] | null; loading: boolean; err?: any }) {
  if (loading) return <div className="skel">加载捕获流水…</div>;
  if (err && !rows) {
    return <div className="skel" style={{ padding: "18px 0" }}>捕获流水接口不可达（见顶部提示），流水暂不可用。</div>;
  }
  const list = rows ?? [];
  if (list.length === 0) {
    return <div className="skel" style={{ padding: "18px 0" }}>暂无捕获记录（账户尚未闭环任何分红周期）。</div>;
  }
  return (
    <div style={{ maxHeight: 380, overflow: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>状态</th><th>代码</th><th>买入日</th><th>卖出日</th><th>除权日</th>
            <th className="num">税前分红</th><th className="num">红利税</th><th className="num">取现</th>
            <th className="num">净盈亏</th><th className="num">收益率</th>
          </tr>
        </thead>
        <tbody>
          {list.map((c) => (
            <tr key={c.cycle_id}>
              <td><StatusBadge status={c.status} /></td>
              <td>
                <span style={{ fontFamily: "var(--mono)" }}>{c.symbol}</span>
                {c.name ? <span style={{ color: "var(--dim)", fontSize: 11 }}> {c.name}</span> : null}
              </td>
              <td>{c.entry_date ?? c.planned_entry_date ?? "—"}</td>
              <td>{c.exit_date ?? "—"}</td>
              <td>{c.ex_date}</td>
              <td className="num"><Num n={live(c.gross_dividend)} fmt={fmtMoney} /></td>
              <td className="num"><Num n={live(c.tax)} fmt={fmtMoney} /></td>
              <td className="num"><Num n={live(c.withdrawn)} fmt={fmtMoney} /></td>
              <td className="num" style={{ color: (c.net_pnl ?? 0) >= 0 ? "var(--acc)" : "var(--red)" }}>
                <Num n={live(c.net_pnl)} fmt={fmtMoney} />
              </td>
              <td className="num" style={{ color: (c.return_pct ?? 0) >= 0 ? "var(--acc)" : "var(--red)" }}>
                <Num n={live(c.return_pct)} fmt={(v) => ratioPct(v)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type BtFmt = (v: number) => string;

const METRICS: { key: string; label: string; fmt: BtFmt }[] = [
  { key: "total_return", label: "总收益", fmt: (v) => ratioPct(v) },
  { key: "cagr", label: "年化收益 CAGR", fmt: (v) => ratioPct(v) },
  { key: "vol", label: "年化波动", fmt: (v) => toPct(v).toFixed(2) + "%" },
  { key: "sharpe", label: "Sharpe（rf=0）", fmt: (v) => v.toFixed(2) },
  { key: "max_drawdown", label: "最大回撤", fmt: (v) => ratioPct(v) },
  { key: "calmar", label: "Calmar", fmt: (v) => v.toFixed(2) },
  { key: "captures", label: "捕获次数", fmt: fmtInt },
  { key: "captures_per_year", label: "捕获次数/年", fmt: (v) => v.toFixed(2) },
  { key: "win_rate", label: "胜率", fmt: (v) => toPct(v).toFixed(2) + "%" },
  { key: "avg_cycle_return", label: "平均单次收益", fmt: (v) => ratioPct(v) },
  { key: "gross_dividend", label: "税前分红合计", fmt: fmtMoney },
  { key: "tax", label: "红利税合计", fmt: fmtMoney },
  { key: "withdrawn", label: "已取现", fmt: fmtMoney },
  { key: "cost", label: "交易成本", fmt: fmtMoney },
  { key: "final_nav", label: "期末净值", fmt: fmtMoney },
  { key: "total_wealth_return", label: "含取现总财富收益", fmt: (v) => ratioPct(v) },
];

function BacktestPanel({ bt, err, loading }: { bt: DcBacktest | null; err: any; loading: boolean }) {
  if (loading) return <div className="skel">加载回测归档…</div>;
  if (err || !bt) {
    return (
      <div className="callout">
        <b>回测归档暂不可用</b>（研究任务进行中或接口未就绪）。页面其余部分为 DC 前瞻账户实时数据，
        不受影响。
      </div>
    );
  }
  if (!bt.available) {
    return (
      <>
        <div className="callout">
          <b>回测归档尚未生成</b>：历史回测（2016-01-04 ~ 2026-11-06，≥500 亿全市场）仍在研究侧运行，
          完成后归档 JSON 生成即自动出现在此处。本区块不参与前瞻业绩展示。
        </div>
        <div style={{ fontSize: 11, color: "var(--dim)" }}>
          口径：归档 · 只读 · 不代表未来；若需查看研究过程，请参考 <code>research/dividend_capture/</code>。
        </div>
      </>
    );
  }

  const metrics = bt.metrics ?? {};
  const yearly = bt.yearly ?? [];
  const variants = bt.variants ?? [];
  const params = bt.params ? JSON.stringify(bt.params, null, 2) : "";

  return (
    <>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 11.5, color: "var(--muted)", marginBottom: 10 }}>
        <span>归档时点 {bt.as_of ?? "—"}</span>
        <span>样本区间 {bt.start ?? "—"} ~ {bt.end ?? "—"}</span>
        <span>股票池 {bt.universe ?? "—"}</span>
        <span className="tag amber">归档 · 只读 · 不代表未来</span>
      </div>

      <div className="dc-metrics">
        {METRICS.map((m) => (
          <div className="m" key={m.key}>
            <div className="l">{m.label}</div>
            <div className="v"><Num n={arch(metrics[m.key] as number | null | undefined)} fmt={m.fmt} /></div>
          </div>
        ))}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1.35fr", marginBottom: 12 }}>
        <div>
          <h4 style={{ margin: "0 0 6px", fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>分年度收益</h4>
          <div style={{ maxHeight: 260, overflow: "auto" }}>
            <table>
              <thead><tr><th>年份</th><th className="num">收益</th><th className="num">年末净值</th></tr></thead>
              <tbody>
                {yearly.length === 0 && (
                  <tr><td colSpan={3} className="skel">归档未提供分年度数据</td></tr>
                )}
                {yearly.map((r) => (
                  <tr key={String(r.year)}>
                    <td>{r.year}</td>
                    <td className="num"><Num n={arch(r.return)} fmt={(v) => ratioPct(v)} /></td>
                    <td className="num"><Num n={arch(r.nav)} fmt={fmtMoney} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h4 style={{ margin: "0 0 6px", fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>
            变体敏感性（研究口径）
          </h4>
          <div style={{ maxHeight: 260, overflow: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>变体</th><th className="num">CAGR</th><th className="num">总收益</th>
                  <th className="num">最大回撤</th><th className="num">捕获</th><th className="num">胜率</th>
                </tr>
              </thead>
              <tbody>
                {variants.length === 0 && (
                  <tr><td colSpan={6} className="skel">归档未提供变体数据</td></tr>
                )}
                {variants.map((v, i) => (
                  <tr key={String(v.name ?? i)}>
                    <td>{v.name ?? "—"}</td>
                    <td className="num"><Num n={arch(v.cagr)} fmt={(x) => ratioPct(x)} /></td>
                    <td className="num"><Num n={arch(v.total_return)} fmt={(x) => ratioPct(x)} /></td>
                    <td className="num"><Num n={arch(v.max_drawdown)} fmt={(x) => ratioPct(x)} /></td>
                    <td className="num"><Num n={arch(v.captures)} fmt={fmtInt} /></td>
                    <td className="num"><Num n={arch(v.win_rate)} fmt={(x) => toPct(x).toFixed(2) + "%"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {params && (
        <details style={{ marginBottom: 10 }}>
          <summary style={{ cursor: "pointer", color: "var(--muted)", fontSize: 11.5 }}>
            回测参数（只读归档）
          </summary>
          <pre>{params}</pre>
        </details>
      )}

      {bt.caveats.length > 0 && (
        <div className="callout amber" style={{ marginTop: 4 }}>
          <b>研究口径披露（必读）</b>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 12 }}>
            {bt.caveats.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}

      <div className="callout">
        <b>免责声明</b>：{bt.disclaimer || "历史回测包含幸存者偏差与执行假设，不代表未来收益，不构成投资建议。"}
        <span className="tag amber" style={{ marginLeft: 8 }}>归档 · 只读 · 不代表未来</span>
      </div>
    </>
  );
}
