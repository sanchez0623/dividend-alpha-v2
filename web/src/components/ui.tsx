import React from "react";
import type { DataClass, NumberWithClass } from "../api";

export function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return "¥" + v.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(digits);
}

export function fW(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return (v / 10000).toLocaleString("zh-CN", { maximumFractionDigits: 1 }) + " 万";
}

export function DataClassTag({ dc }: { dc: DataClass }) {
  // ★ 异常标注设计：实时=默认态不标（整页绿点噪音）；仅非实时口径显式打标
  if (dc === "实时") return null;
  if (dc === "归档") return <span className="tag amber">归档 · 只读</span>;
  if (dc === "估算") return <span className="tag amber">估算</span>;
  return <span className="tag">演示</span>;
}

// ★ 数值口径图例：无标注=实时；演示/估算/归档 显式打标（spec 要求非实时必须可见）
export function DataClassLegend() {
  return (
    <span style={{ color: "var(--dim)", fontSize: 11 }}>
      <span className="tag" style={{ marginLeft: 10 }}>演示</span>
      <span className="tag amber" style={{ marginLeft: 6 }}>估算</span>
      <span className="tag amber" style={{ marginLeft: 6 }}>归档 · 只读</span>
    </span>
  );
}

export function Num({ n, fmt }: { n: NumberWithClass | null | undefined; fmt?: (v: number) => string }) {
  if (!n) return <span className="skel">—</span>;
  const text = fmt ? (n.value === null ? "—" : fmt(n.value)) : n.value === null ? "—" : String(n.value);
  return (
    <span>
      {text} <DataClassTag dc={n.data_class} />
    </span>
  );
}

export function Card({ title, tag, children, style }: any) {
  return (
    <div className="card" style={style}>
      <h3>
        {title}
        {tag ? <span className="tag">{tag}</span> : null}
      </h3>
      {children}
    </div>
  );
}

export function ProgressBar({ label, value, target, neutral, neg }: {
  label: string; value: number; target: number | null; neutral?: boolean; neg?: boolean;
}) {
  let pct = 0;
  if (neutral) {
    pct = Math.min(Math.abs(value) * 10, 100);
  } else if (target) {
    pct = Math.max(0, Math.min(100, (value / target) * 100));
  }
  const cls = neg ? "fill neg" : pct >= 100 ? "fill" : "fill dim";
  const fmt =
    label === "做T配对样本" ? `${value} / 300 笔`
      : label === "运行天数" ? `${value} / 1095 天`
        : neutral ? `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`
          : String(value);
  return (
    <div className="progress">
      <div className="row">
        <span>{label}</span>
        <span className="num" style={{ fontFamily: "var(--mono)" }}>{fmt}</span>
      </div>
      <div className={"track" + (neutral ? " neutral" : "")}>
        <div className={cls} style={{ width: pct + "%" }} />
      </div>
    </div>
  );
}

export function useFetch<T>(fn: () => Promise<T>, deps: any[] = []) {
  const [data, setData] = React.useState<T | null>(null);
  const [err, setErr] = React.useState<any>(null);
  const [loading, setLoading] = React.useState(true);
  const load = React.useCallback(() => {
    setLoading(true);
    fn().then(setData).catch(setErr).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  React.useEffect(load, [load]);
  return { data, err, loading, reload: load };
}

// ★ 数据源真实探活（顶栏状态灯 / 数据源健康卡共用真值，不再写死"待连通"）
export interface DsState {
  loading: boolean;
  offline: boolean;                       // 后端不可达
  primaryConnected: boolean | null;       // 主源实测连通
  sources: { name: string; role: string; status: string; bars?: number }[];
}

export function useDatasources(): DsState {
  const [st, setSt] = React.useState<DsState>(
    { loading: true, offline: false, primaryConnected: null, sources: [] });
  React.useEffect(() => {
    let alive = true;
    import("../api").then(({ api }) => api.datasources())
      .then((d) => {
        if (alive) setSt({ loading: false, offline: false,
          primaryConnected: !!d.primary_connected, sources: d.sources || [] });
      })
      .catch(() => {
        if (alive) setSt({ loading: false, offline: true,
          primaryConnected: null, sources: [] });
      });
    return () => { alive = false; };
  }, []);
  return st;
}
