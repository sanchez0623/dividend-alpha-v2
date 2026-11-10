import React from "react";
import { api } from "../api";
import { Card, Num, fmtMoney, fmtPct, useFetch } from "../components/ui";

const COLS = ["代码", "名称", "行业L1", "银行子类", "股息率TTM", "持仓市值", "成本价",
  "浮盈金额", "浮盈率", "持有天数", "可用股数", "状态"];

export default function Positions() {
  const { data } = useFetch(api.positions, []);
  const rows = data ?? [];
  return (
    <>
      <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", marginBottom: 14 }}>
        <Card title="持仓明细（10 只 · 等权）" tag="价格口径：前复权">
          <table>
            <thead>
              <tr>{COLS.map((c) => <th key={c} className={["股息率TTM", "持仓市值", "成本价", "浮盈金额", "浮盈率", "持有天数", "可用股数"].includes(c) ? "num" : ""}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={COLS.length} className="skel">待数据管道（初期为演示占位）</td></tr>
              )}
              {rows.map((r) => (
                <tr key={r.symbol}>
                  <td>{r.symbol}</td><td>{r.name ?? "—"}</td><td>{r.industry_l1 ?? "—"}</td>
                  <td>{r.bank_subtype ?? "—"}</td>
                  <td className="num"><Num n={r.div_yield_ttm} fmt={(v) => fmtPct(v * 100)} /></td>
                  <td className="num"><Num n={r.market_value} fmt={fmtMoney} /></td>
                  <td className="num"><Num n={r.cost_price} /></td>
                  <td className="num" style={{ color: (r.unrealized_pnl.value ?? 0) >= 0 ? "var(--acc)" : "var(--red)" }}>
                    {(r.unrealized_pnl.value ?? 0) >= 0 ? "+" : "-"}¥{Math.abs(r.unrealized_pnl.value ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}
                  </td>
                  <td className="num"><Num n={r.unrealized_gain} fmt={fmtPct} /></td>
                  <td className="num"><Num n={r.holding_days} fmt={(v) => `${Math.round(v)}天（${(v / 365).toFixed(2)}年）`} /></td>
                  <td className="num"><Num n={r.shares_available} fmt={(v) => String(v)} /></td>
                  <td><StatusBadge s={r.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <div>
          <Card title="行业分散" tag="L1≤1 · 银行子类≤2">
            <div style={{ color: "var(--muted)", fontSize: 12 }}>10 只 · 9 类 L1 · 银行按子类占 2 席</div>
            <div className="callout">「涨了就换」已四次证伪：<b>换出越主动，结果越差</b>。换入按分位排序会跌到 3.21%（按股息率则 9.76%）。</div>
          </Card>
          <Card title="卖出规则" tag="锁定">
            <div style={{ fontSize: 12, color: "var(--muted)" }}>
              硬规则：股息率&lt;1.5% / 分红中断 / 持有满 5 年 → 全部卖出<br />
              超涨：浮盈 ≥50% → 一次性清仓<br />
              禁止：轮动 / 条件换仓 / 强制换仓 / 趋势保护
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}

function StatusBadge({ s }: { s: string }) {
  const map: Record<string, string> = {
    "持有": "g", "观察": "d", "超涨预警": "a", "待卖出": "r", "停牌": "d",
  };
  return <span className={"badge " + (map[s] ?? "d")}>{s}</span>;
}
