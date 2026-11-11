// 数据标注类型：每个数值必带 data_class（实时/演示/估算/归档）
export type DataClass = "实时" | "演示" | "估算" | "归档";

export interface NumberWithClass {
  value: number | null;
  data_class: DataClass;
  unit?: string | null;
}

export interface ProgressItem {
  label: string;
  value: number;
  target: number | null;
  fmt?: string | null;
}

export interface Overview {
  data_date: string;
  mode: string;
  init_cash: NumberWithClass;
  total_equity: NumberWithClass;
  cash: NumberWithClass;
  positions_value: NumberWithClass;
  daily_pnl: NumberWithClass;
  unrealized_total: NumberWithClass;
  position_count: NumberWithClass;
  running_days: NumberWithClass;
  progress: ProgressItem[];
  benchmark_hint: string;
}

export interface PositionRow {
  symbol: string; name: string | null; industry_l1: string | null; bank_subtype: string | null;
  div_yield_ttm: NumberWithClass; div_yield_prev: NumberWithClass; market_value: NumberWithClass; cost_price: NumberWithClass;
  unrealized_gain: NumberWithClass; unrealized_pnl: NumberWithClass; holding_days: NumberWithClass; shares_available: NumberWithClass;
  status: string;
}

export interface SignalRow {
  trade_date: string; symbol: string; drop_20d: NumberWithClass | null;
  rsi_14: NumberWithClass | null; strength: NumberWithClass | null;
  expected_net: NumberWithClass | null; cost_gate: string; status: string; price_basis: string;
}

export interface Benchmarks {
  mode: string; running_days: number; paired_trade_count: number; need_days: number;
  need_trades: number; annual_unlocked: boolean; progress: ProgressItem[]; benchmarks: any[];
}

export interface Archive {
  kpi: Record<string, NumberWithClass>; months: string[]; s: number[]; b2: number[];
  hs300: number[]; yearly: any[]; modules: any[]; withdraw: any[]; data_class: DataClass;
  disclaimer: string;
}

export interface Withdraw {
  year: number; ytd_gross: NumberWithClass; planned_withdraw: NumberWithClass;
  next_withdraw_date: string; rule: any; history: any[];
}

export interface Config {
  strategy_yaml: string; config_hash: string; data_sources: any[];
  limitations: string[]; baseline_check: string;
}

export interface DataSourceStatus {
  name: string; role: string; status: "connected" | "configured" | "missing" | "failed";
  bars?: number; error?: string;
}
export interface Datasources {
  sources: DataSourceStatus[]; primary_connected: boolean;
}

// ---------- 分红捕获（DC）策略：与 S* 并行、独立账户 ----------
// 注意：后端 dc 接口的 money 字段是裸 number（非 NumberWithClass），前端按"实时"口径包装后再渲染。
export interface DcHolding {
  symbol: string; name: string | null;
  ex_date: string;
  entry_date: string | null;
  planned_exit_date: string | null;
  shares: number; buy_price: number; market_value: number;
  gross_dividend: number; tax: number; withdrawn: number;
  status: string; dps: number;
}

export interface DcNextAction {
  kind: "BUY" | "SELL";
  symbol: string;
  name?: string | null;
  date: string | null;
  ex_date?: string | null;
  /** 小数比率（0.067 = 6.7%），渲染需 ×100 */
  div_yield?: number | null;
  /** 单位：元 */
  market_cap?: number | null;
  trend?: string | null;
  reason?: string | null;
}

export interface DcOverview {
  data_date: string; strategy_id: string; account_id: string;
  init_cash: NumberWithClass; total_equity: NumberWithClass; cash: NumberWithClass;
  positions_value: NumberWithClass; daily_pnl: NumberWithClass; position_count: NumberWithClass;
  running_days: NumberWithClass; dividend_gross: NumberWithClass; dividend_net: NumberWithClass;
  withdrawn: NumberWithClass; captures: NumberWithClass;
  holding: DcHolding | null;
  next_action: DcNextAction | null;
  backtest_available: boolean;
}

