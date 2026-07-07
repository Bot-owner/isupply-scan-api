// iSupply.cz — jednoduchý server: servíruje statický web + vytváří Stripe Checkout platby + admin přehled
const express = require("express");
const path = require("path");
const fs = require("fs");
const nodemailer = require("nodemailer");

// Tajný klíč se NIKDY nepíše do kódu — čte se z proměnné prostředí na Railway.
if (!process.env.STRIPE_SECRET_KEY) {
  console.warn("VAROVÁNÍ: STRIPE_SECRET_KEY není nastavený. Platby nebudou fungovat.");
}
if (!process.env.ADMIN_PASSWORD) {
  console.warn("VAROVÁNÍ: ADMIN_PASSWORD není nastavený. Admin panel bude nepřístupný.");
}
if (!process.env.TELEGRAM_BOT_TOKEN || !process.env.TELEGRAM_CHAT_ID) {
  console.warn("VAROVÁNÍ: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID není nastavený. Telegram upozornění na objednávky nebudou fungovat.");
}
if (!process.env.TELEGRAM_PRICE_BOT_TOKEN || !process.env.TELEGRAM_PRICE_CHAT_ID) {
  console.warn("VAROVÁNÍ: TELEGRAM_PRICE_BOT_TOKEN/TELEGRAM_PRICE_CHAT_ID není nastavený. Upozornění na změny cen konkurence nebudou fungovat.");
}
if (!process.env.SMTP_USER || !process.env.SMTP_PASS) {
  console.warn("VAROVÁNÍ: SMTP_USER/SMTP_PASS není nastavený. Odesílání e-mailů zákazníkům nebude fungovat.");
}
if (!process.env.SMS_GATE_USER || !process.env.SMS_GATE_PASS) {
  console.warn("VAROVÁNÍ: SMS_GATE_USER/SMS_GATE_PASS není nastavený. Odesílání SMS zákazníkům nebude fungovat.");
}
const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY || "");

// ===== SMS (přes SMS Gate — vlastní Android telefon jako brána, žádné poplatky za SMS bránu) =====
async function sendShippingSms(order) {
  const phone = order.customer?.phone;
  if (!phone) return { ok: false, error: "Objednávka nemá telefon" };
  if (!process.env.SMS_GATE_USER || !process.env.SMS_GATE_PASS) return { ok: false, error: "SMS brána není nastavená" };

  const isPickup = /osobní odběr/i.test(order.shippingLabel || "");
  const text = isPickup
    ? "iSupply.cz: Vase objednavka je pripravena k vyzvednuti na adrese Opletalova 703, Chrudim."
    : "iSupply.cz: Vase zasilka byla odeslana. Ocekavejte doruceni do 2 pracovnich dnu.";

  // Telefon musí být ve formátu +420... pro spolehlivé doručení
  let normalizedPhone = phone.replace(/\s+/g, "");
  if (/^\d{9}$/.test(normalizedPhone)) normalizedPhone = "+420" + normalizedPhone;

  try {
    const res = await fetch("https://api.sms-gate.app/3rdparty/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Basic " + Buffer.from(`${process.env.SMS_GATE_USER}:${process.env.SMS_GATE_PASS}`).toString("base64")
      },
      body: JSON.stringify({ textMessage: { text }, phoneNumbers: [normalizedPhone] })
    });
    if (!res.ok) {
      const errText = await res.text();
      return { ok: false, error: `SMS Gate HTTP ${res.status}: ${errText}` };
    }
    return { ok: true };
  } catch (err) {
    console.error("Chyba při odesílání SMS:", err.message);
    return { ok: false, error: err.message };
  }
}

// ===== E-mail (přes Forpsi SMTP) =====
const mailTransport = nodemailer.createTransport({
  host: process.env.SMTP_HOST || "smtp.forpsi.com",
  port: +(process.env.SMTP_PORT || 465),
  secure: true, // port 465 = SSL
  auth: {
    user: process.env.SMTP_USER,
    pass: process.env.SMTP_PASS
  }
});

