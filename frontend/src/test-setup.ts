// Node >= 25 exposes an experimental global `localStorage` that shadows jsdom's Storage.
// Install jsdom's real Storage so tests asserting "nothing is stored" inspect real storage.
type JsdomGlobal = { jsdom?: { window: { localStorage: Storage; sessionStorage: Storage; Storage: typeof Storage } } };
const dom = (globalThis as JsdomGlobal).jsdom;
if (dom && typeof globalThis.localStorage?.clear !== "function") {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: dom.window.localStorage });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: dom.window.sessionStorage });
  Object.defineProperty(globalThis, "Storage", { configurable: true, value: dom.window.Storage });
}
