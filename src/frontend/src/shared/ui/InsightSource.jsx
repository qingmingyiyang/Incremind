import { Row } from './Row';
import { insightSourceReference } from '../lib/insightSourceReference';

export function InsightSource({ source, onReference }) {
  const value = insightSourceReference(source);
  if (!value) return null;
  const canOpen = Boolean(value.reference && onReference);
  return <Row title={`插话${value.text ? ` ${value.text}` : ''}`} readOnly={!canOpen}
    onOpen={canOpen ? () => onReference(value.reference) : undefined}/>;
}
