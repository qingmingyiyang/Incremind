export function OutcomeFreshness({ stale, freshCount, verified }) {
  if (stale !== true && !(Number.isSafeInteger(freshCount) && freshCount > 0) && freshCount !== null && verified !== false) return null;
  return <span className="ui-count">{[stale === true ? '需更新' : null,
    Number.isSafeInteger(freshCount) && freshCount > 0 ? `可补充 ${freshCount}` : verified === false ? '依据未核对' : freshCount === null ? '新资料未核对' : null].filter(Boolean).join(' · ')}</span>;
}
