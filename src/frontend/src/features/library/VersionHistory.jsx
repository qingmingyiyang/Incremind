import { useEffect, useState } from "react";
import { recognitionApi } from "../../shared/api/recognitionApi";

const actions = { publish: "发布", revise: "修订", revoke: "撤销", split: "拆分", merge: "合并", import: "导入基线" };

export default function VersionHistory({ projectId, recognitionId, api = recognitionApi }) {
  const [versions, setVersions] = useState([]);
  const [imported, setImported] = useState([]);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(true), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let current = true;
    setVersions([]); setImported([]); setError(""); setReading(true);
    api.loadRecognitionVersions({ projectId, recognitionId }).then((result) => {
      if (current) { setVersions(result.versions || []); setImported(result.imported_versions || []); }
    }).catch(() => { if (current) setError('读取未完成'); })
      .finally(() => { if (current) setReading(false); });
    return () => { current = false; };
  }, [projectId, recognitionId, api, refresh]);
  return <details className="recognition-version-history"><summary>修改历史 · {reading || error ? '—' : versions.length} 个版本{imported.length ? ` · ${imported.length} 个来源版本` : ""}</summary>
    {error ? <p role="alert">{error}<button type="button" disabled={reading} onClick={() => setRefresh(value => value + 1)}>重新读取</button></p> : null}
    {reading && <p role="status">读取修改历史</p>}
    {versions.map((version) => <details key={version.id}><summary>版本 {version.version} · {actions[version.action] || version.action} · {version.recorded_at}</summary>
      <p style={{ whiteSpace: "pre-wrap" }}>{version.snapshot.content}</p>
      {version.snapshot.conditions?.length ? <p>适用条件：{version.snapshot.conditions.join("；")}</p> : null}
      <p>来源：{[...(version.snapshot.source_experience_ids || []), ...(version.snapshot.source_recognition_ids || [])].join("、") || "无"}</p>
    </details>)}
    {imported.map((entry) => {
      const version = entry.source_record.payload;
      return <details key={`${entry.origin_bundle_id}:${entry.source_record.id}`}><summary>来源项目 {entry.source_scope.project_id} · 原版本 {version.version}</summary>
        <p>原认识：{version.recognition_id} · 原修订 {version.recognition_revision} · {version.recorded_at}</p>
        <p style={{ whiteSpace: "pre-wrap" }}>{version.snapshot.content}</p>
        {version.snapshot.conditions?.length ? <p>适用条件：{version.snapshot.conditions.join("；")}</p> : null}
        <p>原来源：{[...(version.snapshot.source_experience_ids || []), ...(version.snapshot.source_recognition_ids || [])].join("、") || "无"}</p>
      </details>;
    })}
  </details>;
}
