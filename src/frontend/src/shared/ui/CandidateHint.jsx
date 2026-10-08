import { Icon } from './Icon';
import { isCommentSource } from './CommentSection';

const relations = new Set(['duplicate_of', 'may_supersede', 'supplement', 'differs', 'new']);

export function CandidateHint({ relation, commentSource }) {
  return relations.has(relation) ? <span className="candidate-hint"><Icon name={relation} size={14}/>{['differs', 'supplement', 'may_supersede'].includes(relation) && isCommentSource(commentSource) && <span className="ui-comment-origin">评</span>}</span> : null;
}
