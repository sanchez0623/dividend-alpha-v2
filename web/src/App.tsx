import React from "react";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Signals from "./pages/Signals";
import Attribution from "./pages/Attribution";
import Benchmarks from "./pages/Benchmarks";
import Withdraw from "./pages/Withdraw";
import Config from "./pages/Config";
import Archive from "./pages/Archive";
import DividendCapture from "./pages/DividendCapture";
import { useDatasources, useFetch, DataClassLegend } from "./components/ui";
import { api } from "./api";

const PAGES = [
  { key: "overview", label: "总览", icon: "◫", grp: "监控", el: <Overview /> },
  { key: "positions", label: "持仓", icon: "▤", grp: "监控", el: <Positions /> },
  { key: "signals", label: "信号", icon: "⌁", grp: "监控", el: <Signals /> },
  { key: "attribution", label: "归因", icon: "◧", grp: "验证", el: <Attribution /> },
  { key: "benchmarks", label: "基准对比", icon: "◎", grp: "验证", el: <Benchmarks /> },
  { key: "withdraw", label: "取现流水", icon: "↧", grp: "验证", el: <Withdraw /> },
  // ★ 分红捕获（DC）：与 S* 并行的独立策略账户；历史回测仅存档只读
  { key: "dc", label: "分红捕获", icon: "◆", grp: "策略", el: <DividendCapture /> },
  { key: "config", label: "配置", icon: "⚙", grp: "系统", el: <Config /> },
  { key: "archive", label: "历史归档", icon: "▣", grp: "归档", el: <Archive /> },
];

// ★ 各页副标题：归档/策略页不冒充前瞻业绩口径
const SUBTITLE: Record<string, string> = {
  archive: "研究存档 · 只读 · 不代表前瞻业绩",
  dc: "分红捕获（DC）独立账户 · 与 S* 并行前瞻运行 · 历史回测仅为归档、不代表未来",
};

export default function App() {
  const [page, setPage] = React.useState("overview");
  const ds = useDatasources();
  const { data: ov } = useFetch(api.overview, []);
  const current = PAGES.find((p) => p.key === page)!;

  // ★ 顶栏数据源状态灯：探活真值（后端未开→黄"未启动"；主源通→绿；失败→红）
  const dsPill = ds.loading
    ? { cls: "pill a", text: "数据源 · 探活中" }
    : ds.offline
      ? { cls: "pill a", text: "数据源 · 后端未启动" }
      : ds.primaryConnected
        ? { cls: "pill g", text: "数据源 · 已连通" }
        : { cls: "pill r", text: "数据源 · 主源失败" };

  // ★ 盘后任务状态真值：最新净值行的交易日（overview.data_date）
  const workerPill = ov
    ? { cls: "pill g", text: `● 盘后任务已完成 · ${ov.data_date}` }
    : { cls: "pill r", text: "盘后任务 · 未运行" };

  React.useEffect(() => {
    const handler = (e: any) => {
      if (e.detail?.page) setPage(e.detail.page);
    };
    window.addEventListener("da:navigate", handler);
    return () => window.removeEventListener("da:navigate", handler);
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          dividend-alpha
          <small>A 股高股息 · 前瞻验证 v3.0</small>
        </div>
        <nav className="nav">
          {PAGES.map((p, i) => (
            <React.Fragment key={p.key}>
              {(i === 0 || PAGES[i - 1].grp !== p.grp) && <div className="grp">{p.grp}</div>}
              <a className={page === p.key ? "on" : ""} onClick={() => setPage(p.key)}>
                <span className="ic">{p.icon}</span>
                {p.label}
                {p.key === "archive" && (
                  <span style={{ marginLeft: "auto", font: "9px var(--mono)", color: "var(--amber)" }}>
                    存档
                  </span>
                )}
              </a>
            </React.Fragment>
          ))}
        </nav>
        <div className="side-foot">
          唯一真值：docs/00_INDEX.md<br />
          参数真值：config/strategy.yaml
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="l">
            <span className="pill">数据日期 {ov ? ov.data_date : "…"}</span>
            <span className="pill g">● 前瞻运行 · 起始 2026-11-05</span>
            <span className="pill">本金 {ov && ov.init_cash.value != null ? `¥${ov.init_cash.value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}` : "…"}</span>
          </div>
          <div className="r">
            <span className={workerPill.cls}>{workerPill.text}</span>
            <span className="pill">A股 · 日线</span>
            <span className="pill">模拟盘</span>
            <span className={dsPill.cls}>{dsPill.text}</span>
          </div>
        </header>

        <div className="content">
          <h1>{current.label}</h1>
          <div className="sub">
            {SUBTITLE[page] ?? "生产系统自 2026-11-05 起前瞻运行，以下均为前瞻实盘数据"}
          </div>
          {current.el}
        </div>

        <footer className="footer">
          数值口径：无标注 = 实时 <DataClassLegend /> · dividend-alpha v3.0 · 唯一真值 docs/00_INDEX.md
        </footer>
      </div>
    </div>
  );
}
