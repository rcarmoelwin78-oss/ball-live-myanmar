const API_BASE = "https://raoxttvjybufllnddupi.supabase.co/functions/v1/ball-general";
const state = { matches: [], filter: "all", league: "all", query: "", configured: false, availability: new Map(), sessionToken: "" };
const list = document.querySelector("#matches");
const notice = document.querySelector("#notice");
const refreshButton = document.querySelector("#refresh");

const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[char]);

function statusOf(match) {
  const status = String(match.status || "").toLowerCase();
  if (status.includes("live")) return "live";
  if (status.includes("finish")) return "finished";
  return "upcoming";
}

function leaguePriority(name) {
  const league = String(name || "").toLowerCase();
  const priorities = [
    [/uefa champions league/, 0],
    [/^(english )?premier league$/, 1],
    [/(spanish )?la ?liga/, 2],
    [/^(italian )?serie a$/, 3],
    [/bundesliga/, 4],
    [/ligue 1/, 5],
    [/uefa europa league/, 6],
    [/uefa conference league/, 7],
    [/english fa cup/, 8],
    [/english football league cup/, 9],
    [/afc champions league elite/, 10],
    [/afc champions league two/, 11]
  ];
  return priorities.find(([pattern]) => pattern.test(league))?.[1] ?? 100;
}

function compareLeagues(a, b) {
  return leaguePriority(a) - leaguePriority(b) || String(a).localeCompare(String(b));
}

function render() {
  const shown = state.matches.filter((match) => {
    if (state.league !== "all" && String(match.league || "Other") !== state.league) return false;
    const haystack = `${match.home_name || ""} ${match.away_name || ""} ${match.league || ""}`.toLowerCase();
    if (state.query && !haystack.includes(state.query)) return false;
    if (state.filter === "all") return true;
    if (state.filter === "available") return state.availability.get(String(match.id)) === true;
    return statusOf(match) === state.filter;
  });
  notice.hidden = true;
  if (!shown.length) {
    list.innerHTML = '<div class="empty">ဒီနေရာမှာ ပြသစရာပွဲ မရှိသေးပါဘူး။</div>';
    return;
  }
  const groups = shown.slice(0, 120).reduce((result, match) => {
    const league = String(match.league || "Other competitions");
    (result[league] ||= []).push(match);
    return result;
  }, {});
  let cardIndex = 0;
  list.innerHTML = Object.entries(groups).sort(([leagueA], [leagueB]) => compareLeagues(leagueA, leagueB)).map(([league, matches]) => `<section class="league-group"><h2 class="league-title">${escapeHtml(league)} <span>${matches.length} ပွဲ</span></h2>${matches.sort((a, b) => ({live: 0, upcoming: 1, finished: 2}[statusOf(a)] - {live: 0, upcoming: 1, finished: 2}[statusOf(b)])).map((match) => {
    const index = cardIndex++;
    const status = statusOf(match);
    const time = (match.time || "—").slice(0, 5);
    const available = state.availability.get(String(match.id));
    const canCheck = state.configured && !match.demo && status === "live" && available !== false;
    const availabilityLabel = status !== "live" ? (status === "upcoming" ? "ပွဲစချိန် စစ်ဆေးမည်" : "ပွဲပြီးဆုံး") : (available === true ? "ကြည့်လို့ရပြီ" : available === false ? "STREAM မရှိ" : "နှိပ်ပြီး စစ်ကြည့်ရန်");
    const availabilityClass = available === true ? "yes" : available === false ? "no" : "";
    const buttonLabel = available === true ? "တိုက်ရိုက်ကြည့်မယ်" : status === "live" ? "စစ်ပြီးကြည့်မယ်" : status === "upcoming" ? "ပွဲမစသေးပါ" : "ပွဲပြီးပါပြီ";
    const actionMarkup = status === "live" && available === false ? "" : `<button class="watch" data-watch="${escapeHtml(match.id)}" data-title="${escapeHtml(match.home_name)} vs ${escapeHtml(match.away_name)}" data-league="${escapeHtml(match.league)}" ${canCheck ? "" : "disabled"}>${buttonLabel}</button>`;
    return `
      <article class="match" data-index="${String(index + 1).padStart(2, "0")}">
        <div class="match-head">
          <strong title="${escapeHtml(match.league)}">${escapeHtml(match.league || "Football")}</strong>
          <span class="status ${status}">${status === "live" ? "● တိုက်ရိုက်" : status === "finished" ? "ပြီးဆုံး" : "မကြာမီ"}</span>
        </div>
        <div class="availability ${availabilityClass}">${availabilityLabel}</div>
        <div class="teams">
          <div class="team">
            <img src="${escapeHtml(match.home_flag)}" alt="" loading="lazy" />
            <span>${escapeHtml(match.home_name)}</span>
          </div>
          <div class="score"><strong>${escapeHtml(match.score || "vs")}</strong><small>${escapeHtml(time)}</small></div>
          <div class="team away">
            <img src="${escapeHtml(match.away_flag)}" alt="" loading="lazy" />
            <span>${escapeHtml(match.away_name)}</span>
          </div>
        </div>
        ${actionMarkup}
      </article>`;
  }).join("")}</section>`).join("");
  document.querySelectorAll("[data-watch]:not(:disabled)").forEach((button) => button.addEventListener("click", () => openPlayer(button)));
}

