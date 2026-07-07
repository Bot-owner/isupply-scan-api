// iSupply.cz — sdílený košík (localStorage), používá se na produkty.html, produkt.html, kosik.html, platba.html
const CART_KEY = "isupply_cart_v1";
const SID_KEY = "isupply_sid_v1";

function getSid() {
  let sid = localStorage.getItem(SID_KEY);
  if (!sid) {
    sid = "s_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(SID_KEY, sid);
  }
  return sid;
}

const Cart = {
  getSid,

  getAll() {
    try {
      const raw = localStorage.getItem(CART_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  },

  saveAll(items) {
    localStorage.setItem(CART_KEY, JSON.stringify(items));
    Cart._notify();
    Cart._sync(items);
  },

  // item: {name, color, cap, stav, battery (bool), price, img, qty}
  add(item) {
    const items = Cart.getAll();
    // Sloučit se stejnou položkou (stejný název+barva+kapacita+stav+baterie), navýšit množství
    const existing = items.find(i =>
      i.name === item.name && i.color === item.color && i.cap === item.cap &&
      i.stav === item.stav && !!i.battery === !!item.battery
    );
    if (existing) {
      existing.qty += item.qty || 1;
    } else {
      items.push({ ...item, qty: item.qty || 1 });
    }
    Cart.saveAll(items);
  },

  removeAt(idx) {
    const items = Cart.getAll();
    items.splice(idx, 1);
    Cart.saveAll(items);
  },

  setQty(idx, qty) {
    const items = Cart.getAll();
    if (items[idx]) {
      items[idx].qty = Math.max(1, qty);
      Cart.saveAll(items);
    }
  },

  clear() {
    Cart.saveAll([]);
  },

  count() {
    return Cart.getAll().reduce((sum, i) => sum + i.qty, 0);
  },

  total() {
    return Cart.getAll().reduce((sum, i) => sum + i.price * i.qty, 0);
  },

  // Aktualizovat badge v hlavičce, pokud existuje na stránce
  _notify() {
    document.querySelectorAll(".cart-badge-count").forEach(el => {
      const n = Cart.count();
      el.textContent = n;
      el.style.display = n > 0 ? "" : "none";
    });
  },

  // Poslat aktuální obsah košíku na server (pro admin přehled "otevřené košíky")
  _sync(items) {
    try {
      fetch("/api/track-cart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid: getSid(), items, total: Cart.total() })
      }).catch(() => {});
    } catch (e) {}
  }
};

// Zaznamenat návštěvu stránky (pro admin přehled návštěvnosti)
function trackPageView() {
  try {
    const page = window.location.pathname.split("/").pop() || "index.html";
    fetch("/api/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page, sid: getSid() })
    }).catch(() => {});
  } catch (e) {}
}

document.addEventListener("DOMContentLoaded", () => {
  Cart._notify();
  trackPageView();
});