async function sendShippingEmail(order) {
  if (!order.email) return { ok: false, error: "Objednávka nemá e-mail" };
  const itemsList = (order.items || []).map(i => `- ${i.name}${i.color && i.color !== "—" ? " (" + i.color + ")" : ""} ×${i.qty || 1}`).join("\n");
  const isPickup = /osobní odběr/i.test(order.shippingLabel || "");
  const greeting = `Dobrý den${order.customer?.name ? " " + order.customer.name : ""},`;

  const subject = isPickup
    ? "Vaše objednávka je připravena k vyzvednutí — iSupply.cz"
    : "Vaše zásilka byla odeslána — iSupply.cz";

  const body = isPickup
    ? `${greeting}

vaše objednávka je připravena k osobnímu vyzvednutí na adrese:

📍 Opletalova 703, Chrudim

Obsah objednávky:
${itemsList}

Přijít si pro ni můžete kdykoliv v provozní době. Pro jistotu doporučujeme mít u sebe potvrzení objednávky nebo doklad totožnosti.

Děkujeme za nákup!
Tým iSupply.cz`
    : `${greeting}

vaše objednávka byla právě odeslána a je na cestě k vám.

Obsah objednávky:
${itemsList}

Doručení očekávejte nejpozději do 2 pracovních dnů.

Děkujeme za nákup!
Tým iSupply.cz`;

  try {
    await mailTransport.sendMail({
      from: `"iSupply.cz" <${process.env.SMTP_USER}>`,
      to: order.email,
      subject,
      text: body
    });
    return { ok: true };
  } catch (err) {
    console.error("Chyba při odesílání e-mailu:", err.message);
    return { ok: false, error: err.message };
  }
}

// Odeslání notifikace o nové objednávce na Telegram (volitelně s tlačítky)
async function sendTelegramNotification(text, replyMarkup) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) return;
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", reply_markup: replyMarkup })
    });
  } catch (e) {
    console.error("Telegram notifikace se nepodařila:", e.message);
  }
}

// Odeslání notifikace o změně ceny konkurence — samostatný, oddělený bot
async function sendPriceTelegramNotification(text) {
  const token = process.env.TELEGRAM_PRICE_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_PRICE_CHAT_ID;
  if (!token || !chatId) return;
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" })
    });
  } catch (e) {
    console.error("Telegram (cenový bot) notifikace se nepodařila:", e.message);
  }
}

async function telegramApi(method, payload) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return null;
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    return await res.json();
  } catch (e) {
    console.error(`Telegram API chyba (${method}):`, e.message);
    return null;
  }
}

const app = express();
app.use(express.json());

// ===== Jednoduché souborové úložiště (data/analytics.json) =====
// Poznámka: na Railway se souborový systém resetuje při každém redeploy (ne při běžném provozu).
// Pro dlouhodobé/trvalé uchování dat by v budoucnu bylo lepší přidat opravdovou databázi (např. Railway Postgres).
const DATA_DIR = path.join(__dirname, "data");
const DATA_FILE = path.join(DATA_DIR, "analytics.json");

function loadData() {
  try {
    const d = JSON.parse(fs.readFileSync(DATA_FILE, "utf8"));
    if (!d.priceOverrides) d.priceOverrides = {};
    if (!d.competitorMappings) d.competitorMappings = {};
    return d;
  } catch (e) {
    return { visits: [], carts: {}, orders: [], priceOverrides: {}, competitorMappings: {} };
  }
}
function saveData(data) {
  try {
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(DATA_FILE, JSON.stringify(data));
  } catch (e) {
    console.error("Chyba při ukládání dat:", e.message);
  }
}
let db = loadData();

// ===== Sledování cen konkurence (jabkolevne.cz) =====
// Zaokrouhlí cenu dolů na nejbližší "...90" (např. 9483 → 9390)
function roundToPsychological90(price) {
  return Math.floor((price - 90) / 100) * 100 + 90;
}

