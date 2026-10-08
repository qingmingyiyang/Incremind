import { StrictMode } from "react";
import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import { useRequestScope } from "@src/shared/lib/useRequestScope";

it("rejects old results and old callbacks after unmount, including StrictMode replay", () => {
  const { result, unmount } = renderHook(() => useRequestScope("project-a"), {
    wrapper: ({ children }) => <StrictMode>{children}</StrictMode>,
  });
  const gate = result.current;
  const scope = gate.scope;
  let token;
  act(() => { token = gate.issue("list", scope); });
  expect(gate.isCurrent(token)).toBe(true);
  unmount();
  expect(gate.isCurrent(token)).toBe(false);
  expect(gate.issue("list", scope)).toBeNull();
});

it("rejects callbacks from the first visit after project A to B to A", () => {
  const { result, rerender } = renderHook(({ project }) => useRequestScope(project), {
    initialProps: { project: "A" },
  });
  const gate = result.current;
  const firstA = gate.scope;
  const old = gate.issue("list", firstA);
  rerender({ project: "B" });
  rerender({ project: "A" });
  expect(gate.issue("list", firstA)).toBeNull();
  expect(gate.isCurrent(old)).toBe(false);
  expect(gate.issue("list", gate.scope)).not.toBeNull();
});
