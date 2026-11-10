import React from "react";
import { api } from "../api";
import { Card, Num, useFetch } from "../components/ui";

export default function Attribution() {
  const { data } = useFetch(api.attribution, []);
  return (
    <>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 14 }}>
        <Card title="前瞻模块贡献" tag="实时">
          {data ? (
            <div>
              <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 8 }}>
                已运行 {data.running_days.value} 天
              </div>
              {data.modules.map((m: any) => (
                <div className="progress" key={m.name}>
                  <div className="row"><span>{m.name}</span><span className="num">待积累</span></div>
                  <div className="track"><div className="fill dim" style={{ width: "4%" }} /></div>
                </div>
              ))}
            </div>
          ) : <div className="skel">加载中…</div>}
        </Card>
        <Card title="现金流拆解" tag="实时">
          {data ? (
            <table>
              <tbody>
                <tr><td>分红到账（净）</td><td className="num"><Num n={data.cashflow.dividend_net} fmt={(v) => "¥" + v.toLocaleString("zh-CN")} /></td></tr>
                <tr><td>交易成本</td><td className="num"><Num n={data.cashflow.trading_cost} fmt={(v) => "¥" + v.toLocaleString("zh-CN")} /></td></tr>
                <tr><td>红利税</td><td className="num"><Num n={data.cashflow.dividend_tax} fmt={(v) => "¥" + v.toLocaleString("zh-CN")} /></td></tr>
                <tr><td>逆回购利息</td><td className="num"><Num n={data.cashflow.repo_interest} fmt={(v) => "¥" + v.toLocaleString("zh-CN")} /></td></tr>
              </tbody>
            </table>
          ) : <div className="skel">加载中…</div>}
        </Card>
      </div>
      <Card title="分年度收益" tag="样本 &lt; 3 年">
        <div className="callout">
          样本不足 3 年，分年度柱状图暂以占位展示，<b>不显示年化</b>。
          历史分年度见 <a className="link" onClick={() => window.dispatchEvent(new CustomEvent("da:navigate", { detail: { page: "archive" } }))}>历史归档</a>。
        </div>
      </Card>
    </>
  );
}
