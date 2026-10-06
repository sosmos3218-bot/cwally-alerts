const TOPICS = ["cwally-port", "cwally-ops"];
const ARCHIVE_URL =
  "https://raw.githubusercontent.com/sosmos3218-bot/cwally-alerts/main/state/archive.json";

const $ = (id) => document.getElementById(id);

function kst(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
}

function prefixOf(title) {
  const m = String(title || "").match(/^\[[^\]]+\]/);
  return m ? m[0] : "";
}

async function pollTopic(topic) {
  const url = `https://ntfy.sh/${topic}/json?poll=1&since=48h`;
  const res = await fetch(url);
  if (!res.ok) return [];
  const text = await res.text();
  const out = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const msg = JSON.parse(line);
      if (!msg.message && !msg.title) continue;
      out.push({
        id: String(msg.id || `${topic}-${msg.time}`),
        topic,
        title: msg.title || "",
        message: msg.message || "",
        priority: msg.priority || 3,
        time: msg.time,
        source: "live",
      });
    } catch {}
  }
  return out;
}

async function loadArchive() {
  try {
    const res = await fetch(ARCHIVE_URL + "?t=" + Date.now());
    if (!res.ok) return { items: [], updated_at: null };
    return await res.json();
  } catch {
    return { items: [], updated_at: null };
  }
}

function merge(live, stored) {
  const map = new Map();
  for (const x of stored || []) map.set(String(x.id), { ...x, source: x.source || "git" });
  for (const x of live) map.set(String(x.id), x);
  return [...map.values()].sort((a, b) => (b.time || 0) - (a.time || 0));
}

function render(items) {
  const q = $("q").value.trim().toLowerCase();
  const topic = $("topic").value;
  const filtered = items.filter((x) => {
    if (topic && x.topic !== topic) return false;
    if (!q) return true;
    const blob = `${x.title} ${x.message} ${x.topic}`.toLowerCase();
    return blob.includes(q);
  });
  const root = $("list");
  if (!filtered.length) {
    root.innerHTML = '<div class="empty">표시할 알림이 없습니다.</div>';
    return;
  }
  root.innerHTML = filtered
    .map((x) => {
      const px = prefixOf(x.title) || x.topic;
      return `<article class="card">
        <div class="row">
          <span class="tag">${px}</span>
          <span class="tag">${x.topic}</span>
          <span class="title">${escapeHtml(x.title || "(제목 없음)")}</span>
          <span class="time">${kst(x.time)} · ${x.source}</span>
        </div>
        <pre>${escapeHtml(x.message || "")}</pre>
      </article>`;
    })
    .join("");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

let cache = [];

async function refresh() {
  $("status").textContent = "불러오는 중";
  const [arch, ...lives] = await Promise.all([loadArchive(), ...TOPICS.map(pollTopic)]);
  cache = merge(lives.flat(), arch.items || []);
  $("status").textContent = `표시 ${cache.length}건 · git ${arch.updated_at || "아직 없음"}`;
  render(cache);
}

$("q").addEventListener("input", () => render(cache));
$("topic").addEventListener("change", () => render(cache));
$("reload").addEventListener("click", refresh);
refresh();
