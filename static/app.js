/* software factory — no frameworks, one small script.
   Four jobs, all optional per page:
   1. tick every [data-started-epoch] elapsed counter once a second (no fetches);
   2. refresh every [data-ts] relative timestamp ("4 min ago") and put the full
      datetime in its tooltip, in the browser's own timezone;
   3. filter the archived table as you type (client-side, no fetches);
   4. poll the [data-live] region's fragment endpoint every 4s and swap it in
      only when the content actually changed — no full reloads, ever. */

(() => {
  "use strict";

  // 1. elapsed ticker — same h:mm:ss / mm:ss format the server prints
  const fmt = (secs) => {
    secs = Math.max(0, Math.floor(secs));
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    const two = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
  };
  const tick = () => {
    const now = Math.floor(Date.now() / 1000);
    document.querySelectorAll("[data-started-epoch]").forEach((el) => {
      const start = Number(el.dataset.startedEpoch);
      if (start > 0) el.textContent = fmt(now - start);
    });
  };
  setInterval(tick, 1000);
  tick();

  // 2. relative timestamps — text mirrors the server's format, tooltip localizes
  const ago = (ts) => {
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 45) return "just now";
    if (s < 90) return "1 min ago";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 7200) return "1 hour ago";
    if (s < 86400) return `${Math.floor(s / 3600)} hours ago`;
    if (s < 172800) return "yesterday";
    return `${Math.floor(s / 86400)} days ago`;
  };
  const tickRel = () => {
    document.querySelectorAll("[data-ts]").forEach((el) => {
      const ts = Number(el.dataset.ts);
      if (ts > 0) {
        el.textContent = ago(ts);
        el.title = new Date(ts * 1000).toLocaleString();
      }
    });
  };
  setInterval(tickRel, 15000);
  tickRel();

  // 3. archived table filter
  const input = document.querySelector("[data-archive-search]");
  const rows = document.querySelectorAll("#archive-rows tr[data-search]");
  if (input && rows.length) {
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      rows.forEach((tr) => {
        tr.hidden = !!q && !tr.dataset.search.includes(q);
      });
    });
  }

  // 4. live region polling — swap only on change, keep open <details> open
  const region = document.querySelector("[data-live]");
  if (!region) return;
  let lastHash = "";
  // djb2: tiny, no crypto; its only job is "did this text change since last poll"
  const hash = (text) => {
    let h = 5381;
    for (let i = 0; i < text.length; i++) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0;
    return `${text.length}:${h}`;
  };
  const openDetails = () =>
    new Set(Array.from(region.querySelectorAll("details[open]")).map((d) => d.id));
  const poll = async () => {
    let html;
    try {
      const res = await fetch(region.dataset.fragmentSrc, {
        headers: { Accept: "text/html" },
      });
      if (!res.ok) return; // 404/500: keep what is on screen, retry next tick
      html = await res.text();
    } catch {
      return; // transient network error — the next tick retries
    }
    const h = hash(html);
    if (h === lastHash) return;
    lastHash = h;
    const wasOpen = openDetails();
    region.innerHTML = html;
    if (wasOpen.size) {
      region.querySelectorAll("details").forEach((d) => {
        if (wasOpen.has(d.id)) d.open = true;
      });
    }
    tick(); // fresh fragment means fresh counters and timestamps
    tickRel();
  };
  setInterval(poll, 4000);
})();
