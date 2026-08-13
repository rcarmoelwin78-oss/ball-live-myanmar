import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "@supabase/supabase-js";

const secrets = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
const db = createClient(Deno.env.get("SUPABASE_URL")!, secrets.default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false } });
const cors = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type, x-telegram-init-data",
  "access-control-allow-methods": "POST, OPTIONS",
  "content-type": "application/json; charset=utf-8"
};
const secret = (key: string) => Deno.env.get(key)?.trim() || "";
const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: cors });

async function digest(key: string | ArrayBuffer, value: string) {
  const cryptoKey = await crypto.subtle.importKey("raw", typeof key === "string" ? new TextEncoder().encode(key) : key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(value));
}
async function hex(buffer: ArrayBuffer) { return [...new Uint8Array(buffer)].map((byte) => byte.toString(16).padStart(2, "0")).join(""); }
async function member(req: Request) {
  const init = req.headers.get("x-telegram-init-data") || ""; const token = secret("TELEGRAM_BOT_TOKEN");
  if (!init || !token) return null;
  const values = new URLSearchParams(init); const received = values.get("hash") || ""; values.delete("hash");
  const check = [...values.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key}=${value}`).join("\n");
  const key = await digest("WebAppData", token);
  if (received !== await hex(await digest(key, check))) return null;
  let user: any; try { user = JSON.parse(values.get("user") || "null"); } catch { return null; }
  if (!user?.id) return null;
  const { data } = await db.from("members").select("expires_at").eq("telegram_user_id", String(user.id)).maybeSingle();
  return data && new Date(data.expires_at) > new Date() ? user : null;
}
function isAdmin(req: Request) { return Boolean(secret("ADMIN_API_TOKEN")) && req.headers.get("authorization") === `Bearer ${secret("ADMIN_API_TOKEN")}`; }
async function telegram(method: string, payload: unknown) {
  const response = await fetch(`https://api.telegram.org/bot${secret("TELEGRAM_BOT_TOKEN")}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) throw new Error("Telegram request failed"); return response.json();
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return reply({ error: "POST required" }, 405);
  const { action, ...body } = await req.json().catch(() => ({}));
  if (action === "matches") {
    if (!await member(req)) return reply({ error: "Subscription expired or inactive" }, 403);
    const { data, error } = await db.from("matches").select("id,league,home,away,home_logo,away_logo,time_label").eq("authorized", true).eq("is_live", true).order("id");
    return error ? reply({ error: error.message }, 500) : reply((data || []).map((match: any) => ({ ...match, homeLogo: match.home_logo, awayLogo: match.away_logo, time: match.time_label })));
  }
  if (action === "resolve") {
    if (!await member(req)) return reply({ error: "Subscription expired or inactive" }, 403);
    const { data } = await db.from("matches").select("stream_type,stream_url").eq("id", Number(body.id)).eq("authorized", true).maybeSingle();
    return data ? reply({ type: data.stream_type, streamUrl: data.stream_url }) : reply({ error: "Match not found" }, 404);
  }
  if (!isAdmin(req)) return reply({ error: "Unauthorized" }, 401);
  if (action === "users") {
    const { data } = await db.from("payment_requests").select("telegram_user_id,display_name,received_at,customer_messages(message_type,body,file_id,received_at)").order("received_at", { ascending: false });
    return reply((data || []).map((row: any) => ({ ...row, latest_message: row.customer_messages?.at(-1)?.body || "", message_type: row.customer_messages?.at(-1)?.message_type || "", file_id: row.customer_messages?.at(-1)?.file_id || "" })));
  }
  if (action === "matches_admin") {
    const { data, error } = await db.from("matches").select("id,is_live,league,home,away,home_logo,away_logo,time_label,stream_type,stream_url").order("id", { ascending: false });
    return error ? reply({ error: error.message }, 500) : reply(data || []);
  }
  if (action === "publish_match") {
    const required = [body.league, body.home, body.away, body.streamUrl];
    if (required.some((value) => !String(value || "").trim())) return reply({ error: "League, teams and stream link are required" }, 400);
    const record = {
      is_live: true, authorized: true, league: String(body.league).trim(), home: String(body.home).trim(), away: String(body.away).trim(),
      home_logo: String(body.homeLogo || ""), away_logo: String(body.awayLogo || ""), time_label: String(body.time || "LIVE NOW"),
      stream_type: body.streamType === "youtube" ? "youtube" : "hls", stream_url: String(body.streamUrl).trim(), updated_at: new Date().toISOString()
    };
    const { data, error } = await db.from("matches").insert(record).select("id").single();
    return error ? reply({ error: error.message }, 500) : reply({ ok: true, id: data.id });
  }
  if (action === "set_match_live") {
    const { error } = await db.from("matches").update({ is_live: Boolean(body.isLive), updated_at: new Date().toISOString() }).eq("id", Number(body.id));
    return error ? reply({ error: error.message }, 500) : reply({ ok: true });
  }
  if (action === "activate") {
    const days = Number(body.days); const id = String(body.telegramUserId || ""); if (!id || !Number.isFinite(days) || days < 1) return reply({ error: "Invalid member" }, 400);
    const { data: current } = await db.from("members").select("expires_at").eq("telegram_user_id", id).maybeSingle();
    const begins = current && new Date(current.expires_at) > new Date() ? new Date(current.expires_at) : new Date();
    const expires_at = new Date(begins.getTime() + days * 86400000).toISOString();
    await db.from("members").upsert({ telegram_user_id: id, display_name: String(body.displayName || ""), expires_at, updated_at: new Date().toISOString() });
    try { await telegram("sendMessage", { chat_id: id, text: "✅ Live Pass ဖွင့်ပေးပြီးပါပြီ။", reply_markup: { inline_keyboard: [[{ text: "🔴 Watch Live", web_app: { url: secret("WEB_APP_URL") } }]] } }); return reply({ expiresAt: expires_at, delivery: "Watch Live button ပို့ပြီးပါပြီ" }); } catch { return reply({ expiresAt: expires_at, delivery: "Membership ဖွင့်ပြီးပါပြီ" }); }
  }
  if (action === "configure_bot") { await telegram("setWebhook", { url: `${Deno.env.get("SUPABASE_URL")}/functions/v1/telegram-bot`, secret_token: secret("TELEGRAM_WEBHOOK_SECRET"), allowed_updates: ["message", "callback_query"] }); return reply({ ok: true }); }
  return reply({ error: "Unknown action" }, 400);
});
