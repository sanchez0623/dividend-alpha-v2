import React from "react";
import { api } from "../api";
import { Card, Num, fmtPct, useFetch } from "../components/ui";

const COLS = ["日期", "标的", "20日跌幅", "RSI(14)", "强度", "预期净收益", "成本闸", "执行状态"];

export default function Signals() {
  const { data } = useFetch(api.signals, []);
  const rows = data ?? [];
  return (
    <>
      <div className="callout g">
        因子级（历史样本）：净超额 <b>+3.09%</b>，t=11.28，胜率 60.1%（2,026 样本）；
        B6 安慰剂分位 <b>75%</b>；组合层不显著。增强层标记 <b>experimental</b>，前 2-3 年可能颗粒无收。
      </div>
      <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", marginTop: 14 }}>
        <Card title="信号流水" tag="价格口径：前复权">
          <table>
            <thead><tr>{COLS.map((c) => <th key={c} className={["20日跌幅", "RSI(14)", "强度", "预期净收益"].includes(c) ? "num" : ""}>{c}</th>)}</tr></thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={COLS.length} className="skel">暂无信号（触发条件：20日跌幅≥12% 且 RSI(14)＜25；盘后管道每日评估）</td></tr>
              )}
              {rows.map((r) => (
                <tr key={r.trade_date + r.symbol}>
                  <td>{r.trade_date}</td><td>{r.symbol}</td>
                  <td className="num"><Num n={r.drop_20d} fmt={fmtPct} /></td>
                  <td className="num"><Num n={r.rsi_14} fmt={(v) => v.toFixed(1)} /></td>
                  <td className="num"><Num n={r.strength} fmt={(v) => v.toFixed(2)} /></td>
                  <td className="num"><Num n={r.expected_net} fmt={fmtPct} /></td>
                  <td><GateBadge s={r.cost_gate} /></td>
                  <td>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <div>
          <Card title="参数（config 读取，不硬编码）">
            <pre>{`overlay:
  trigger:
    kind: combo
    lookback_days: 20
    drop_threshold: 0.12
    rsi_period: 14
    rsi_threshold: 25
  holding_days: 30
  max_concurrent: 12
  position_per_trade: 0.03
  cooldown_days: 5
  min_expected_net: 0.008
  cost_multiple: 3
  price_basis: ADJ  # 前复权`}</pre>
          </Card>
          <Card title="实证状态" tag="历史样本">
            <div style={{ fontSize: 12, color: "var(--muted)" }}>
              因子级净超额 +3.09% · t=11.28<br />B6 分位 75% · 组合层不显著
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}

function GateBadge({ s }: { s: string }) {
  const map: Record<string, string> = {
    "通过": "g", "拦截": "d", "冷静期": "a", "资金不足": "r",
  };
  return <span className={"badge " + (map[s] ?? "d")}>{s}</span>;
}
