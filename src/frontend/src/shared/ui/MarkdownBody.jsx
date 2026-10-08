import ReactMarkdown from 'react-markdown';
import { ProductFileLink } from './ProductFileLink';
import { CommentCount, commentSectionRange } from './CommentSection';
import { hideReadingMarkers } from '../lib/readingMarkdown';
import './MarkdownBody.css';

const components = {
  // Reading a private note must not automatically fetch embedded remote images.
  img: ({ alt }) => <span>{alt}</span>,
  a: ({ href, children }) => <ProductFileLink href={href}>{children}</ProductFileLink>,
};
const plain = node => node.value || (node.children || []).map(plain).join('');
const readingPlugins = [hideReadingMarkers];
export function MarkdownBody({ children, evidence = [], onEvidence, omitEmptyArtifacts = false, documentId, documentRevision, commentSection }) {
  const section = commentSectionRange({ markdown: children, documentId, documentRevision, commentSection });
  const readingComponents = { ...components, h2: ({ node, children: content }) => <h2>{content}{section && node.position?.start.offset === section.start && node.position?.end.offset === section.end && <CommentCount count={section.count}/>}</h2>, p: ({ node, children: content }) => omitEmptyArtifacts && plain(node).trim() === '：。。' ? null : <p>{content}</p>, li: ({ node, children: content }) => {
    const matches = evidence.filter(fact => fact.text === plain(node).trim());
    const fact = matches.length === 1 ? matches[0] : null;
    return <li>{content}{fact && onEvidence && <button type="button" aria-label="定位原句" onClick={() => onEvidence(fact)}>↩</button>}</li>;
  } };
  return <div className="ui-markdown-body"><ReactMarkdown remarkPlugins={readingPlugins} components={readingComponents}>{children || ''}</ReactMarkdown></div>;
}
export default MarkdownBody;