async function loadHlsLibrary() {
  if (window.Hls) return;
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js";
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

async function openPlayer(button) {
  const modal = document.querySelector("#player-modal");
  const stage = document.querySelector("#player-stage");
  document.querySelector("#player-title").textContent = button.dataset.title;
  document.querySelector("#player-league").textContent = button.dataset.league;
  stage.innerHTML = '<div class="player-message">Preparing official stream…</div>';
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  try {
    const response = await fetch(`${API_BASE}/playback?id=${encodeURIComponent(button.dataset.watch)}`, { cache: "no-store", headers: state.sessionToken ? { Authorization: `Bearer ${state.sessionToken}` } : {} });
    const playback = await response.json();
    if (response.status === 429) {
      const error = new Error("API limit reached. Please try again later.");
      error.retryLater = true;
      throw error;
    }
    if (!response.ok) throw new Error(playback.message || "Stream unavailable");
    const url = playback.url || playback.playbackUrl || playback.stream_url;
    const embedUrl = playback.embedUrl || playback.embed_url;
    if (embedUrl) {
      stage.innerHTML = `<iframe src="${escapeHtml(embedUrl)}" allow="autoplay; encrypted-media; fullscreen" allowfullscreen title="Official match player"></iframe>`;
      return;
    }
    if (!url) throw new Error("This match stream is not available from the provider yet.");
    state.availability.set(String(button.dataset.watch), true);
    render();
    stage.innerHTML = '<video controls autoplay playsinline></video>';
    const video = stage.querySelector("video");
    if (url.includes(".m3u8") && !video.canPlayType("application/vnd.apple.mpegurl")) {
      await loadHlsLibrary();
      if (!window.Hls.isSupported()) throw new Error("This browser cannot play the official HLS stream");
      const hls = new window.Hls();
      hls.loadSource(url);
      hls.attachMedia(video);
    } else {
      video.src = url;
    }
  } catch (error) {
    if (!error.retryLater) {
      state.availability.set(String(button.dataset.watch), false);
      render();
    }
    stage.innerHTML = `<div class="player-message">${escapeHtml(error.message || "Official stream is unavailable right now.")}</div>`;
  }
}

function closePlayer() {
  document.querySelector("#player-modal").hidden = true;
  document.querySelector("#player-stage").innerHTML = "";
  document.body.style.overflow = "";
}

async function loadMatches() {
  refreshButton.disabled = true;
  notice.hidden = false;
  notice.textContent = "ပွဲစာရင်း ရယူနေပါတယ်…";
  list.innerHTML = "";
  try {
    const response = await fetch(`${API_BASE}/matches`, { cache: "no-store", headers: state.sessionToken ? { Authorization: `Bearer ${state.sessionToken}` } : {} });
    if (!response.ok) throw new Error("Request failed");
    const payload = await response.json();
    state.matches = Array.isArray(payload) ? payload : (payload.result || payload.matches || payload.data || []);
    const leagueSelect = document.querySelector("#league-filter");
    const leagues = [...new Set(state.matches.map((match) => String(match.league || "Other competitions")))].sort(compareLeagues);
    leagueSelect.innerHTML = '<option value="all">League အားလုံး</option>' + leagues.map((league) => `<option value="${escapeHtml(league)}">${escapeHtml(league)}</option>`).join("");
    leagueSelect.value = state.league;
    state.configured = payload.configured !== false && !payload.demo;
    document.querySelector(".live-clock").classList.toggle("connected", state.configured);
    document.querySelector("#provider-state").textContent = state.configured ? "Official provider connected" : "Demo mode";
    document.querySelector("#total-count").textContent = state.matches.length;
    document.querySelector("#live-count").textContent = state.matches.filter((match) => statusOf(match) === "live").length;
    render();
  } catch {
    notice.hidden = false;
    notice.textContent = "ပွဲစာရင်း မရသေးပါ။ ခဏနေပြီး ပြန်စစ်ကြည့်ပါ။";
  } finally {
    refreshButton.disabled = false;
  }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item === tab);
    item.setAttribute("aria-selected", item === tab ? "true" : "false");
  });
  state.filter = tab.dataset.filter;
  render();
}));