// Vytáhne cenu z <meta property="product:price:amount" content="X"> na stránce konkurence
async function fetchCompetitorPrice(url) {
  try {
    const res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (compatible; iSupplyPriceBot/1.0)" } });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const html = await res.text();
    const match = /product:price:amount["']?\s*content=["']([\d.]+)["']/i.exec(html)
      || /<meta[^>]*content=["']([\d.]+)["'][^>]*product:price:amount/i.exec(html);
    if (!match) return { ok: false, error: "Cena nenalezena na stránce" };
    const price = parseFloat(match[1]);
    if (!price || price <= 0) return { ok: false, error: "Neplatná cena" };
    return { ok: true, price };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// Zkontroluje jedno mapování, uloží novou cenu a případně upozorní na Telegramu
async function checkOneMapping(mappingId) {
  const m = db.competitorMappings[mappingId];
  if (!m) return { ok: false, error: "Mapování nenalezeno" };

  const result = await fetchCompetitorPrice(m.url);
  m.lastChecked = Date.now();
  if (!result.ok) {
    m.lastError = result.error;
    saveData(db);
    return result;
  }
  m.lastError = null;
  m.lastCompetitorPrice = result.price;

  const suggested = roundToPsychological90(result.price * 0.95);

  if (!db.priceOverrides[m.productId]) db.priceOverrides[m.productId] = {};
  if (!db.priceOverrides[m.productId][m.stav]) db.priceOverrides[m.productId][m.stav] = {};
  const oldPrice = db.priceOverrides[m.productId][m.stav][m.cap];

  if (oldPrice !== suggested) {
    db.priceOverrides[m.productId][m.stav][m.cap] = suggested;
    m.ourPrice = suggested;
    saveData(db);
    sendPriceTelegramNotification(
      `💰 <b>Změna ceny — ${m.productName}</b>\n${m.stav} · ${m.cap}\n\n` +
      (oldPrice ? `Předchozí cena: ${oldPrice.toLocaleString("cs-CZ")} Kč\n` : "") +
      `Nová cena: <b>${suggested.toLocaleString("cs-CZ")} Kč</b>\n` +
      `(konkurence: ${result.price.toLocaleString("cs-CZ")} Kč)`
    );
  } else {
    m.ourPrice = suggested;
    saveData(db);
  }

  return { ok: true, competitorPrice: result.price, suggested };
}

async function checkAllMappings() {
  const ids = Object.keys(db.competitorMappings);
  for (const id of ids) {
    await checkOneMapping(id);
    await new Promise(r => setTimeout(r, 2000)); // slušné rozestupy mezi požadavky
  }
}

// ===== Sledování návštěv =====
app.post("/api/track", (req, res) => {
  const { page, sid } = req.body || {};
  if (!page || !sid) return res.status(400).json({ error: "chybí page/sid" });
  db.visits.push({ ts: Date.now(), page, sid });
  // Omezit velikost historie návštěv (posledních 20 000 záznamů stačí)
  if (db.visits.length > 20000) db.visits = db.visits.slice(-20000);
  saveData(db);
  res.json({ ok: true });
});

// ===== Sledování obsahu košíku (pro přehled "otevřené košíky") =====
app.post("/api/track-cart", (req, res) => {
  const { sid, items, total } = req.body || {};
  if (!sid) return res.status(400).json({ error: "chybí sid" });
  if (!items || items.length === 0) {
    delete db.carts[sid];
  } else {
    db.carts[sid] = {
      items, total: total || 0,
      updatedAt: Date.now(),
      status: (db.carts[sid] && db.carts[sid].status === "completed") ? "completed" : "open"
    };
  }
  saveData(db);
  res.json({ ok: true });
});

// Zjištění veřejné URL webu (Railway ji poskytuje automaticky)
function getBaseUrl(req) {
  if (process.env.PUBLIC_URL) return process.env.PUBLIC_URL;
  return `${req.protocol}://${req.get("host")}`;
}

// Vytvoření platební session — voláno z platba.html
app.post("/create-checkout-session", async (req, res) => {
  try {
    const { items, sid, customer, shippingLabel } = req.body;
    if (!Array.isArray(items) || items.length === 0) {
      return res.status(400).json({ error: "Košík je prázdný." });
    }

    const baseUrl = getBaseUrl(req);

    const line_items = items.map(it => ({
      price_data: {
        currency: "czk",
        product_data: {
          name: it.name + (it.color && it.color !== "—" ? ` — ${it.color}` : "") + (it.cap && it.cap !== "—" ? `, ${it.cap}` : ""),
        },
        // Stripe očekává nejmenší měnovou jednotku (haléře), CZK zaokrouhlujeme na celé koruny
        unit_amount: Math.round(it.price * 100),
      },
      quantity: it.qty || 1,
    }));

    // Uložit kontaktní/doručovací údaje zákazníka pro tuto session (dohledáme je při dokončení platby)
    if (sid && customer) {
      if (!db.carts[sid]) db.carts[sid] = { items, total: items.reduce((s,i)=>s+i.price*(i.qty||1),0), updatedAt: Date.now(), status: "open" };
      db.carts[sid].customer = customer;
      db.carts[sid].shippingLabel = shippingLabel || null;
      saveData(db);
    }

    const session = await stripe.checkout.sessions.create({
      mode: "payment",
      // Metody platby (karta, Apple Pay, Google Pay, bankovní převody...) se řídí nastavením
      // v Stripe Dashboardu → Settings → Payment methods. Nic se nemusí nastavovat v kódu.
      line_items,
      customer_email: customer?.email || undefined,
      success_url: `${baseUrl}/uspech.html?session_id={CHECKOUT_SESSION_ID}&sid=${encodeURIComponent(sid || "")}`,
      cancel_url: `${baseUrl}/platba.html`,
      locale: "cs",
    });

    res.json({ url: session.url });
  } catch (err) {
    console.error("Stripe chyba:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Ověření stavu platby na stránce úspěchu
app.get("/session-status", async (req, res) => {
  try {
    const session = await stripe.checkout.sessions.retrieve(req.query.session_id);
    const sid = req.query.sid;

    let shippingLabelForResponse = null;
    if (session.payment_status === "paid" && sid) {
      // Zaznamenat dokončenou objednávku (jen jednou — kontrola podle session id)
      const already = db.orders.find(o => o.stripeSessionId === session.id);
      if (!already) {
        const orderItems = (db.carts[sid] && db.carts[sid].items) || [];
        const customer = (db.carts[sid] && db.carts[sid].customer) || null;
        const shippingLabel = (db.carts[sid] && db.carts[sid].shippingLabel) || null;
        shippingLabelForResponse = shippingLabel;
        const amount = (session.amount_total || 0) / 100;
        const email = customer?.email || session.customer_details?.email || null;

        if (!db.nextOrderId) db.nextOrderId = 1;
        const orderId = db.nextOrderId++;

        db.orders.push({
          id: orderId,
          ts: Date.now(),
          sid,
          stripeSessionId: session.id,
          amount,
          email,
          customer,
          shippingLabel,
          items: orderItems,
          shipped: false
        });
        if (db.carts[sid]) delete db.carts[sid]; // z otevřených košíků zmizí, je dokončený
        saveData(db);

        // Upozornění na Telegram — s kontaktními a doručovacími údaji + tlačítkem "odesláno"
        const itemsText = orderItems.map(i => `• ${i.name}${i.color && i.color !== "—" ? " — " + i.color : ""} ×${i.qty || 1}`).join("\n") || "—";
        const customerText = customer
          ? `\n\n👤 <b>${customer.name}</b>\n📧 ${customer.email}\n📞 ${customer.phone || "—"}\n🏠 ${customer.address}${customer.psc ? ", " + customer.psc : ""}`
          : `\n\n📧 ${email || "neznámý e-mail"}`;
        const shipText = shippingLabel ? `\n🚚 ${shippingLabel}` : "";

        const isPickupOrder = /osobní odběr/i.test(shippingLabel || "");
        const buttonText = isPickupOrder
          ? "🏠 Připraveno k vyzvednutí — poslat e-mail"
          : "📦 Odesláno — poslat e-mail zákazníkovi";

        sendTelegramNotification(
          `🎉 <b>Nová objednávka #${orderId}</b>\n\n${itemsText}\n\n💰 <b>${amount.toLocaleString("cs-CZ")} Kč</b>${customerText}${shipText}`,
          { inline_keyboard: [[{ text: buttonText, callback_data: `ship_${orderId}` }]] }
        );
      } else {
        shippingLabelForResponse = already.shippingLabel;
      }
    }

    res.json({ status: session.payment_status, email: session.customer_details?.email || null, shippingLabel: shippingLabelForResponse });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ===== ADMIN API (chráněno heslem) =====
function checkAdminAuth(req, res, next) {
  const key = req.headers["x-admin-key"];
  if (!process.env.ADMIN_PASSWORD || key !== process.env.ADMIN_PASSWORD) {
    return res.status(401).json({ error: "Neautorizováno" });
  }
  next();
}

app.post("/api/admin/login", (req, res) => {
  const { password } = req.body || {};
  if (process.env.ADMIN_PASSWORD && password === process.env.ADMIN_PASSWORD) {
    return res.json({ ok: true });
  }
  res.status(401).json({ ok: false });
});

// ===== Veřejný endpoint — aktuální přepsané ceny (produkty.html/produkt.html si je stahují) =====
app.get("/api/prices", (req, res) => {
  res.json(db.priceOverrides);
});

// ===== Admin — správa mapování na konkurenci =====
app.get("/api/admin/competitor-mappings", checkAdminAuth, (req, res) => {
  res.json(db.competitorMappings);
});

app.post("/api/admin/competitor-mappings", checkAdminAuth, (req, res) => {
  const { productId, productName, stav, cap, url } = req.body || {};
  if (!productId || !stav || !cap || !url) {
    return res.status(400).json({ error: "Chybí productId, stav, cap nebo url" });
  }
  const id = `${productId}_${stav}_${cap}`.replace(/\s+/g, "_");
  db.competitorMappings[id] = { productId, productName, stav, cap, url, lastChecked: null, lastCompetitorPrice: null, ourPrice: null, lastError: null };
  saveData(db);
  res.json({ ok: true, id });
});

app.delete("/api/admin/competitor-mappings/:id", checkAdminAuth, (req, res) => {
  delete db.competitorMappings[req.params.id];
  saveData(db);
  res.json({ ok: true });
});

app.post("/api/admin/competitor-mappings/:id/check", checkAdminAuth, async (req, res) => {
  const result = await checkOneMapping(req.params.id);
  res.json(result);
});

app.post("/api/admin/check-all-prices", checkAdminAuth, async (req, res) => {
  res.json({ ok: true, started: true }); // odpovíme hned, kontrola běží na pozadí
  checkAllMappings();
});

app.get("/api/admin/stats", checkAdminAuth, (req, res) => {
  const now = Date.now();
  const dayMs = 24 * 60 * 60 * 1000;

  // Návštěvy za posledních 30 dní, seskupené podle dne
  const days = [];
  for (let i = 29; i >= 0; i--) {
    const dayStart = now - i * dayMs;
    const label = new Date(dayStart).toLocaleDateString("cs-CZ", { day: "2-digit", month: "2-digit" });
    days.push({ label, from: dayStart - dayMs, to: dayStart });
  }
  const visitsByDay = days.map(d => ({
    label: d.label,
    count: db.visits.filter(v => v.ts >= d.from && v.ts < d.to).length,
    uniqueSessions: new Set(db.visits.filter(v => v.ts >= d.from && v.ts < d.to).map(v => v.sid)).size
  }));

  const last30dVisits = db.visits.filter(v => v.ts >= now - 30 * dayMs);
  const uniqueVisitors30d = new Set(last30dVisits.map(v => v.sid)).size;

  const openCarts = Object.entries(db.carts).map(([sid, c]) => ({ sid, ...c }))
    .sort((a, b) => b.updatedAt - a.updatedAt);

  const orders = [...db.orders].sort((a, b) => b.ts - a.ts);
  const totalRevenue = orders.reduce((sum, o) => sum + o.amount, 0);
  const conversionRate = uniqueVisitors30d > 0 ? (orders.length / uniqueVisitors30d) * 100 : 0;

  // Prodeje seskupené podle produktu (kolik kusů, za kolik celkem)
  const productMap = {};
  let totalUnitsSold = 0;
  orders.forEach(o => {
    (o.items || []).forEach(it => {
      const key = it.name + (it.color && it.color !== "—" ? " — " + it.color : "");
      if (!productMap[key]) productMap[key] = { name: key, qty: 0, revenue: 0 };
      productMap[key].qty += it.qty || 1;
      productMap[key].revenue += (it.price || 0) * (it.qty || 1);
      totalUnitsSold += it.qty || 1;
    });
  });
  const productSales = Object.values(productMap).sort((a, b) => b.revenue - a.revenue);

  res.json({
    visitsByDay,
    totalVisits: db.visits.length,
    uniqueVisitors30d,
    openCarts,
    openCartsValue: openCarts.reduce((s, c) => s + (c.total || 0), 0),
    orders,
    totalRevenue,
    totalUnitsSold,
    productSales,
    conversionRate
  });
});

// ===== Telegram webhook — zpracuje kliknutí na tlačítko "Odesláno" =====
app.post("/telegram-webhook", async (req, res) => {
  res.sendStatus(200); // Telegramu potvrdíme příjem hned, zbytek zpracujeme na pozadí

  const cq = req.body?.callback_query;
  if (!cq) return;

  const match = /^ship_(\d+)$/.exec(cq.data || "");
  if (!match) {
    return telegramApi("answerCallbackQuery", { callback_query_id: cq.id, text: "Neznámá akce." });
  }

  const orderId = +match[1];
  const order = db.orders.find(o => o.id === orderId);
  if (!order) {
    return telegramApi("answerCallbackQuery", { callback_query_id: cq.id, text: "Objednávka nenalezena." });
  }
  if (order.shipped) {
    return telegramApi("answerCallbackQuery", { callback_query_id: cq.id, text: "Už bylo odesláno dřív." });
  }

  const result = await sendShippingEmail(order);
  const smsResult = await sendShippingSms(order);
  if (result.ok) {
    order.shipped = true;
    order.shippedAt = Date.now();
    saveData(db);
    const isPickupOrder = /osobní odběr/i.test(order.shippingLabel || "");
    const smsNote = smsResult.ok ? " + SMS" : (smsResult.error ? " (SMS se nepodařilo: " + smsResult.error + ")" : "");
    await telegramApi("answerCallbackQuery", { callback_query_id: cq.id, text: "✅ E-mail zákazníkovi odeslán!" + smsNote });
    // Upravit původní zprávu, ať je vidět, že je vyřízeno (odstraníme tlačítko)
    if (cq.message) {
      await telegramApi("editMessageReplyMarkup", {
        chat_id: cq.message.chat.id,
        message_id: cq.message.message_id,
        reply_markup: { inline_keyboard: [[{ text: isPickupOrder ? "✅ Zákazník informován — k vyzvednutí" : "✅ Zákazník informován o odeslání", callback_data: "noop" }]] }
      });
    }
  } else {
    await telegramApi("answerCallbackQuery", { callback_query_id: cq.id, text: "⚠️ E-mail se nepodařilo odeslat: " + result.error, show_alert: true });
  }
});

// Servírování statických souborů webu (html, css, js, obrázky)
app.use(express.static(path.join(__dirname), { extensions: ["html"] }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, async () => {
  console.log(`iSupply server běží na portu ${PORT}`);
  // Zaregistrovat Telegram webhook, ať bot ví, kam posílat kliknutí na tlačítka
  if (process.env.TELEGRAM_BOT_TOKEN && process.env.PUBLIC_URL) {
    const webhookUrl = `${process.env.PUBLIC_URL}/telegram-webhook`;
    const result = await telegramApi("setWebhook", { url: webhookUrl });
    console.log("Telegram webhook registrace:", result?.ok ? "OK → " + webhookUrl : result);
  } else {
    console.warn("VAROVÁNÍ: PUBLIC_URL není nastavený, Telegram webhook (tlačítko Odesláno) nebude fungovat.");
  }

  // Denní automatická kontrola cen konkurence (jednou za 24 hodin, první běh o hodinu později po startu)
  const DAY_MS = 24 * 60 * 60 * 1000;
  setTimeout(() => {
    checkAllMappings();
    setInterval(checkAllMappings, DAY_MS);
  }, 60 * 60 * 1000);
});
