import { useEffect, useRef } from "react";

// A scope is captured by each render. Old callbacks cannot issue a fresh request
// after the project changes, even if their network operation finishes later.
export function useRequestScope(key) {
  const gate = useRef(null);
  if (!gate.current) {
    gate.current = {
      scope: { key, sequences: new Map() },
      mounted: false,
      lifetime: 0,
      isScopeCurrent(scope) { return this.mounted && this.scope === scope; },
      issue(channel, scope) {
        if (!this.isScopeCurrent(scope)) return null;
        const sequence = (scope.sequences.get(channel) || 0) + 1;
        scope.sequences.set(channel, sequence);
        return { scope, channel, sequence, lifetime: this.lifetime };
      },
      isCurrent(token) {
        return Boolean(token && token.lifetime === this.lifetime && this.isScopeCurrent(token.scope)
          && token.scope.sequences.get(token.channel) === token.sequence);
      },
      invalidate(channel) {
        const scope = this.scope;
        scope.sequences.set(channel, (scope.sequences.get(channel) || 0) + 1);
      },
    };
  }
  if (gate.current.scope.key !== key) gate.current.scope = { key, sequences: new Map() };
  useEffect(() => {
    const current = gate.current;
    current.mounted = true;
    return () => { current.mounted = false; current.lifetime += 1; };
  }, []);
  return gate.current;
}
