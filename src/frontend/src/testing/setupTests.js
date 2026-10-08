import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/dom";

configure({ asyncUtilTimeout: 15000 });

if (!window.localStorage || typeof window.localStorage.clear !== "function") {
  const values = new Map();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key) => values.has(String(key)) ? values.get(String(key)) : null,
      key: (index) => Array.from(values.keys())[index] ?? null,
      removeItem: (key) => values.delete(String(key)),
      setItem: (key, value) => values.set(String(key), String(value)),
      get length() { return values.size; },
    },
  });
}
