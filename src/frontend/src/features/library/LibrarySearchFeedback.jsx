export function LibrarySearchFeedback({ id, project, scene, layer, query, loading, error, empty, onSearchSources }) {
  return <>
    <p id={id} className="library-search-scope">搜索范围 · {project} / {scene || '全部场景'} / {layer}</p>
    {!error && (loading ? <p className="library-search-feedback" role="status">{query ? '搜索中' : '读取中'}</p> : empty &&
      <div className="library-search-feedback">
        <span role="status">{query ? `这个范围没有匹配的${layer}` : `暂无${layer}`}</span>
        {query && onSearchSources && <button type="button" onClick={onSearchSources}>搜原件</button>}
      </div>)}
  </>;
}

export function LibraryMatch({ match }) {
  if (!match?.quote) return null;
  return <span className="library-search-match">{match.pre}<mark>{match.quote}</mark>{match.post}</span>;
}