function startRefreshCooldown(seconds = 60) {
  let remaining = seconds;
  refreshButton.disabled = true;
  const original = refreshButton.innerHTML;
  refreshButton.innerHTML = `↻ <span>${remaining}s</span>`;
  const timer = setInterval(() => {
    remaining -= 1;
    refreshButton.innerHTML = `↻ <span>${remaining}s</span>`;
    if (remaining <= 0) {
      clearInterval(timer);
      refreshButton.disabled = false;
      refreshButton.innerHTML = original;
    }
  }, 1000);
}

refreshButton.addEventListener("click", async () => { await loadMatches(); startRefreshCooldown(); });
document.querySelector("#league-filter").addEventListener("change", (event) => { state.league = event.target.value; render(); });
document.querySelector("#match-search").addEventListener("input", (event) => { state.query = event.target.value.trim().toLowerCase(); render(); });
document.querySelectorAll("[data-close-player]").forEach((button) => button.addEventListener("click", closePlayer));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePlayer(); });
setInterval(() => {
  document.querySelector("#clock").textContent = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Yangon", hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(new Date()) + " MMT";
}, 1000);
async function beginMiniApp() {
  const telegram = window.Telegram?.WebApp;
  telegram?.ready();
  telegram?.expand();
  try {
    const response = await fetch(`${API_BASE}/auth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: telegram?.initData || "" })
    });
    const result = await response.json();
    if (!response.ok || !result.allowed) {
      document.querySelector("#gate-loading").hidden = true;
      document.querySelector("#gate-locked").hidden = false;
      document.querySelector("#payment-text").textContent = result.payment_text || result.message || "Subscription သက်တမ်းမရှိပါ";
      return;
    }
    state.sessionToken = result.token || "";
    document.querySelector("#access-gate").hidden = true;
    document.querySelector("#app-shell").hidden = false;
    await loadMatches();
    startRefreshCooldown();
  } catch {
    document.querySelector("#gate-loading").textContent = "ဝင်ခွင့်စစ်ဆေး၍မရသေးပါ။ ခဏနေပြီး ပြန်ဖွင့်ပါ။";
  }
}

document.querySelector("#close-mini-app").addEventListener("click", () => window.Telegram?.WebApp?.close());
beginMiniApp();
