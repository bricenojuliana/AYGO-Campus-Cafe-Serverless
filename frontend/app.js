"use strict";

const API = ((window.CAMPUS_CAFE_CONFIG || {}).apiBaseUrl || "").replace(/\/+$/, "");
const MAX_LINES = 10;
const MAX_QTY = 10;
const STORAGE = {
  cart: "campusCafe.cart",
  pending: "campusCafe.pendingSubmission",
  orders: "campusCafe.knownOrders",
};

const state = {
  products: new Map(),            // productId -> product from GET /products
  cart: new Map(),                // productId -> quantity
  pending: null,                  // { key, payload } of a submission not yet confirmed by the server
  knownOrders: [],                // order IDs, newest first
  searchedOrder: null,            // while a search is active only this order is shown ("" = no match)
  submitting: false,
};

const $ = (id) => document.getElementById(id);

// ---------- storage (best effort: the app works without it) ----------
function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable: state stays in memory */ }
}

// ---------- API ----------
class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `HTTP ${status}`);
    this.status = status;
    this.code = body && body.code;
    this.requestId = body && body.requestId;
  }
}

async function api(method, path, { body, headers } = {}) {
  if (!API) throw new ApiError(0, { message: "API URL not configured. Run scripts/configure_frontend.sh." });
  let response;
  try {
    response = await fetch(API + path, {
      method,
      headers: body ? { "content-type": "application/json", ...headers } : headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, { message: "Network error: the API could not be reached." });
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) throw new ApiError(response.status, data);
  return { status: response.status, data };
}

function newIdempotencyKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------- formatting ----------
const APP_CURRENCY = "COP";
const ZERO_DECIMAL_DISPLAY = new Set(["COP"]); // centavos are stored but never shown for pesos

function money(cents, currency) {
  const code = currency || APP_CURRENCY;
  const digits = ZERO_DECIMAL_DISPLAY.has(code) ? 0 : 2;
  return new Intl.NumberFormat("es-CO", {
    style: "currency", currency: code, minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(cents / 100);
}

function setMessage(el, text, kind) {
  el.textContent = text || "";
  el.className = el.className.replace(/\b(ok|warn|error)\b/g, "").trim() + (kind ? ` ${kind}` : "");
}

// Friendlier wording for the API's error codes; the API message is the fallback.
const FRIENDLY_ERRORS = {
  PRODUCT_UNAVAILABLE: "One of the products in your cart is not available right now.",
  UNKNOWN_PRODUCT: "One of the products in your cart is no longer on the menu.",
  INVALID_REQUEST: "The order could not be accepted. Please review your cart.",
  IDEMPOTENCY_KEY_CONFLICT: "This submission was already used for a different cart. Please place the order again.",
  CATALOG_UNAVAILABLE: "The menu service is not responding right now.",
  STORE_UNAVAILABLE: "The ordering service is not available right now.",
  ORDER_NOT_FOUND: "No order exists with that code.",
};

function describe(error) {
  const ref = error.requestId ? ` (ref. ${error.requestId})` : "";
  return `${FRIENDLY_ERRORS[error.code] || error.message}${ref}`;
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const field = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }
  const label = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = label; }, 1500);
}

