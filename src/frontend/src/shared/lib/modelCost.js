// Only complete CNY amounts are displayable. Historical/partial facts stay unknown.
export function formatModelCost(cost) {
  const amount = cost?.amount;
  if (cost?.currency !== 'CNY' || cost.observed_only ||
      !['number', 'string'].includes(typeof amount) ||
      !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(String(amount))) return '—';
  const [whole, fraction = ''] = String(amount).split('.');
  // Decimal half-up at four places, matching the exact backend amount.
  let scaled = BigInt(whole) * 10000n + BigInt((fraction + '0000').slice(0, 4));
  if (fraction[4] >= '5') scaled += 1n;
  return `¥${scaled / 10000n}.${String(scaled % 10000n).padStart(4, '0')}`;
}
