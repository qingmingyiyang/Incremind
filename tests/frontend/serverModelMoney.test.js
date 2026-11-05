import { describe, expect, it } from 'vitest';
import { validMoney, validModelPrice } from '../../src/frontend/src/features/settings/serverModelMoney';

describe('管理员金额输入', () => {
  it.each(['0', '0.000000000001', '999999999999.999999999999', '12.34'])('保留有效十进制字符串 %s', value => {
    expect(validMoney(value)).toBe(true);
    expect(JSON.parse(JSON.stringify({ amount: value })).amount).toBe(value);
  });
  it.each(['1e3', '1000000000000', '0.0000000000001', 'NaN', 'Infinity', ' 1', '+1', '01', '', '-1'])('拒绝非正金额格式 %s', value => {
    expect(validMoney(value)).toBe(false);
  });
  it('允许管理员使用负金额调整余额', () => {
    expect(validMoney('-12.340000000001', true)).toBe(true);
    expect(validMoney('-1000000000000', true)).toBe(false);
  });
  it('拒绝已经转换成浮点的金额', () => {
    expect(validMoney(0.1)).toBe(false);
    expect(validMoney(null)).toBe(false);
  });
});

describe('模型单价输入', () => {
  it.each(['0', '0.000001', '999999.999999', '1000000', '1000000.000000'])('接受六位精度和上限 %s', value => {
    expect(validModelPrice(value)).toBe(true);
  });
  it.each(['0.0000001', '1000001', '1000000.000001', '-0.1', '999999999999', '1e2'])('拒绝后端不接受的单价 %s', value => {
    expect(validModelPrice(value)).toBe(false);
  });
  it('余额和单价使用各自的精度边界', () => {
    expect(validMoney('0.0000001')).toBe(true);
    expect(validModelPrice('0.0000001')).toBe(false);
  });
});
