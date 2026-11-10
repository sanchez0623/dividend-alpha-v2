import React from "react";
import { api } from "../api";
import { Card, ProgressBar, useFetch } from "../components/ui";

type Mode = "progress" | "annual";

export default function Benchmarks() {
  const [mode, setMode] = React.useState<Mode>("progress");
  const [locked, setLocked] = React.useState<{ need_days: number; need_trades: number } | null>(null);
  const { data, reload, err } = useFetch(() => api.benchmarks(mode), [mode]);

  React.useEffect(() => {
    if (err?.status === 409) {
      setLocked(err.body?.detail ?? null);
    } else {
      setLocked(null);
    }
  }, [err]);

  const unlocked = data?.annual_unlocked ?? false;

  function switchMode(m: Mode) {
    if (m === "annual" && !unlocked) {
      // 触发 API 守门拦截（后端 409）
      api.benchmarks("annual").catch(() => { /* 拦截由 err 处理 */ });
      setMode("annual");
      return;
    }
    setMode(m);
  }

  return (
    <>
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 14 }}>
        <div className="seg">
          <button className={mode === "progress" ? "on" : ""} onClick={() => switchMode("progress")}>
            前瞻进度（默认）
          </button>
          <button className={mode === "annual" ? "on" : ""} disabled={!unlocked}
            onClick={() => switchMode("annual")} title={unlocked ? "" : "样本不足"}>
            年化超额 {unlocked ? "" : "🔒"}
          </button>
        </div>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          生产运行仅 D+{data?.running_days ?? 1}；样本 &lt;3 年时不计算、不渲染年化超额字段
        </span>
      </div>

      {mode === "annual" && locked && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>样本不足，暂不展示年化超额</h3>
          <div className="callout amber">
            还需 <b>{locked.need_days}</b> 天 / <b>{locked.need_trades}</b> 笔做T配对样本<br />
            年化超额在样本 &lt;3 年时无统计意义 →{" "}
            <a className="link" onClick={() => window.dispatchEvent(new CustomEvent("da:navigate", { detail: { page: "archive" } }))}>
              查看历史基准存档
            </a>
          </div>
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: "1fr 2fr", marginBottom: 14 }}>
        <Card title="前瞻进度" tag="实时">
          {(data?.progress ?? []).map((p) => (
            <ProgressBar key={p.label} label={p.label} value={p.value} target={p.target}
              neutral={p.target === null} neg={p.target === null && p.value < 0} />
          ))}
        </Card>
        <Card title={mode === "annual" && unlocked ? "年化超额" : "B1–B6 状态"} tag="实时">
          {mode === "annual" && unlocked ? (
            <div className="callout g">样本已达标（≥1095 天且 ≥300 笔），年化超额解锁（占位：由后端填充）。</div>
          ) : (
            <table>
              <thead><tr><th>基准</th><th>定义</th><th className="num">当前净值</th><th>状态</th><th className="num">B6 样本</th></tr></thead>
              <tbody>
                {(data?.benchmarks ?? []).map((b: any) => (
                  <tr key={b.id}>
                    <td>{b.id}</td><td>{b.name}</td>
                    <td className="num">{b.nav != null ? b.nav.toLocaleString("zh-CN") : "—"}</td>
                    <td><span className={"badge " + (b.status === "未通过" ? "r" : b.data_class === "估算" ? "a" : "g")}>{b.status}</span></td>
                    <td className="num">{b.id === "B6" ? `${data?.paired_trade_count ?? 0} / 300 笔` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <div className="callout">
        <b>判定树</b>：S*−B2 &gt; 成本 <b>且</b> 显著 &gt; B6（p&lt;0.05，样本≥300）才算有效。
      </div>
    </>
  );
}
