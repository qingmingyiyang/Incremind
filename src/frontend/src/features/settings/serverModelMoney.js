// 金额直接传递十进制字符串，避免二进制浮点改变扣费数值。
export const validMoney = (value, signed = false) => typeof value === 'string' && (signed ? /^-?(?:0|[1-9]\d{0,11})(?:\.\d{1,12})?$/ : /^(?:0|[1-9]\d{0,11})(?:\.\d{1,12})?$/).test(value);
export const validModelPrice = value => typeof value === 'string' && /^(?:0|[1-9]\d{0,5}|1000000)(?:\.\d{1,6})?$/.test(value) && (!value.startsWith('1000000.') || /^1000000\.0+$/.test(value));
