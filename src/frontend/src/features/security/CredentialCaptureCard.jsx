import { useRef, useState } from "react";
import "./credentialCaptureCard.css";

function commandId() {
  const suffix = globalThis.crypto?.randomUUID?.().toLowerCase() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `cmd-${suffix}`;
}

export function CredentialCaptureCard({
  credentialKind,
  credentialSubject,
  label = "凭据",
  configured = false,
  disabled = false,
  bridge = globalThis.electronAPI,
  onStored = () => {},
}) {
  const inputRef = useRef(null);
  const [status, setStatus] = useState("idle");
  const capture = bridge?.captureCredential;

  const submit = async (event) => {
    event.preventDefault();
    const input = inputRef.current;
    const value = input?.value || "";
    if (!value || typeof capture !== "function" || status === "saving") return;
    setStatus("saving");
    try {
      const result = await capture(credentialKind, credentialSubject, value, commandId());
      if (input) input.value = "";
      setStatus("stored");
      onStored(result);
    } catch {
      if (input) input.value = "";
      setStatus("error");
    }
  };

  if (typeof capture !== "function") {
    return <p className="credential-capture-card__warning">凭据只能在 Chriptmas OS 桌面应用中安全录入。</p>;
  }
  return (
    <form className="credential-capture-card" onSubmit={submit} aria-label={`${label}安全录入`}>
      <label className="credential-capture-card__field">
        <span>{label}</span>
        <input ref={inputRef} disabled={disabled || status === "saving"} type="password" autoComplete="off"
          defaultValue="" placeholder={configured ? "已安全保存，输入新值可轮换" : "输入后直达本机安全存储"} />
      </label>
      <div className="credential-capture-card__actions">
        <button className="credential-capture-card__button" type="submit" disabled={disabled || status === "saving"}>
          {status === "saving" ? "安全保存中" : configured ? "轮换凭据" : "安全保存"}
        </button>
        <small className={`credential-capture-card__status ${status}`} aria-live="polite">{status === "stored" ? "已写入本机 Secret Store，输入值未进入页面状态。" : status === "error" ? "凭据未保存，输入已清空。" : "输入不会进入聊天、模型上下文或普通 HTTP 请求。"}</small>
      </div>
    </form>
  );
}
