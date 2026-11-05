import { Fragment, useEffect, useRef } from 'react';
import { AudioEvidenceClip } from '../../shared/ui/AudioEvidenceClip';

// Locator 使用 Python Unicode 坐标，按原文核验后才显示窗口与播放入口。
export function CitationBody({ text, coordinateSpace, citation, projectId, sourceId, scopeKey }) {
  const mark = useRef(null), points = Array.from(text);
  const windows = citation.locator?.windows || [];
  let end = 0;
  const valid = coordinateSpace === citation.locator?.coordinate_space && windows.length > 0 && windows.every(window => {
    const okay = Number.isInteger(window.start) && Number.isInteger(window.end) && window.start >= end && window.end > window.start && window.end <= points.length;
    end = window.end; return okay;
  }) && windows.map(window => points.slice(window.start, window.end).join('')).join('\n…\n') === citation.quote;
  useEffect(() => { if (valid) mark.current?.scrollIntoView?.({ block: 'center' }); }, [text, citation, valid]);
  const parts = []; end = 0;
  if (valid) for (const [index, window] of windows.entries()) {
    parts.push(points.slice(end, window.start).join(''));
    parts.push(<Fragment key={JSON.stringify([window.start, window.end, citation.quote])}>
      <AudioEvidenceClip projectId={projectId} sourceId={sourceId} seconds={window.seconds} scopeKey={scopeKey}/>
      <mark ref={index === 0 ? mark : null}>{points.slice(window.start, window.end).join('')}</mark>
    </Fragment>);
    end = window.end;
  }
  parts.push(points.slice(end).join(''));
  return <>{!valid && citation.quote && <blockquote className="workbench-citation-quote">{citation.quote}</blockquote>}<div className="ui-source-text">{parts}</div></>;
}
