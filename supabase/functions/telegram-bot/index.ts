import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "@supabase/supabase-js";

const keys = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
const db = createClient(Deno.env.get("SUPABASE_URL")!, keys.default || Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false } });
const secret = (key: string) => Deno.env.get(key)?.trim() || "";
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
async function telegram(method: string, payload: unknown) {
  const result = await fetch(`https://api.telegram.org/bot${secret("TELEGRAM_BOT_TOKEN")}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  if (!result.ok) throw new Error("Telegram API error"); return result.json();
}
function menu() {
  const buttons: unknown[][] = [[{ text: "🔴 Live ဝယ်မည်", callback_data: "buy_live" }]];
  if (secret("WEB_APP_URL").startsWith("https://")) buttons[0].push({ text: "📺 Live ကြည့်မည်", web_app: { url: secret("WEB_APP_URL") } });
  if (secret("CHANNEL_URL").startsWith("https://t.me/")) buttons.push([{ text: "📢 Channel ဝင်မည်", url: secret("CHANNEL_URL") }]);
  return { inline_keyboard: buttons };
}
Deno.serve(async (req) => {
  if (req.method !== "POST" || req.headers.get("x-telegram-bot-api-secret-token") !== secret("TELEGRAM_WEBHOOK_SECRET")) return response({ error: "Unauthorized" }, 401);
  const update = await req.json(); const callback = update.callback_query; const message = update.message || callback?.message; const sender = callback?.from || message?.from; const chat = message?.chat;
  if (!sender?.id || !chat?.id) return response({ ok: true });
  const name = [sender.first_name, sender.last_name].filter(Boolean).join(" ");
  if (!callback) {
    await db.from("payment_requests").upsert({ telegram_user_id: String(sender.id), display_name: name, received_at: new Date().toISOString() });
    let message_type = "text", body = message?.text || "", file_id = "";
    if (message?.photo?.length) { message_type = "photo"; file_id = message.photo.at(-1).file_id; body = message.caption || ""; }
    else if (message?.document) { message_type = "document"; file_id = message.document.file_id; body = message.caption || ""; }
    if (body || file_id) await db.from("customer_messages").insert({ telegram_user_id: String(sender.id), message_type, body, file_id });
  }
  if (callback?.id) await telegram("answerCallbackQuery", { callback_query_id: callback.id });
  const wantsBuy = callback?.data === "buy_live" || ["buy", "live ဝယ်မည်", "ဝယ်မည်"].includes((message?.text || "").trim().toLowerCase());
  const payment = ["💳 တစ်လ Live Pass — 5,000 Ks (30 days)", `KPay: ${secret("KPAY_NUMBER")}`, `အမည်: ${secret("KPAY_ACCOUNT_NAME")}`, "", "1. KPay သို့ ငွေလွှဲပါ", "2. Payment screenshot ကို ဒီ bot ထဲသို့ပို့ပါ", "3. Admin အတည်ပြုပြီးလျှင် Watch Live ခလုတ် ပြန်ပို့ပေးပါမယ်။"].filter(Boolean).join("\n");
  const answer = message?.photo || message?.document ? "✅ Screenshot ကိုလက်ခံပြီးပါပြီ။ Admin စစ်ဆေးပြီးလျှင် Watch Live ခလုတ်ကိုပြန်ပို့ပေးပါမယ်။" : wantsBuy ? payment : "⚽ Ball General Live Myanmar မှ ကြိုဆိုပါတယ်။\n\nလိုချင်တာကို အောက်ကခလုတ်တစ်ခုနှိပ်ပါ။";
  await telegram("sendMessage", { chat_id: chat.id, text: answer, reply_markup: menu() }); return response({ ok: true });
});
