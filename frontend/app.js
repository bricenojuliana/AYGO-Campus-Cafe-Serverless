"use strict";

const CONFIG = window.CAMPUS_CAFE_CONFIG || {};
const API = (CONFIG.apiBaseUrl || "").replace(/\/+$/, "");
const MAX_LINES = 10;
const MAX_QTY = 10;
// The scopes the Orders API requires, one per route. The access token carries them.
const ORDERS_SCOPES = "orders:create orders:read orders:cancel";
// Requested at login: the OpenID scopes give the profile shown in the header (name, email); the
// Orders scopes go into the access token for the API.
const LOGIN_SCOPES = `openid profile email ${ORDERS_SCOPES}`;
const STORAGE = {
  cart: "campusCafe.cart",              // the cart is not personal: it survives logging in and out
  legacy: ["campusCafe.knownOrders", "campusCafe.pendingSubmission"],  // pre-login keys, no longer used
};

const state = {
  products: new Map(),            // productId -> product from GET /products
  cart: new Map(),                // productId -> quantity
  pending: null,                  // { key, payload } of a submission not yet confirmed by the server
  knownOrders: [],                // order IDs, newest first
  searchedOrder: null,            // while a search is active only this order is shown ("" = no match)
  submitting: false,
  user: null,                     // profile of the signed-in user (display only), or null
  session: null,                  // { scopes, expiresAt } granted to the access token (display only)
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

// Remembered order codes and unconfirmed submissions belong to one user: Bob never sees Alice's list.
// This only organizes the UI; the API is what enforces that nobody can read someone else's orders.
function userKey(name) {
  return `campusCafe.user.${state.user.sub}.${name}`;
}

// ---------- authentication (Auth0 SPA SDK: Authorization Code Flow with PKCE) ----------
let auth0Client = null;

function authConfigured() {
  return Boolean(CONFIG.auth0Domain && CONFIG.auth0ClientId && CONFIG.auth0Audience && window.auth0);
}

async function initAuth() {
  if (!authConfigured()) return;
  // No client secret: a browser cannot keep one. PKCE protects the authorization code instead.
  // Tokens stay in memory (the SDK default), never in localStorage where any script could read them.
  auth0Client = await auth0.createAuth0Client({
    domain: CONFIG.auth0Domain,
    clientId: CONFIG.auth0ClientId,
    authorizationParams: {
      redirect_uri: window.location.origin,
      audience: CONFIG.auth0Audience,   // the access token is for the Campus Café API, not an ID token
      scope: LOGIN_SCOPES,
    },
  });
  const params = new URLSearchParams(window.location.search);
  if (params.has("code") && params.has("state")) {
    try {
      await auth0Client.handleRedirectCallback();
    } catch (error) {
      setMessage($("orders-message"), `Login failed: ${error.message}`, "error");
    }
    window.history.replaceState({}, document.title, window.location.pathname);  // drop code/state from the URL
  }
  if (await auth0Client.isAuthenticated()) {
    // Profile claims from the ID token, used only to show who is signed in. Never sent to the API.
    state.user = await auth0Client.getUser();
    state.session = await sessionDetails();
  }
}

// What Auth0 granted, as reported next to the token (the access token itself stays opaque to the UI).
async function sessionDetails() {
  try {
    const response = await auth0Client.getTokenSilently({
      authorizationParams: { audience: CONFIG.auth0Audience, scope: LOGIN_SCOPES },
      detailedResponse: true,
    });
    return {
      scopes: new Set((response.scope || "").split(" ").filter(Boolean)),
      expiresAt: Date.now() + (response.expires_in || 0) * 1000,
    };
  } catch {
    return null;
  }
}

async function accessToken() {
  try {
    return await auth0Client.getTokenSilently({
      authorizationParams: { audience: CONFIG.auth0Audience, scope: LOGIN_SCOPES },
    });
  } catch {
    throw new ApiError(401, { code: "UNAUTHENTICATED", message: "Your session has expired. Please log in again." });
  }
}

let bannerTimer = null;

function showBanner() {
  $("banner").hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(hideBanner, 6000);
}

function hideBanner() {
  clearTimeout(bannerTimer);
  $("banner").hidden = true;
}

function displayName(user) {
  return user.nickname || user.name || (user.email || "user").split("@")[0];
}

function renderAvatar(el, user) {
  const initials = displayName(user).replace(/[^\p{L}\p{N}]/gu, "").slice(0, 2).toUpperCase() || "?";
  el.replaceChildren(initials);
  if (user.picture) {
    const img = Object.assign(document.createElement("img"), { src: user.picture, alt: "" });
    img.addEventListener("error", () => img.remove());  // keep the initials if the picture cannot load
    el.append(img);
  }
}

function describeExpiry(expiresAt) {
  const minutes = Math.round((expiresAt - Date.now()) / 60000);
  if (minutes <= 0) return "expired: log in again";
  return minutes >= 60 ? `expires in ${Math.floor(minutes / 60)} h ${minutes % 60} min` : `expires in ${minutes} min`;
}

function renderUserCard() {
  const user = state.user;
  renderAvatar($("user-card-avatar"), user);
  $("user-card-name").textContent = displayName(user);
  $("user-card-email").textContent = user.email
    ? `${user.email}${user.email_verified ? " · verified" : " · not verified"}` : "";
  const id = $("user-card-id");
  id.textContent = user.sub.length > 18 ? `${user.sub.slice(0, 18)}…` : user.sub;
  id.title = user.sub;
  // One chip per scope the Orders API uses: granted ones are marked, missing ones explain a 403.
  $("user-card-scopes").replaceChildren(...ORDERS_SCOPES.split(" ").map((scope) => {
    const granted = state.session !== null && state.session.scopes.has(scope);
    return Object.assign(document.createElement("span"), {
      className: `scope ${granted ? "granted" : "missing"}`,
      textContent: `${granted ? "✓" : "✗"} ${scope.split(":")[1]}`,
      title: `${scope}: ${granted ? "granted" : "not granted"}`,
    });
  }));
  $("user-card-session").textContent = state.session ? describeExpiry(state.session.expiresAt) : "unknown";
}

function toggleUserCard(open) {
  const card = $("user-card");
  const show = open ?? card.hidden;
  if (show) renderUserCard();
  card.hidden = !show;
  $("user-chip").setAttribute("aria-expanded", String(show));
}

function renderSession() {
  const signedIn = state.user !== null;
  $("login").hidden = signedIn || !auth0Client;
  $("user-menu").hidden = !signedIn;
  if (signedIn) {
    renderAvatar($("user-avatar"), state.user);
    $("user-name").textContent = displayName(state.user);
  }
  $("session-user").textContent = signedIn ? ""
    : auth0Client ? "Not signed in" : "Login not configured (see config.example.js)";
  $("login-hint").hidden = signedIn;
  $("orders-hint").hidden = signedIn;
  $("lookup-form").hidden = !signedIn;
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

async function api(method, path, { body, headers, auth = false } = {}) {
  if (!API) throw new ApiError(0, { message: "API URL not configured. Run scripts/configure_frontend.sh." });
  const sent = { ...headers };
  if (body) sent["content-type"] = "application/json";
  if (auth) {
    // Only the Orders calls carry the access token; the public menu is fetched without one.
    if (!state.user) throw new ApiError(401, { code: "UNAUTHENTICATED", message: "Please log in first." });
    sent.authorization = `Bearer ${await accessToken()}`;
  }
  let response;
  try {
    response = await fetch(API + path, {
      method,
      headers: sent,
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
  UNAUTHENTICATED: "Please log in again to continue.",
  INSUFFICIENT_SCOPE: "Your sign-in does not allow this action.",
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
  // The gateway's own 401/403 (rejected token or missing scope) carry no code, only the status.
  const code = error.code || { 401: "UNAUTHENTICATED", 403: "INSUFFICIENT_SCOPE" }[error.status];
  return `${FRIENDLY_ERRORS[code] || error.message}${ref}`;
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
  button.disabled = state.cart.size === 0 || state.submitting;  // logged out: the click explains how to continue
  button.textContent = state.submitting ? "Placing order…" : isRetry() ? "Retry order" : "Place order";
}

function isRetry() {
  return state.pending !== null && state.pending.payload === JSON.stringify(cartPayload());
}

async function submitOrder() {
  if (!state.user) {
    // The menu and the cart are public; placing an order needs a signed-in user.
    showBanner();
    return;
  }
  const payload = cartPayload();
  const payloadJson = JSON.stringify(payload);
  // A new submission gets a new key; a retry of the same, unchanged submission reuses its key.
  if (!isRetry()) {
    state.pending = { key: newIdempotencyKey(), payload: payloadJson };
    save(userKey("pendingSubmission"), state.pending);
  }
  state.submitting = true;
  renderCart();
  setMessage($("submit-message"), "Sending order…");
  try {
    const { status, data } = await api("POST", "/orders", {
      body: payload,
      headers: { "Idempotency-Key": state.pending.key },
      auth: true,
    });
    state.searchedOrder = null;  // a new order is always shown with the others
    rememberOrder(data.orderId);
    upsertOrderCard(data);
    applyOrderFilter();
    state.cart.clear();
    save(STORAGE.cart, []);
    state.pending = null;
    save(userKey("pendingSubmission"), null);
    setMessage($("submit-message"), "");
    showConfirmation(data, status !== 201);
  } catch (error) {
    if (error.status === 0 || error.status >= 500) {
      // Outcome unknown or dependency down: keep the key so the retry cannot create a second order.
      setMessage($("submit-message"), `${describe(error)} Your cart is kept; press “Retry order” to try again safely.`, "warn");
    } else {
      state.pending = null;
      save(userKey("pendingSubmission"), null);
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
  save(userKey("knownOrders"), state.knownOrders);
}

function forgetOrder(orderId) {
  state.knownOrders = state.knownOrders.filter((id) => id !== orderId);
  save(userKey("knownOrders"), state.knownOrders);
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
    const { data } = await api("GET", `/orders/${encodeURIComponent(orderId)}`, { auth: true });
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
    const { data } = await api("POST", `/orders/${encodeURIComponent(orderId)}/cancel`, { auth: true });
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
    const { data } = await api("GET", `/orders/${encodeURIComponent(orderId)}`, { auth: true });
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
async function init() {
  $("api-status").textContent = API ? `API: ${API}` : "API URL not configured";
  STORAGE.legacy.forEach((key) => save(key, null));
  state.cart = new Map(load(STORAGE.cart, []).filter(([, q]) => Number.isInteger(q) && q >= 1 && q <= MAX_QTY));
  $("login").addEventListener("click", () => auth0Client.loginWithRedirect());
  $("banner-close").addEventListener("click", hideBanner);
  $("user-chip").addEventListener("click", () => toggleUserCard());
  document.addEventListener("click", (event) => {
    if (!$("user-menu").contains(event.target)) toggleUserCard(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggleUserCard(false);
  });
  $("logout").addEventListener("click",
    () => auth0Client.logout({ logoutParams: { returnTo: window.location.origin } }));
  $("submit-order").addEventListener("click", submitOrder);
  $("lookup-form").addEventListener("submit", lookupOrder);
  $("show-all").addEventListener("click", showAllOrders);
  $("lookup-id").addEventListener("input", () => { if (!$("lookup-id").value.trim()) showAllOrders(); });
  $("new-order").addEventListener("click", () => { hideConfirmation(); setMessage($("submit-message"), ""); });
  $("confirmation-copy").addEventListener("click",
    () => copyText($("confirmation-code").textContent, $("confirmation-copy")));
  renderCart();
  loadMenu();  // public: works before and without login

  await initAuth();
  renderSession();
  if (!state.user) {
    renderCart();
    return;
  }
  state.pending = load(userKey("pendingSubmission"), null);
  state.knownOrders = load(userKey("knownOrders"), []).filter((id) => typeof id === "string");
  if (state.pending && isRetry()) {
    setMessage($("submit-message"), "A previous submission was not confirmed. Press “Retry order” to finish it safely.", "warn");
  }
  renderCart();
  // Known order IDs come from local storage; their state always comes from the API.
  state.knownOrders.forEach((id) => { ensureCard(id); refreshOrder(id); });
}

init();
