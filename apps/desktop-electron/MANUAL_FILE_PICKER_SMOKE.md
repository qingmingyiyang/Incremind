# Manual Smoke: Real OS File Picker

This record covers the real native OS file picker in the Electron shell.

It is different from `npm run smoke:file-picker`. The automated smoke starts a real BrowserWindow but returns a controlled path from the test IPC handler, so it does not open the native OS dialog. Use this manual smoke when the goal is to confirm the actual Windows file chooser.

## Scope

Validate that the user-triggered file picker can fill the local authorization path field for:

- image Source authorization
- audio Source authorization
- video Source authorization
- document Source authorization
- canceling the dialog

Document selection uses a dedicated PDF / DOC / DOCX filter in `src/main.cjs`. Image, audio, and video use dedicated extension filters.

## Preconditions

1. Start the frontend entry that contains Library overview and Source detail actions.
2. Start the backend with rebuild endpoints available.
3. Open the Electron shell:

```powershell
cd F:\Chriptmas_OS\apps\desktop-electron
$env:CHRIPTMAS_REPLAY_FRONTEND_URL="http://127.0.0.1:4173/?view=rebuild-library-overview"
$env:CHRIPTMAS_REPLAY_BACKEND_URL="http://127.0.0.1:8001"
npm run dev
```

4. Prepare one test Source for each media kind: image, audio, video, and document.

## Steps

For each image, audio, video, and document Source:

1. Open the Source detail in Library overview.
2. Click `选择本地文件`.
3. Confirm that the native Windows file picker opens.
4. Select a harmless local test file for that media kind.
5. Confirm that only the visible local path input is filled.
6. Do not click the authorization button unless this smoke is intentionally extended to cover authorization.

For cancel behavior:

1. Click `选择本地文件`.
2. Cancel the native Windows file picker.
3. Confirm that the path input remains unchanged or empty.

## Pass Criteria

- The native OS file picker opens from a user action.
- Selecting an image, audio, video, or document test file fills the path input.
- The document dialog offers the PDF / DOC / DOCX filter.
- Canceling the dialog does not create a path or modify authorization state.
- No file bytes are read by the picker action.
- No file path is authorized until the user clicks the explicit authorization action.
- No OCR, audio transcription, video frame extraction, document text extraction, Provider command, Memory Candidate generation, staging atom creation, or long-term Memory publication is triggered by file selection.
- The renderer still does not expose Node `require` or `process`.

## Manual Record Template

```text
Date:
Operator:
OS:
Electron version:
Frontend URL:
Backend URL:

Image source:
- Test extension:
- Native dialog opened:
- Path input filled:
- No authorization / Provider / Memory side effect:
- Result:

Audio source:
- Test extension:
- Native dialog opened:
- Path input filled:
- No authorization / Provider / Memory side effect:
- Result:

Video source:
- Test extension:
- Native dialog opened:
- Path input filled:
- No authorization / Provider / Memory side effect:
- Result:

Document source:
- Test extension:
- Native dialog opened:
- Path input filled:
- Document filter observed:
- No authorization / Provider / Memory side effect:
- Result:

Cancel:
- Input unchanged:
- No authorization / Provider / Memory side effect:
- Result:

Screenshots or notes:
```

## Current Status

No completed human-run record is claimed in this file. This file defines the manual smoke procedure and acceptance criteria only.
