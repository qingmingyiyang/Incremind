import { Row } from '../../shared/ui/Row';

export function GroupedRememberReceipt({ value, renderRemember }) {
  const states = { done: '已保存', partial: '部分完成', failed: '未完成', processing: '处理中' };
  return <div aria-label="合并的原件">{value.items.map(item => <div key={item.turn_id}>
    {renderRemember ? renderRemember(item.turn_id, item.receipt.remember, item)
      : <Row readOnly title={item.receipt.remember.title || '已保存原件'} meta={states[item.receipt.remember.state] || '待核对'} />}
  </div>)}</div>;
}
