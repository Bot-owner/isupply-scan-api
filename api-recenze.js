/* ═══════════════════════════════════════════════════════════════════
   /api/recenze — hodnocení a recenze z Googlu
   ═══════════════════════════════════════════════════════════════════

   PROČ TO MUSÍ BÝT NA SERVERU
   Klíč ke Google API se nesmí dostat do prohlížeče. Kdokoli by si ho
   otevřel ve zdrojovém kódu, mohl by ho použít na svoje dotazy a
   účet by ti platil jejich provoz. Proto se Googlu ptá server a
   webu posílá jen hotový výsledek.

   JAK TO NASADIT
   1) V Google Cloud Console založ projekt a zapni „Places API".
   2) Vytvoř API klíč a omez ho na Places API (a ideálně na IP serveru).
   3) Na Railway přidej dvě proměnné prostředí:
        GOOGLE_API_KEY   … ten klíč
        GOOGLE_PLACE_ID  … identifikátor tvé provozovny
      Place ID najdeš přes „Place ID Finder" v dokumentaci Google Maps
      — vypadá zhruba jako ChIJ....
   4) V hlavním souboru serveru (app.js / server.js / index.js) přidej:

        const recenze = require("./api-recenze");
        app.get("/api/recenze", recenze);

   5) Nasaď a otevři https://www.isupply.cz/api/recenze — musí vrátit
      JSON s průměrem a počtem. Web si ho pak načte sám.

   NEŽ TO ZAPNEŠ, VĚZ TOHLE
   · Google vrací nejvýš pět recenzí a nedá se vybrat které —
     posílá ty, které sám považuje za nejrelevantnější.
   · Volání se účtují. Proto se výsledek drží v paměti (viz PLATNOST)
     a Googlu se voláme řádově jednotky případů denně.
   · Podmínky Googlu vyžadují uvádět, že data pocházejí z Googlu, a
     omezují, jak dlouho se smí uchovávat. Než to spustíš naostro,
     přečti si aktuální „Places API Policies" — pravidla se mění a
     tenhle komentář nemusí být aktuální.
   ═══════════════════════════════════════════════════════════════════ */

"use strict";

const PLATNOST_MS = 6 * 60 * 60 * 1000;   // jak dlouho držíme odpověď (6 hodin)

let cache = { kdy: 0, data: null };

/* Google vrací jazyk podle parametru; chceme české texty. */
const JAZYK = "cs";

async function zGooglu() {
  const klic = process.env.GOOGLE_API_KEY;
  const place = process.env.GOOGLE_PLACE_ID;
  if (!klic || !place) {
    throw new Error("Chybí GOOGLE_API_KEY nebo GOOGLE_PLACE_ID");
  }

  /* Places API (New). Přes hlavičku X-Goog-FieldMask říkáme, která
     pole chceme — účtuje se podle toho, takže si neříkáme o nic navíc. */
  const url = "https://places.googleapis.com/v1/places/" +
              encodeURIComponent(place) + "?languageCode=" + JAZYK;

  const odpoved = await fetch(url, {
    headers: {
      "X-Goog-Api-Key": klic,
      "X-Goog-FieldMask": "rating,userRatingCount,googleMapsUri,reviews"
    }
  });

  if (!odpoved.ok) {
    const telo = await odpoved.text().catch(() => "");
    throw new Error("Google odpověděl " + odpoved.status + ": " + telo.slice(0, 200));
  }

  const g = await odpoved.json();

  /* Přemapujeme na tvar, kterému rozumí web. Držíme se jen toho, co
     opravdu potřebujeme — méně dat, méně starostí s jejich uchováním. */
  const recenze = (g.reviews || []).map(r => ({
    text: (r.originalText && r.originalText.text) || (r.text && r.text.text) || "",
    jmeno: (r.authorAttribution && r.authorAttribution.displayName) || "Zákazník",
    co: "Ověřená recenze z Googlu",
    odkaz: r.googleMapsUri || g.googleMapsUri || ""
  })).filter(r => r.text.trim().length > 0);

  return {
    prumer: typeof g.rating === "number" ? g.rating : 0,
    pocet: g.userRatingCount || 0,
    odkaz: g.googleMapsUri || "",
    recenze
  };
}

module.exports = async function handler(req, res) {
  try {
    const ted = Date.now();

    /* Čerstvá odpověď z paměti — Googlu se ptáme jen po vypršení. */
    if (cache.data && ted - cache.kdy < PLATNOST_MS) {
      res.set("Cache-Control", "public, max-age=1800");
      return res.json(cache.data);
    }

    const data = await zGooglu();
    cache = { kdy: ted, data };
    res.set("Cache-Control", "public, max-age=1800");
    return res.json(data);

  } catch (e) {
    console.error("[api/recenze]", e.message);

    /* Když Google selže, ale máme starší odpověď, pošleme radši ji.
       Lepší mírně zastaralé hodnocení než rozbitá sekce na webu. */
    if (cache.data) {
      res.set("Cache-Control", "public, max-age=300");
      return res.json(cache.data);
    }

    /* Nemáme nic — web si poradí a nechá ruční seznam. */
    return res.status(503).json({ chyba: "Hodnocení se nepodařilo načíst" });
  }
};
