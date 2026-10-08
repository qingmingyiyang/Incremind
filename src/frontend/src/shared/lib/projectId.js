export function resolveCompanionProjectId(value) {
  if (typeof value !== "string") return "";
  const resolved = value.trim();
  return resolved && resolved.length <= 191 && !/[\u0000-\u001f\u007f]/.test(resolved) ? resolved : "";
}
