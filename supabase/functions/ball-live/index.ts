import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "@supabase/supabase-js";

const projectUrl = Deno.env.get("SUPABASE_URL")!;
const secretKeys = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
const serviceKey = secretKeys.default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const db = createClient(projectUrl, serviceKey, { auth: { persistSession: false } });
const encoder = new TextEncoder();
const jsonHeaders = { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*", "access-control-allow-headers": "authorization, content-type, x-telegram-init-data" };

function json(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: jsonHeaders }); }
function text(body: string, status = 200) {
  return new Response(new Blob([body], { type: "text/html; charset=utf-8" }), { status });
}
function base(req: Request) { const url = new URL(req.url); return `${url.origin}/functions/v1/ball-live`; }
function secret(name: string) { return Deno.env.get(name)?.trim() || ""; }
function authorizedAdmin(req: Request) { const value = secret("ADMIN_API_TOKEN"); return Boolean(value) && req.headers.get("authorization") === `Bearer ${value}`; }

async function hex(buffer: ArrayBuffer) { return [...new Uint8Array(buffer)].map((v) => v.toString(16).padStart(2, "0")).join(""); }
async function hmac(key: string | ArrayBuffer, value: string) {
  const cryptoKey = await crypto.subtle.importKey("raw", typeof key === "string" ? encoder.encode(key) : key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(value));
}
async function telegramUser(req: Request) {
  const initData = req.headers.get("x-telegram-init-data") || "";
  const token = secret("TELEGRAM_BOT_TOKEN");
  if (!initData || !token) return null;
  const params = new URLSearchParams(initData); const received = params.get("hash") || ""; params.delete("hash");
  const check = [...params.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${k}=${v}`).join("\n");
  const key = await hmac("WebAppData", token);
  if (received !== await hex(await hmac(key, check))) return null;
  try { return JSON.parse(params.get("user") || "null"); } catch { return null; }
}
async function activeMember(req: Request) {
  const user = await telegramUser(req); if (!user?.id) return null;
  const { data } = await db.from("members").select("expires_at").eq("telegram_user_id", String(user.id)).maybeSingle();
  return data && new Date(data.expires_at) > new Date() ? user : null;
}
async function telegram(method: string, payload: Record<string, unknown>) {
  const token = secret("TELEGRAM_BOT_TOKEN"); if (!token) throw new Error("Bot token is not configured");
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) throw new Error(`Telegram ${method} failed`); return await response.json();
}
function menu(req: Request) {
  const rows: unknown[][] = [[{ text: "🔴 Live ဝယ်မည်", callback_data: "buy_live" }, { text: "📺 Live ကြည့်မည်", web_app: { url: `${base(req)}/` } }]];
  const channel = secret("CHANNEL_URL"); if (channel.startsWith("https://t.me/")) rows.push([{ text: "📢 Channel ဝင်မည်", url: channel }]);
  return { inline_keyboard: rows };
}
async function recordMessage(sender: any, message: any) {
  if (!sender?.id) return;
  const display_name = [sender.first_name, sender.last_name].filter(Boolean).join(" ");
  await db.from("payment_requests").upsert({ telegram_user_id: String(sender.id), display_name, received_at: new Date().toISOString() });
  let message_type = "text", body = message?.text || "", file_id = "";
  if (message?.photo?.length) { message_type = "photo"; file_id = message.photo.at(-1).file_id; body = message.caption || ""; }
  else if (message?.document) { message_type = "document"; file_id = message.document.file_id; body = message.caption || ""; }
  if (body || file_id) await db.from("customer_messages").insert({ telegram_user_id: String(sender.id), message_type, body, file_id });
}
async function botWebhook(req: Request) {
  if (req.headers.get("x-telegram-bot-api-secret-token") !== secret("TELEGRAM_WEBHOOK_SECRET")) return json({ error: "Unauthorized" }, 401);
  const update = await req.json(); const callback = update.callback_query; const message = update.message || callback?.message;
  const sender = callback?.from || message?.from; const chat = message?.chat;
  if (!sender?.id || !chat?.id) return json({ ok: true });
  if (!callback) await recordMessage(sender, message);
  if (callback?.id) await telegram("answerCallbackQuery", { callback_query_id: callback.id });
  const wantsBuy = callback?.data === "buy_live" || ["live ဝယ်မည်", "ဝယ်မည်", "buy"].includes((message?.text || "").trim().toLowerCase());
  const pay = ["💳 တစ်လ Live Pass — 5,000 Ks (30 days)", `KPay: ${secret("KPAY_NUMBER") || "admin ထံမေးပါ"}`, secret("KPAY_ACCOUNT_NAME") ? `အမည်: ${secret("KPAY_ACCOUNT_NAME")}` : "", "", "1. KPay သို့ ငွေလွှဲပါ", "2. Payment screenshot ကို ဒီ bot ထဲသို့ပို့ပါ", "3. Admin အတည်ပြုပြီးလျှင် 🔴 Watch Live ခလုတ် ပြန်ပို့ပေးပါမယ်။"].filter(Boolean).join("\n");
  const reply = message?.photo || message?.document ? "✅ Payment screenshot ကိုလက်ခံပြီးပါပြီ။ Admin စစ်ဆေးပြီးလျှင် Watch Live ခလုတ်ကိုပို့ပေးပါမယ်။" : wantsBuy ? pay : "⚽ Ball General Live Myanmar မှ ကြိုဆိုပါတယ်။\n\nလိုချင်တာကို အောက်ကခလုတ်တစ်ခုနှိပ်ပါ။";
  await telegram("sendMessage", { chat_id: chat.id, text: reply, reply_markup: menu(req) }); return json({ ok: true });
}

const style = `<style>:root{--g:#31dc82;--i:#eef4f0;--m:#99a69f}*{box-sizing:border-box}body{margin:0;background:#090d0b;color:var(--i);font-family:Arial,sans-serif}.app{max-width:650px;margin:auto;min-height:100vh;padding:20px 16px}.brand{font-weight:900}.hero,.card{margin-top:24px;padding:20px;border:1px solid #ffffff18;border-radius:20px;background:#14271d}.hero small{color:var(--g);font-weight:900}.hero h1{margin:9px 0}.grid{display:grid;gap:10px}.match,.btn{width:100%;padding:15px;border:0;border-radius:14px;background:#16221c;color:var(--i);font:inherit;text-align:left;cursor:pointer}.match{border:1px solid #ffffff18}.match b{display:block;margin:5px 0}.green{background:var(--g);color:#062315;text-align:center;font-weight:900}.muted{color:var(--m);font-size:13px}.empty{padding:35px;text-align:center;color:var(--m)}input{width:100%;padding:13px;margin:6px 0;border:1px solid #ffffff20;border-radius:10px;background:#0d120f;color:var(--i)}label{display:block;margin-top:14px;font-size:12px;font-weight:700}.notice{padding:12px;background:#132019;border-radius:10px;margin-top:12px}.back{color:var(--g);text-decoration:none}.player{width:100%;aspect-ratio:16/9;background:#000;border-radius:16px;margin-top:18px}video{width:100%;height:100%}</style>`;
const indexPage = `${style}<main class="app"><div class="brand">⚽ BALL LIVE MYANMAR</div><section class="hero"><small>PREMIUM CHANNEL</small><h1>Live football, one tap away.</h1><p class="muted">Telegram အတွင်းကနေ တိုက်ရိုက်ကြည့်ရှုနိုင်ပါသည်။</p></section><h2>Live matches</h2><div id="matches" class="grid"><div class="empty">Loading…</div></div></main><script>const init=window.Telegram?.WebApp?.initData||'';window.Telegram?.WebApp?.ready();window.Telegram?.WebApp?.expand();fetch('api/matches',{headers:{'X-Telegram-Init-Data':init}}).then(async r=>{if(!r.ok)throw Error((await r.json()).error);return r.json()}).then(ms=>{document.querySelector('#matches').innerHTML=ms.length?ms.map(m=>'<button class="match" onclick="location.href=\'watch?id='+m.id+'\'"><span class="muted">'+m.league+'</span><b>'+m.home+(m.away?' vs '+m.away:'')+'</b><span class="muted">🔴 '+m.time_label+'</span></button>').join(''):'<div class="empty">Live ပွဲမရှိသေးပါ</div>'}).catch(e=>document.querySelector('#matches').innerHTML='<div class="empty">'+e.message+'</div>')</script>`;
const watchPage = `${style}<main class="app"><a class="back" href="./">← Live matches</a><h2 id="title">Loading match…</h2><div class="player"><video id="video" controls playsinline></video></div><div id="note" class="notice">Live stream ကို ပြင်ဆင်နေပါသည်…</div></main><script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script><script>const init=window.Telegram?.WebApp?.initData||'',id=new URLSearchParams(location.search).get('id'),h={'X-Telegram-Init-Data':init};Promise.all([fetch('api/matches',{headers:h}),fetch('api/resolve/'+id,{headers:h})]).then(async a=>{let m=await a[0].json(),s=await a[1].json();if(!a[1].ok)throw Error(s.error);m=m.find(x=>x.id==id);document.querySelector('#title').textContent=m.home+(m.away?' vs '+m.away:'');let v=document.querySelector('#video');new Hls().loadSource(s.stream_url);let x=new Hls();x.loadSource(s.stream_url);x.attachMedia(v);document.querySelector('#note').textContent='Authorized provider stream.'}).catch(e=>document.querySelector('#note').textContent=e.message)</script>`;
const adminPage = `${style}<main class="app"><div class="brand">BALL LIVE MYANMAR</div><h1>Member activation</h1><section class="card"><label>Admin secret</label><input id="token" type="password" placeholder="ADMIN_API_TOKEN"><button class="btn green" onclick="connectBot()">Bot ကိုချိတ်မည် (တစ်ကြိမ်သာ)</button><button class="btn green" onclick="load()">Payment users ကိုကြည့်မည်</button><div id="users"></div></section></main><script>const H=()=>({Authorization:'Bearer '+token.value,'Content-Type':'application/json'});async function connectBot(){let r=await fetch('api/admin/telegram/configure',{method:'POST',headers:H()}),d=await r.json();alert(d.ok?'Bot ချိတ်ပြီးပါပြီ':d.error)}async function load(){let r=await fetch('api/admin/payment-requests',{headers:H()}),u=await r.json();users.innerHTML=u.map(x=>'<div class="notice"><b>'+x.display_name+'</b><br>'+x.telegram_user_id+'<br>'+x.message_type+' '+(x.latest_message||'')+'<br><button class="btn green" onclick="activate(\''+x.telegram_user_id+'\',\''+x.display_name.replaceAll("'",'')+'\')">Activate 30 days</button></div>').join('')}async function activate(id,name){let r=await fetch('api/admin/members/activate',{method:'POST',headers:H(),body:JSON.stringify({telegramUserId:id,displayName:name,days:30})});alert((await r.json()).delivery||'Activated')}</script>`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: jsonHeaders });
  const url = new URL(req.url);
  // Edge Runtime removes /functions/v1 on some requests but preserves it on
  // others. Strip everything through the function name in both cases.
  const path = url.searchParams.get("path") || url.pathname.replace(/^.*\/ball-live/, "") || "/";
  if (path === "/telegram" && req.method === "POST") return botWebhook(req);
  if (path === "/") return text(indexPage);
  if (path === "/watch") return text(watchPage);
  if (path === "/admin") return text(adminPage);
  if (path === "/api/matches") { if (!await activeMember(req)) return json({ error: "Subscription expired or inactive" }, 403); const { data, error } = await db.from("matches").select("id,league,home,away,home_logo,away_logo,time_label").eq("authorized", true).eq("is_live", true).order("id"); return error ? json({ error: error.message }, 500) : json(data); }
  if (path.startsWith("/api/resolve/")) { if (!await activeMember(req)) return json({ error: "Subscription expired or inactive" }, 403); const { data } = await db.from("matches").select("stream_type,stream_url").eq("id", Number(path.split("/").at(-1))).eq("authorized", true).maybeSingle(); return data ? json(data) : json({ error: "Match not found" }, 404); }
  if (!authorizedAdmin(req)) return json({ error: "Unauthorized" }, 401);
  if (path === "/api/admin/payment-requests") { const { data } = await db.from("payment_requests").select("telegram_user_id,display_name,received_at,customer_messages(message_type,body,file_id,received_at)").order("received_at", { ascending: false }); return json((data || []).map((r: any) => ({ ...r, latest_message: r.customer_messages?.at(-1)?.body || "", message_type: r.customer_messages?.at(-1)?.message_type || "", file_id: r.customer_messages?.at(-1)?.file_id || "" }))); }
  if (path === "/api/admin/members/activate" && req.method === "POST") { const body = await req.json(); const days = Number(body.days); if (!body.telegramUserId || !Number.isFinite(days) || days < 1) return json({ error: "Invalid member" }, 400); const id = String(body.telegramUserId); const { data: existing } = await db.from("members").select("expires_at").eq("telegram_user_id", id).maybeSingle(); const start = existing && new Date(existing.expires_at) > new Date() ? new Date(existing.expires_at) : new Date(); const expires_at = new Date(start.getTime() + days * 86400000).toISOString(); await db.from("members").upsert({ telegram_user_id: id, display_name: body.displayName || "", expires_at, updated_at: new Date().toISOString() }); let delivery = "Activated"; try { await telegram("sendMessage", { chat_id: id, text: `✅ Live Pass ဖွင့်ပေးပြီးပါပြီ။\nသက်တမ်းကုန်: ${new Date(expires_at).toLocaleString()}`, reply_markup: { inline_keyboard: [[{ text: "🔴 Watch Live", web_app: { url: `${base(req)}/` } }]] } }); delivery = "Watch Live button ပို့ပြီးပါပြီ"; } catch { delivery = "Activated (bot message ပို့မရသေးပါ)"; } return json({ expiresAt: expires_at, delivery }); }
  if (path === "/api/admin/telegram/configure" && req.method === "POST") { await telegram("setWebhook", { url: `${base(req)}/telegram`, secret_token: secret("TELEGRAM_WEBHOOK_SECRET"), allowed_updates: ["message", "callback_query"] }); return json({ ok: true }); }
  return json({ error: "Not found" }, 404);
});
