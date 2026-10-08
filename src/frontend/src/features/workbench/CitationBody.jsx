import { useEffect, useRef } from 'react';

// Locator offsets are Python Unicode coordinates; never search for a replacement.
export function CitationBody({ text, coordinateSpace, citation }) {
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
    parts.push(<mark key={index} ref={index === 0 ? mark : null}>{points.slice(window.start, window.end).join('')}</mark>);
    end = window.end;
  }
  parts.push(points.slice(end).join(''));
  return <>{!valid && citation.quote && <blockquote className="workbench-citation-quote">{citation.quote}</blockquote>}<div className="ui-source-text">{parts}</div></>;
}
