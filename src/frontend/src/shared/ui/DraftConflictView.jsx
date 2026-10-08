import "./draftConflict.css";

// Presentation only: callers own snapshots, validation and conflict resolution.
export function DraftConflictView({ baseDraft, editor, server, fields, confirmed, onChoose, disabled = false, unknownBase = false, compact = false }) {
  return <section className={`ws-draft-conflict ${compact ? "is-compact" : ""}`} role="alert" aria-label="草稿版本冲突">
    <strong>{compact ? "版本冲突 · 选择版本" : unknownBase ? "本机历史草稿缺少版本基线" : "草稿已由另一窗口更新"}</strong>
    {!compact && <p>{confirmed ? "此版本已入库，本地内容仍保留供复制。只能采用服务器版本，无法继续修改已入库草稿。" : unknownBase ? "请核对服务器正文和本机稿，再明确选择版本。保留本地内容后仍需重新保存。" : "你的修改仍在本机。请核对差异，再选择采用服务器版本，或保留本地内容继续编辑。继续编辑后仍需重新保存。"}</p>}
    {fields.length > 0 && <div className="ws-conflict-diff">{fields.map(([key, label]) => <div key={key} className="ws-conflict-row"><b>{label}</b><span>原版：{unknownBase ? "（基线未知）" : baseDraft?.[key] || "（空）"}</span><span>服务器：{server[key] || "（空）"}</span><span>本地：{editor?.[key] || "（空）"}</span></div>)}</div>}
    <div className="ws-conflict-actions"><button type="button" disabled={disabled} aria-label="采用服务器版本" onClick={() => onChoose(true)}>{compact ? "用服务端" : "采用服务器版本"}</button>{!confirmed && <button type="button" disabled={disabled} aria-label="保留本地内容继续编辑" onClick={() => onChoose(false)}>{compact ? "留本机" : "保留本地内容继续编辑"}</button>}</div>
  </section>;
}
