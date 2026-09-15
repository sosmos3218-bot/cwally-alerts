const SQUARE = "402340.KS";
const HYNIX = "000660.KS";
const SHARES_SQ = 131923998;
const SHARES_HX = 146128233;
const OTHER_NAV = 4.84e12;
const topic = process.env.NTFY_TOPIC || "cwally-port";
const server = (process.env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, "");
const force = process.env.FORCE_ALERT === "1";

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
const pct = (x) => (x == null ? "n/a" : `${(x * 100).toFixed(1)}%`);

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
const msg = [
  `[${status}] 할인 ${(discount * 100).toFixed(1)}%`,
  `스쿈어 ${Math.round(sq.last).toLocaleString()} / 하이닉스 ${Math.round(hx.last).toLocaleString()}`,
  `조건1 사이클 ${cycle} 하이닉스 20일 ${pct(hx.ret20)} 60일고점 ${pct(hx.dd60)}`,
  `조건2 상대 ${rel} 20일스프레드 ${spread20 == null ? "n/a" : (spread20 * 100).toFixed(1) + "p"}`,
].join("\n");
console.log(msg, "topic", topic);
if (status !== "HOLD" || force) {
  await ntfy(`[SQUARE] ${force && status === "HOLD" ? "TEST " + status : status}`, msg, status === "SELL" ? 5 : 4);
} else {
  console.log("NO_ALERT");
}
