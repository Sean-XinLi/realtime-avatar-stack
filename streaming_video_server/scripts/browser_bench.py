#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websockets


class DevToolsClient:
    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self._ws = None
        self._next_id = 0
        self._event_queue: list[dict[str, object]] = []

    async def __aenter__(self) -> "DevToolsClient":
        self._ws = await websockets.connect(self.websocket_url, max_size=8 * 1024 * 1024)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._next_id += 1
        message_id = self._next_id
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"DevTools error for {method}: {message['error']}")
                return message.get("result", {})
            self._event_queue.append(message)

    async def next_event(self, timeout: float | None = None) -> dict[str, object]:
        if self._event_queue:
            return self._event_queue.pop(0)
        if timeout is None:
            return json.loads(await self._ws.recv())
        message = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        return json.loads(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the browser first-frame benchmark via headless Chrome")
    parser.add_argument("--server-url", default="http://127.0.0.1:18000")
    parser.add_argument("--chrome-path", default="google-chrome")
    parser.add_argument("--capture-ms", type=int, default=20)
    parser.add_argument("--input-chunk-ms", type=int, default=40)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--remote-debugging-port", type=int, default=9222)
    parser.add_argument("--legacy-wait-ice-complete", action="store_true")
    return parser


def fetch_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_devtools(port: int, timeout: float = 10.0) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = fetch_json(f"http://127.0.0.1:{port}/json/list")
            assert isinstance(payload, list)
            return payload
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"DevTools endpoint not available: {last_error}")


def launch_chrome(chrome_path: str, user_data_dir: str, port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            chrome_path,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={port}",
            "about:blank",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def run_once(args: argparse.Namespace, run_index: int) -> dict[str, object]:
    user_data_dir = tempfile.mkdtemp(prefix="chrome-bench-", dir="/tmp")
    process = launch_chrome(args.chrome_path, user_data_dir, args.remote_debugging_port)
    try:
        targets = wait_for_devtools(args.remote_debugging_port)
        page_target = next((item for item in targets if item.get("type") == "page"), None)
        if page_target is None or "webSocketDebuggerUrl" not in page_target:
            raise RuntimeError("no page target available in DevTools")

        bench_url = (
            f"{args.server_url.rstrip('/')}/debug-client/bench.html?"
            + urllib.parse.urlencode(
                {
                    "serverUrl": args.server_url.rstrip("/"),
                    "captureMs": args.capture_ms,
                    "inputChunkMs": args.input_chunk_ms,
                    "timeoutMs": args.timeout_ms,
                    "waitIceComplete": "1" if args.legacy_wait_ice_complete else "0",
                    "run": run_index,
                }
            )
        )

        async with DevToolsClient(str(page_target["webSocketDebuggerUrl"])) as client:
            await client.send("Runtime.enable")
            await client.send("Page.enable")
            await client.send("Page.navigate", {"url": bench_url})

            deadline = time.monotonic() + (args.timeout_ms / 1000.0) + 10.0
            while time.monotonic() < deadline:
                try:
                    event = await client.next_event(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if event.get("method") == "Runtime.exceptionThrown":
                    raise RuntimeError(json.dumps(event["params"], ensure_ascii=False))
                if event.get("method") != "Runtime.consoleAPICalled":
                    continue
                values = [item.get("value") for item in event.get("params", {}).get("args", [])]
                if len(values) >= 2 and values[0] == "BENCH_RESULT":
                    payload = json.loads(values[1])
                    payload["run"] = run_index
                    return payload
            raise RuntimeError("timed out waiting for BENCH_RESULT console event")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
        shutil.rmtree(user_data_dir, ignore_errors=True)


async def run_all(args: argparse.Namespace) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for run_index in range(1, args.runs + 1):
        results.append(await run_once(args, run_index))
        await asyncio.sleep(0.5)
    return results


def main() -> None:
    args = build_parser().parse_args()
    results = asyncio.run(run_all(args))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
