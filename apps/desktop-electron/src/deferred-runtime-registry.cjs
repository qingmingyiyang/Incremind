"use strict";

const RUNTIME_NAME = /^[a-z][a-z0-9-]{1,63}$/;

class DeferredRuntimeRegistry {
  constructor(definitions = {}) {
    if (!definitions || typeof definitions !== "object" || Array.isArray(definitions)) {
      throw new TypeError("deferred_runtime_definitions_invalid");
    }
    this.definitions = new Map();
    this.values = new Map();
    for (const [name, definition] of Object.entries(definitions)) {
      if (!RUNTIME_NAME.test(name) || !definition || typeof definition.create !== "function"
        || (definition.dispose !== undefined && typeof definition.dispose !== "function")) {
        throw new TypeError("deferred_runtime_definition_invalid");
      }
      this.definitions.set(name, Object.freeze({ create: definition.create, dispose: definition.dispose || null }));
    }
    if (this.definitions.size === 0) throw new TypeError("deferred_runtime_definitions_invalid");
  }

  initialize(name) {
    const definition = this.requireDefinition(name);
    if (this.values.has(name)) return this.values.get(name);
    const value = definition.create();
    if (value === null || value === undefined || (value && typeof value.then === "function")) {
      throw new TypeError("deferred_runtime_factory_result_invalid");
    }
    this.values.set(name, value);
    return value;
  }

  get(name) {
    this.requireDefinition(name);
    return this.values.has(name) ? this.values.get(name) : null;
  }

  dispose(name) {
    const definition = this.requireDefinition(name);
    if (!this.values.has(name)) return false;
    const value = this.values.get(name);
    this.values.delete(name);
    if (!definition.dispose) return true;
    const result = definition.dispose(value);
    return result && typeof result.then === "function" ? Promise.resolve(result).then(() => true) : true;
  }

  requireDefinition(name) {
    const definition = this.definitions.get(name);
    if (!definition) throw new Error("deferred_runtime_unknown");
    return definition;
  }
}

module.exports = { DeferredRuntimeRegistry };
