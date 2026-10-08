const kind = document.getElementById("kind");
const text = document.getElementById("text");
const actions = document.getElementById("actions");
const status = document.getElementById("status");
const composer = document.getElementById("composer");
const input = document.getElementById("input");
let currentEvent = null;

const KIND_LABELS = Object.freeze({
  reminder: "提醒",
  reminder_due: "提醒",
  ambient_idle: "陪伴消息",
  random_event: "陪伴消息",
  random_event_result: "陪伴消息",
  clipboard_changed: "剪贴板提醒",
  clipboard_comment: "剪贴板提醒",
  clipboard_eaten: "剪贴板提醒",
  clipboard_restored: "剪贴板提醒",
  choice: "需要选择",
  status: "状态提醒",
  routine_sleep: "休息时间",
  routine_morning: "早安问候",
  petting: "陪伴消息",
  petting_cooldown: "陪伴消息",
  focus_warning: "专注提醒",
  focus_complete: "完成提醒",
  peer_short_line: "伙伴消息",
});

function labelForKind(value) {
  return KIND_LABELS[value] || "陪伴消息";
}

function renderEvent(event) {
  currentEvent = event;
  kind.textContent = labelForKind(event?.kind);
  text.textContent = typeof event?.text === "string" && event.text ? event.text : "宠物正在这里陪着你。";
  actions.replaceChildren();
  const projectedActions = Array.isArray(event?.actions) ? event.actions.slice(0, 1) : [];
  for (const action of projectedActions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await globalThis.companionOverlay.performCompanionAction({ event_id: event.event_id, action_id: action.id });
        status.textContent = "操作已完成。";
      } catch {
        button.disabled = false;
        status.textContent = "操作暂时无法完成。";
      }
    });
    actions.append(button);
  }
  if (event?.requires_ack === true && projectedActions.length === 0) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "知道了";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await globalThis.companionOverlay.acknowledgeCompanionEvent(event.event_id);
      } catch {
        button.disabled = false;
        status.textContent = "暂时无法确认。";
      }
    });
    actions.append(button);
  }
}

globalThis.companionOverlay.subscribeCompanionOverlay(renderEvent);
document.getElementById("close").addEventListener("click", () => globalThis.companionOverlay.closeCompanionOverlay());
document.getElementById("open-center").addEventListener("click", () => globalThis.companionOverlay.openCompanionCenter("chat"));
composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = input.value.trim();
  if (!value) return;
  const requestId = `overlay-${Date.now().toString(36)}`;
  try {
    await globalThis.companionOverlay.submitCompanionText({ request_id: requestId, text: value });
    input.value = "";
    status.textContent = "消息已提交。";
  } catch {
    status.textContent = "聊天运行时尚未接入。";
  }
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") globalThis.companionOverlay.closeCompanionOverlay();
});
