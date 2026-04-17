# avatar_console

A local web console that uses text to drive a `streaming_video_server` instance and play back synchronized audio and video.

## Features

- Writes input text to `input.txt`, splits it into short sentences locally, generates TTS audio per segment, and continuously sends those segments to the remote video service over browser WebRTC.
- Supports browser speech input: hold `Control` to speak, let the built-in STT write the transcript into `input.txt`, then continue with the existing `direct` or `reply` flow. If the browser does not report `Control` key events, hold the on-page button instead.
- For question input, the app first runs:

  ```bash
  cat input.txt | codex exec \
    -m gpt-5.3-codex-spark \
    -c model_reasoning_effort="low" \
    --sandbox read-only \
    --output-last-message output.txt \
    "Please answer based on the input only. Do not read other files. Output only the answer body with no explanation."
  ```

  The project listens to `codex exec --json` output and writes incremental reply text into `output.txt` whenever possible. If the CLI only emits a final message, the app still splits that final reply into segments and plays it back.
- In `reply` mode, the app also maintains `memory.json` in the project root. Each successful "start persistent session" resets it for a new session. After that, every round increments `turn`, updates `topic` and `summary`, and by default keeps only the most recent user/assistant pair in `last_chat_history`.
- The repo includes a reusable repo-local skill at `skills/structured-chat-memory/` for compressing structured conversation context into a refreshed memory summary.
- The browser directly plays the synchronized audio and video returned by the remote service.
- The browser first establishes a persistent WebRTC session. As long as that session is not stopped manually, it keeps sending a near-silent carrier stream. Generated speech segments are then inserted into that same uplink stream in order.

## Run

```bash
cd avatar_console
npm start
```

By default the app listens on `http://127.0.0.1:3010`, and the page initially pre-fills the stream server as `http://127.0.0.1:8080`.

Optional environment variables:

- `AVATAR_CONSOLE_PORT`: local service port, default `3010`
- `AVATAR_CONSOLE_STREAM_SERVER`: default stream server URL; falls back to `http://127.0.0.1:8080`
- `AVATAR_CONSOLE_TTS_VOICE`: default `say` voice; if unset, Chinese text prefers an installed Chinese voice such as `Tingting`
- `AVATAR_CONSOLE_PROMPT`: prompt used in question/answer mode; the server automatically prepends `memory.json` context before it
- `AVATAR_CONSOLE_CODEX_BIN`: path to the `codex` executable, default `codex`
- `AVATAR_CONSOLE_MEMORY_HISTORY_TURNS`: number of recent dialogue pairs kept in `memory.json.last_chat_history`, default `1`; set it to `5` to keep the last `5` pairs

The current version also accepts the legacy `CONTROL_AVATAR_*` prefix for smoother migration.

## Requirements

- macOS built-in `say` and `afconvert`
- a working `codex exec`
- a reachable `streaming_video_server`

## Page Flow

1. Enter the remote server address.
   The page remembers the last manually edited address and prefers that saved value after reload.
2. Enter text in the chat box on the right.
3. Choose one of:
   - `Read input.txt directly`
   - `Answer and read output.txt`
   - Or choose a post-speech action first, then hold `Control` to speak. On release, STT writes to `input.txt` and continues with the selected flow. If the browser does not report `Control` key events, hold the on-page button instead.
4. Click `Start persistent session`.
   The browser keeps a near-silent uplink stream alive. At the same time, the local app clears `input.txt` and `output.txt`, and resets `memory.json` to the initial structure for a new session.
5. After that, every text or speech input is split into short sentences and sent through the same stream, while returned audio and video are played in the page.
6. In `reply` mode, once the answer is complete, the app updates `memory.json` so the next turn can reuse the accumulated context.

## Troubleshooting

- If remote logs show `Components {1} have no candidate pairs`, WebRTC failed during ICE and the returned video never started.
- The project now waits for the first usable local ICE candidate before sending the offer, which reduces the chance that the remote side fails before trickle candidates arrive.
- Following the current `streaming_video_server` flow, the page in `tailscale` mode also requests microphone permission once before creating `RTCPeerConnection`; the actual sent track is not direct microphone passthrough, but a browser-generated near-silent carrier plus later inserted TTS audio.
- If the remote service runs in `tailscale` mode and the page shows `no usable local ICE candidate found for tailscale mode`, the browser is usually hiding the local Tailscale address behind an mDNS hostname, so the remote side never receives a usable `100.x.x.x` candidate.
- Speech input depends on `SpeechRecognition / webkitSpeechRecognition`. If the page says speech input is unsupported, prefer Chrome or Safari on localhost and make sure microphone permission is allowed.