export interface DcCycle {
  cycle_id: string; symbol: string; name: string | null;
  ex_date: string; status: string;
  planned_entry_date: string | null; planned_exit_date: string | null;
  entry_date: string | null; exit_date: string | null;
  shares: number; buy_price: number; sell_price: number | null;
  dps: number; gross_dividend: number; tax: number; withdrawn: number;
  net_dividend: number;
  net_pnl: number | null;
  /** 小数比率（0.031 = 3.1%），渲染需 ×100 */
  return_pct: number | null;
  reason: string | null;
}

export type DcTrend = "up_pull" | "up" | "nondown" | "down" | "insufficient";

export interface DcCandidate {
  symbol: string; name: string | null;
  ex_date: string; entry_date: string; exit_date: string;
  dps: number; ttm_dps: number; price: number;
  /** div_yield.value 是小数比率（0.067 = 6.7%）；unit 仅为展示提示 */
  div_yield: NumberWithClass;
  /** market_cap.value 单位：元 */
  market_cap: NumberWithClass;
  trend: DcTrend;
  eligible: boolean; reason: string; rank: number;
}

export interface DcNav {
  dates: string[];
  s: (number | null)[];
  dc: (number | null)[];
  b2: (number | null)[];
}

export interface DcConfig {
  strategy_yaml: string; config_hash: string;
  strategy_id: string; account_id: string;
  init_cash: number; start_date: string;
}

/** 回测归档：字段可能缺失（研究任务未完成时 available=false），全部按可选做防御。 */
export interface DcBacktestMetrics {
  total_return?: number | null;
  cagr?: number | null;
  vol?: number | null;
  sharpe?: number | null;
  max_drawdown?: number | null;
  calmar?: number | null;
  captures?: number | null;
  captures_per_year?: number | null;
  win_rate?: number | null;
  avg_cycle_return?: number | null;
  gross_dividend?: number | null;
  tax?: number | null;
  withdrawn?: number | null;
  cost?: number | null;
  final_nav?: number | null;
  total_wealth_return?: number | null;
  avg_hold_days?: number | null;
  skipped?: number | null;
  [key: string]: number | null | undefined;
}

export interface DcBacktestYearly {
  year: number | string;
  return?: number | null;
  nav?: number | null;
}

export interface DcBacktestVariant {
  name: string;
  cagr?: number | null;
  total_return?: number | null;
  max_drawdown?: number | null;
  captures?: number | null;
  win_rate?: number | null;
  [key: string]: number | string | null | undefined;
}

export interface DcBacktest {
  available: boolean;
  data_class: DataClass;
  as_of: string | null;
  universe: string | null;
  start: string | null;
  end: string | null;
  params: Record<string, any> | null;
  metrics: DcBacktestMetrics | null;
  yearly: DcBacktestYearly[];
  variants: DcBacktestVariant[];
  caveats: string[];
  disclaimer: string;
  raw?: any;
}

// ★ 接口前缀与后端 DA_API_PREFIX 对齐（默认 /da/v1，避免与同机其它服务 /api 冲突）
export const API_PREFIX = import.meta.env.VITE_API_PREFIX || "/da/v1";
const BASE = API_PREFIX;

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status, body });
  }
  return res.json();
}

// ★ path 不带前缀（get 内部统一拼 BASE，避免 /da/v1 双重拼接）
export const api = {
  overview: () => get<Overview>("/overview"),
  positions: () => get<PositionRow[]>("/positions"),
  signals: () => get<SignalRow[]>("/signals"),
  attribution: () => get<any>("/attribution"),
  benchmarks: (mode: "progress" | "annual") => get<Benchmarks>(`/benchmarks?mode=${mode}`),
  withdraw: () => get<Withdraw>("/withdraw"),
  config: () => get<Config>("/config"),
  archive: () => get<Archive>("/archive"),
  datasources: () => get<Datasources>("/datasources"),
  // DC 分红捕获（与 S* 并行的独立账户；全部只读）
  dcOverview: () => get<DcOverview>("/dc/overview"),
  dcCycles: (limit = 100) => get<DcCycle[]>(`/dc/cycles?limit=${limit}`),
  dcCandidates: (days = 60) => get<DcCandidate[]>(`/dc/candidates?days=${days}`),
  dcNav: () => get<DcNav>("/dc/nav"),
  dcConfig: () => get<DcConfig>("/dc/config"),
  dcBacktest: () => get<DcBacktest>("/dc/backtest"),
};
