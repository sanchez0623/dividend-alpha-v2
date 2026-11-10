import React from "react";
import { api } from "../api";
import { Card, Num, fmtMoney, useFetch } from "../components/ui";

export default function Withdraw() {
  const { data } = useFetch(api.withdraw, []);
  return (
    <>
      <div className="kpis" style={{ gridTemplateColumns: "repeat(3,1fr)", marginBottom: 14 }}>
        <div className="card kpi"><div className="lbl">当年累计税前分红</div>
          <div className="v"><Num n={data?.ytd_gross} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">应取 30%</div>
          <div className="v"><Num n={data?.planned_withdraw} fmt={fmtMoney} /></div></div>
        <div className="card kpi"><div className="lbl">距 12 月最后交易日</div>
          <div className="v" style={{ fontSize: 18 }}>{data?.next_withdraw_date ?? "—"}</div></div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card title="规则定义（config，只读）">
          <pre>{`dividend_withdraw:
  enabled: true
  mode: annual
  timing: last_trading_day_of_December
  base: ytd_cash_dividend_gross
  ratio: 0.30
  shortfall_policy: cash_only
  idempotency_key: "(year, account_id)"
  skip_incomplete_year: true`}</pre>
        </Card>
        <div>
          <Card title="取现执行日志" tag="幂等 (year, account_id)">
            <table>
              <thead><tr><th>年份</th><th>执行时间</th><th className="num">应取</th><th className="num">实取</th><th className="num">兑现率</th><th>状态</th></tr></thead>
              <tbody><tr><td colSpan={6} className="skel">待首个完整年份（{data?.year ?? "—"} 进行中，skip_incomplete_year）</td></tr></tbody>
            </table>
          </Card>
          <Card title="方案对比" tag="历史样本">
            <div className="callout g">
              历史方案对比（不取现 / 每笔取 / 年底取 / 卖股补足 / 年内预留）已归入历史归档；
              <b>年底取 30% + cash_only</b> 为锁定方案（卖股补足年化差 0.04%）。
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