function renderItems(list, order) {
  list.replaceChildren(...order.items.map((item) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${item.quantity} × ${item.name}`;
    const amount = document.createElement("span");
    amount.textContent = money(item.unitPriceCents * item.quantity, order.currency);
    li.append(label, amount);
    return li;
  }));
}

// ---------- confirmation ----------
function showConfirmation(order, replayed) {
  $("confirmation-title").textContent = replayed ? "This order was already placed" : "Order confirmed!";
  $("confirmation-code").textContent = order.orderId;
  renderItems($("confirmation-items"), order);
  $("confirmation-total").textContent = money(order.totalCents, order.currency);
  $("confirmation").hidden = false;
  $("cart-area").hidden = true;
}

function hideConfirmation() {
  $("confirmation").hidden = true;
  $("cart-area").hidden = false;
}

// ---------- menu ----------
async function loadMenu() {
  setMessage($("menu-message"), "Loading menu…");
  try {
    const { data } = await api("GET", "/products");
    state.products = new Map(data.items.map((p) => [p.productId, p]));
    setMessage($("menu-message"), "");
  } catch (error) {
    setMessage($("menu-message"), `Menu unavailable: ${describe(error)}`, "error");
  }
  renderMenu();
  renderCart();
}

function renderMenu() {
  const list = $("menu");
  list.replaceChildren();
  const template = $("menu-item");
  for (const product of state.products.values()) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.querySelector(".name").textContent = product.name;
    node.querySelector(".price").textContent = product.available
      ? money(product.priceCents, product.currency)
      : "Unavailable";
    node.classList.toggle("unavailable", !product.available);
    const add = node.querySelector(".add");
    add.disabled = !product.available || !canAdd(product.productId);
    add.setAttribute("aria-label", `Add ${product.name}`);
    add.addEventListener("click", () => changeQuantity(product.productId, +1));
    list.append(node);
  }
}

// ---------- cart ----------
function canAdd(productId) {
  const qty = state.cart.get(productId) || 0;
  return qty > 0 ? qty < MAX_QTY : state.cart.size < MAX_LINES;
}

function changeQuantity(productId, delta) {
  hideConfirmation();
  const next = (state.cart.get(productId) || 0) + delta;
  if (next <= 0) state.cart.delete(productId);
  else if (next <= MAX_QTY && (state.cart.has(productId) || state.cart.size < MAX_LINES)) state.cart.set(productId, next);
  save(STORAGE.cart, [...state.cart]);
  setMessage($("submit-message"), "");
  renderMenu();
  renderCart();
}

function cartPayload() {
  // Only IDs and quantities are sent; prices and totals are always computed by the server.
  return { items: [...state.cart].map(([productId, quantity]) => ({ productId, quantity })) };
}

function renderCart() {
  const list = $("cart");
  list.replaceChildren();
  const template = $("cart-item");
  let total = 0;
  let currency = APP_CURRENCY;
  for (const [productId, quantity] of state.cart) {
    const product = state.products.get(productId);
    const node = template.content.firstElementChild.cloneNode(true);
    node.querySelector(".name").textContent = product ? product.name : productId;
    node.querySelector(".count").textContent = String(quantity);
    if (product) {
      total += product.priceCents * quantity;
      currency = product.currency;
      node.querySelector(".line-total").textContent = money(product.priceCents * quantity, product.currency);
    }
    node.querySelector(".dec").addEventListener("click", () => changeQuantity(productId, -1));
    const inc = node.querySelector(".inc");
    inc.disabled = quantity >= MAX_QTY;
    inc.addEventListener("click", () => changeQuantity(productId, +1));
    node.querySelector(".remove").addEventListener("click", () => changeQuantity(productId, -quantity));
    list.append(node);
  }
  $("cart-empty").hidden = state.cart.size > 0;
  $("cart-total").textContent = state.cart.size ? money(total, currency) : "—";
  const button = $("submit-order");
  button.disabled = state.cart.size === 0 || state.submitting;
  button.textContent = state.submitting ? "Placing order…" : isRetry() ? "Retry order" : "Place order";
}

function isRetry() {
  return state.pending !== null && state.pending.payload === JSON.stringify(cartPayload());
}

async function submitOrder() {
  const payload = cartPayload();
  const payloadJson = JSON.stringify(payload);
  // A new submission gets a new key; a retry of the same, unchanged submission reuses its key.
  if (!isRetry()) {
    state.pending = { key: newIdempotencyKey(), payload: payloadJson };
    save(STORAGE.pending, state.pending);
  }
  state.submitting = true;
  renderCart();
  setMessage($("submit-message"), "Sending order…");
  try {
    const { status, data } = await api("POST", "/orders", {
      body: payload,
      headers: { "Idempotency-Key": state.pending.key },
    });
    state.searchedOrder = null;  // a new order is always shown with the others
    rememberOrder(data.orderId);
    upsertOrderCard(data);
    applyOrderFilter();
    state.cart.clear();
    save(STORAGE.cart, []);
    state.pending = null;
    save(STORAGE.pending, null);
    setMessage($("submit-message"), "");
    showConfirmation(data, status !== 201);
  } catch (error) {
    if (error.status === 0 || error.status >= 500) {
      // Outcome unknown or dependency down: keep the key so the retry cannot create a second order.
      setMessage($("submit-message"), `${describe(error)} Your cart is kept; press “Retry order” to try again safely.`, "warn");
    } else {
      state.pending = null;
      save(STORAGE.pending, null);
      setMessage($("submit-message"), `Order rejected: ${describe(error)}`, "error");
      if (error.code === "PRODUCT_UNAVAILABLE" || error.code === "UNKNOWN_PRODUCT") loadMenu();
    }
  } finally {
    state.submitting = false;
    renderMenu();
    renderCart();
  }
}

// ---------- orders ----------
function rememberOrder(orderId) {
  state.knownOrders = [orderId, ...state.knownOrders.filter((id) => id !== orderId)].slice(0, 50);
  save(STORAGE.orders, state.knownOrders);
}

function forgetOrder(orderId) {
  state.knownOrders = state.knownOrders.filter((id) => id !== orderId);
  save(STORAGE.orders, state.knownOrders);
  const card = cardFor(orderId);
  if (card) card.remove();
}

function cardFor(orderId) {
  return [...$("orders").children].find((el) => el.dataset.orderId === orderId);
}

function ensureCard(orderId) {
  let card = cardFor(orderId);
  if (!card) {
    card = $("order-card").content.firstElementChild.cloneNode(true);
    card.dataset.orderId = orderId;
    card.querySelector(".order-code").textContent = orderId;
    const copy = card.querySelector(".copy");
    copy.setAttribute("aria-label", `Copy order code ${orderId}`);
    copy.addEventListener("click", () => copyText(orderId, copy));
    card.querySelector(".refresh").addEventListener("click", () => refreshOrder(orderId));
    card.querySelector(".cancel").addEventListener("click", () => cancelOrder(orderId));
    card.querySelector(".forget").addEventListener("click", () => forgetOrder(orderId));
    const index = state.knownOrders.indexOf(orderId);
    const before = [...$("orders").children].find((el) => state.knownOrders.indexOf(el.dataset.orderId) > index);
    $("orders").insertBefore(card, before || null);
  }
  return card;
}

function upsertOrderCard(order) {
  const card = ensureCard(order.orderId);
  const badge = card.querySelector(".badge");
  badge.textContent = order.status;
  badge.className = `badge ${order.status}`;
  renderItems(card.querySelector(".order-items"), order);
  card.querySelector(".total").textContent = `Total ${money(order.totalCents, order.currency)}`;
  card.querySelector(".dates").textContent = `Updated ${new Date(order.updatedAt).toLocaleString()}`;
  card.querySelector(".cancel").hidden = order.status !== "CREATED";
  setMessage(card.querySelector(".order-message"), "");
}

function showCardError(orderId, error) {
  const card = ensureCard(orderId);
  if (error.status === 404) {
    const badge = card.querySelector(".badge");
    badge.textContent = "NOT FOUND";
    badge.className = "badge UNKNOWN";
    card.querySelector(".cancel").hidden = true;
  }
  setMessage(card.querySelector(".order-message"), describe(error), error.status === 404 ? "warn" : "error");
}

async function refreshOrder(orderId) {
  try {
    const { data } = await api("GET", `/orders/${encodeURIComponent(orderId)}`);
    upsertOrderCard(data);
    return data;
  } catch (error) {
    showCardError(orderId, error);
    return null;
  }
}

async function cancelOrder(orderId) {
  if (!window.confirm(`Cancel order ${orderId}?`)) return;
  const card = ensureCard(orderId);
  const button = card.querySelector(".cancel");
  button.disabled = true;
  try {
    const { data } = await api("POST", `/orders/${encodeURIComponent(orderId)}/cancel`);
    upsertOrderCard(data);
    setMessage(card.querySelector(".order-message"), "Order cancelled.", "ok");
  } catch (error) {
    showCardError(orderId, error);
  } finally {
    button.disabled = false;
  }
}

function applyOrderFilter() {
  const filter = state.searchedOrder;
  for (const card of $("orders").children) {
    card.hidden = filter !== null && card.dataset.orderId !== filter;
  }
  $("show-all").hidden = filter === null;
}

function showAllOrders() {
  state.searchedOrder = null;
  $("lookup-id").value = "";
  setMessage($("orders-message"), "");
  applyOrderFilter();
}

async function lookupOrder(event) {
  event.preventDefault();
  const orderId = $("lookup-id").value.trim().toUpperCase();  // the server also normalizes codes
  if (!orderId) return;
  setMessage($("orders-message"), "Looking up order…");
  try {
    const { data } = await api("GET", `/orders/${encodeURIComponent(orderId)}`);
    rememberOrder(data.orderId);
    upsertOrderCard(data);
    $("lookup-id").value = data.orderId;
    state.searchedOrder = data.orderId;
    setMessage($("orders-message"), "");
  } catch (error) {
    state.searchedOrder = "";
    setMessage($("orders-message"),
      error.status === 404 ? `No order with code ${orderId}.` : `Lookup failed: ${describe(error)}`,
      error.status === 404 ? "warn" : "error");
  }
  applyOrderFilter();
}

// ---------- start ----------
function init() {
  $("api-status").textContent = API ? `API: ${API}` : "API URL not configured";
  state.cart = new Map(load(STORAGE.cart, []).filter(([, q]) => Number.isInteger(q) && q >= 1 && q <= MAX_QTY));
  state.pending = load(STORAGE.pending, null);
  state.knownOrders = load(STORAGE.orders, []).filter((id) => typeof id === "string");
  $("submit-order").addEventListener("click", submitOrder);
  $("lookup-form").addEventListener("submit", lookupOrder);
  $("show-all").addEventListener("click", showAllOrders);
  $("lookup-id").addEventListener("input", () => { if (!$("lookup-id").value.trim()) showAllOrders(); });
  $("new-order").addEventListener("click", () => { hideConfirmation(); setMessage($("submit-message"), ""); });
  $("confirmation-copy").addEventListener("click",
    () => copyText($("confirmation-code").textContent, $("confirmation-copy")));
  if (state.pending && isRetry()) {
    setMessage($("submit-message"), "A previous submission was not confirmed. Press “Retry order” to finish it safely.", "warn");
  }
  renderCart();
  loadMenu();
  // Known order IDs come from local storage; their state always comes from the API.
  state.knownOrders.forEach((id) => { ensureCard(id); refreshOrder(id); });
}

init();
