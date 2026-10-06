const SQUARE = "402340.KS";
const HYNIX = "000660.KS";
const SHARES_SQ = 131923998;
const SHARES_HX = 146128233;
const OTHER_NAV = 4.84e12;
const topic = "cwally-port";
const server = (process.env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, "");
const force = process.env.FORCE_ALERT === "1";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const STATE_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "state", "square.json");
const loadState = () => {
  try {
    return existsSync(STATE_PATH) ? JSON.parse(readFileSync(STATE_PATH, "utf8")) : {};
  } catch {
    return {};
  }
};
const kstDay = () => new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);

async function chart(symbol) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?range=3mo&interval=1d`;
  const res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 cwally-alerts" } });
  if (!res.ok) throw new Error(`${symbol} ${res.status}`);
  const json = await res.json();
  const r = json.chart.result[0];
  const closes = r.indicators.quote[0].close.filter((x) => x != null);
  const last = closes.at(-1);
  const ret = (n) => (closes.length > n ? last / closes.at(-1 - n) - 1 : null);
  const last60 = closes.slice(-60);
  return { last, ret20: ret(20), dd60: last / Math.max(...last60) - 1 };
}
const pct = (x) => (x == null ? "없음" : `${(x * 100).toFixed(1)}%`);

async function ntfy(title, message, priority) {
  const res = await fetch(server, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic, title, message, priority, tags: ["chart_with_upwards_trend"] }),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`ntfy ${res.status} ${text}`);
  console.log("ntfy ok", text);
}

const sq = await chart(SQUARE);
const hx = await chart(HYNIX);
const nav = (hx.last * SHARES_HX + OTHER_NAV) / SHARES_SQ;
const discount = 1 - sq.last / nav;
const spread20 = sq.ret20 != null && hx.ret20 != null ? sq.ret20 - hx.ret20 : null;
let cycle = "quiet";
if (hx.dd60 <= -0.25 && hx.ret20 <= -0.15) cycle = "SELL";
else if (hx.dd60 <= -0.18 || hx.ret20 <= -0.08) cycle = "WATCH";
let rel = "quiet";
if (hx.ret20 != null && hx.ret20 < 0 && spread20 != null) {
  if (spread20 <= -0.08) rel = "SELL";
  else if (spread20 <= -0.04) rel = "WATCH";
}
const status = cycle === "SELL" || rel === "SELL" ? "SELL" : cycle === "WATCH" || rel === "WATCH" ? "WATCH" : "HOLD";

const statusKo = { SELL: "매도 검토", WATCH: "주의 관찰", HOLD: "그대로 보유" }[status];
const cycleKo = { SELL: "하이닉스 강한 조정", WATCH: "하이닉스 조정 조짐", quiet: "하이닉스 평온" }[cycle];
const relKo = { SELL: "스퀘어가 하이닉스보다 확연히 약함", WATCH: "스퀘어가 조금 처짐", quiet: "상대약세 평온" }[rel];
const doNow = {
  SELL: "SK스퀘어 비중 축소를 검토하세요. 하이닉스 조정이나 스퀘어 소외가 함께 나타난 상태입니다.",
  WATCH: "지금 팔지는 말고, 내일 재검토만 하세요. 추가 매수는 하지 마세요.",
  HOLD: "그대로 들고 계세요. 오늘은 팔 신호가 아닙니다.",
}[status];
const title = {
  SELL: "[SQUARE] 비중 축소 검토",
  WATCH: "[SQUARE] 주의 | 추가매수 보류",
  HOLD: "[SQUARE] 보유 유지",
}[status];

const msg = [
  `이게 뭐 알림: SK스퀘어가 하이닉스보다 너무 약해지면 줄일지 보는 점검입니다.`,
  `지금 상태: ${statusKo}. 스퀘어 ${Math.round(sq.last).toLocaleString()}원, 하이닉스 ${Math.round(hx.last).toLocaleString()}원, 잠재 NAV 대비 할인 ${(discount * 100).toFixed(1)}%.`,
  `지금 할 일: ${doNow}`,
  `나중에: 하이닉스 20일 ${pct(hx.ret20)}, 60일 고점 대비 ${pct(hx.dd60)}. 스퀘어-하이닉스 20일 차이 ${spread20 == null ? "없음" : (spread20 * 100).toFixed(1) + "%p"}.`,
  `익절/줄일 때: 조건1 하이닉스 사이클(${cycleKo}), 조건2 상대약세(${relKo}). 둘 다 SELL이면 우선 줄입니다.`,
  `하지 말 것: 할인율만 보고 추격매수. 이 알림은 매수 신호가 아닙니다.`,
].join("\n");
console.log(msg, "topic", topic);
// dedupe: send only when the status changes (SELL re-reminds after 3 days); HOLD never sends.
const prev = loadState();
const today = kstDay();
const daysSince = prev.last_sent_date ? (Date.parse(today) - Date.parse(prev.last_sent_date)) / 86400e3 : Infinity;
const changed = status !== prev.last_status;
const remind = status === "SELL" && daysSince >= 3;
let sent = false;
if (force || (status !== "HOLD" && (changed || remind))) {
  const shown = force && status === "HOLD" ? "[SQUARE] 테스트 | 보유 유지" : title;
  await ntfy(shown, msg, status === "SELL" ? 5 : 4);
  sent = true;
} else {
  console.log(status === "HOLD" ? "NO_ALERT" : `NO_ALERT (same status ${status} already sent ${prev.last_sent_date})`);
}
writeFileSync(
  STATE_PATH,
  JSON.stringify(
    {
      updated_at: today,
      last_status: status,
      last_sent_date: sent ? today : prev.last_sent_date || null,
      discount: Number(discount.toFixed(4)),
      spread20,
    },
    null,
    2,
  ) + "\n",
);
