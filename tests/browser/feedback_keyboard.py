"""Keyboard-only Chromium component acceptance via existing CDP/websockets tooling.

Set COIRE_BROWSER_BIN to an installed Chromium executable. Copied fixture content
and browser profiles are private/temporary; no production API or credentials are used.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps/coire-web"
HARNESS = r"""
import React, {useState} from "react";
import {createRoot} from "react-dom/client";
import {FeedbackSettings} from "/src/components/chat/FeedbackSettings.tsx";
import {FeedbackControls} from "/src/components/chat/FeedbackControls.tsx";
import {Comparison} from "/src/components/chat/Comparison.tsx";
import {ReviewQueue} from "/src/components/feedback/ReviewQueue.tsx";
const stamp="2026-10-09T00:00:00Z", id="01ARZ3NDEKTSV4RRFFQ69G5FAV";
let preference={owner_id:"owner",enabled:false,capture_generation:1,version:1,changed_at:stamp,disclosure_version:"feedback-v1",disclosure:"Published datasets and trained adapters remain unchanged."};
let receipt={id,state:"ready",selection_state:"pending",version:1,expires_at:"2026-10-10T00:00:00Z",created_at:stamp};
let detail={...receipt,conversation_id:"conversation",source_message_id:"message",owner_id:"owner",target:{model_id:"model",variant_id:"variant"},prompt:[{role:"user",content:"private prompt"}],original:"private original",candidate:"private candidate",selected:null,owner_judgement:null,admin_judgement:null,owner_tags:[],admin_tags:[],eligibility:"eligible"};
let skipped=false, thumbVersion=0;
window.acceptance={calls:[]};
window.fetch=async (path,options={})=>{
 const body=options.body ? JSON.parse(options.body) : null;
 window.acceptance.calls.push({path,method:options.method||"GET",body});
 let value,status=200;
 if(path.endsWith("feedback-preference")) {
  if(body) preference={...preference,enabled:body.enabled,version:preference.version+1,capture_generation:preference.capture_generation+1};
  value=preference;
 } else if(path.includes("/messages/") && path.endsWith("/feedback")) value={id:"label",version:++thumbVersion,judgement:body.judgement};
 else if(path.endsWith("/selection")) {
  receipt={...receipt,version:receipt.version+1,selection_state:"chosen"};
  detail={...detail,...receipt,selected:detail.selected||body.candidate};
  value={comparison:receipt,feedback:{id:"owner-label",version:1,judgement:body.candidate}};
 } else if(path.endsWith("/judgement")) {
  skipped=body.choice==="skip";
  if(!skipped) detail.admin_judgement={id:"admin-label",version:1,judgement:body.choice};
  value={pair_id:id,skipped,judgement:detail.admin_judgement};
 } else if(path.includes("/admin/feedback/comparisons?")) {
  const state=new URL(path,location.origin).searchParams.get("state");
  value={items:preference.enabled && ((state==="skipped" && skipped)||(state==="unreviewed" && !skipped && !detail.admin_judgement)||(state==="reviewed" && detail.admin_judgement)) ? [detail] : [],next_cursor:null};
 } else if(path.includes("/comparisons/")) {value=detail; if(!preference.enabled) status=403;}
 else if(path.endsWith("/comparisons")) value=receipt;
 else {status=404;value={};}
 return new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json"}});
};
function Fixture(){
 const [enabled,setEnabled]=useState(false),[revision,setRevision]=useState(0);
 return <main><h1>Keyboard feedback acceptance</h1><p>Ordinary chat remains available.</p>
 <FeedbackSettings onChange={p=>{setEnabled(p.enabled);setRevision(p.version);}}/>
 <FeedbackControls conversationId="conversation" revision={revision} row={{message_id:"message",feedback:null,tags:[],eligibility:"eligible"}} enabled={enabled} complete onChange={()=>{}} onCreated={()=>{}}/>
 <Comparison conversationId="conversation" receipt={receipt} enabled={enabled} onChange={()=>{}}/>
 {enabled && <ReviewQueue/>}
 </main>;
}
createRoot(document.getElementById("root")).render(<Fixture/>);
"""


async def run_browser(socket: str, url: str) -> dict[str, object]:
    async with connect(socket, max_size=4 * 1024**2) as websocket:
        sequence = 0

        async def command(method: str, params: dict[str, object] | None = None) -> Any:
            nonlocal sequence
            sequence += 1
            identity = sequence
            await websocket.send(
                json.dumps({"id": identity, "method": method, "params": params or {}})
            )
            while True:
                value = json.loads(await websocket.recv())
                if value.get("id") == identity:
                    if "error" in value:
                        raise RuntimeError("CDP command failed: " + method)
                    return value.get("result", {})

        async def evaluate(expression: str) -> Any:
            result = await command(
                "Runtime.evaluate", {"expression": expression, "returnByValue": True}
            )
            if "exceptionDetails" in result:
                raise RuntimeError("Browser acceptance expression failed")
            return result["result"].get("value")

        async def wait(expression: str) -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if await evaluate(expression):
                    return
                await asyncio.sleep(0.05)
            raise RuntimeError("Browser acceptance state deadline exceeded: " + expression)

        async def key(name: str, code: int) -> None:
            key_code = {
                "Enter": "Enter",
                "Tab": "Tab",
                " ": "Space",
                "ArrowDown": "ArrowDown",
                "End": "End",
                "s": "KeyS",
            }[name]
            for kind in ("keyDown", "keyUp"):
                params: dict[str, object] = {
                    "type": kind,
                    "key": name,
                    "code": key_code,
                    "windowsVirtualKeyCode": code,
                    "nativeVirtualKeyCode": code,
                }
                if kind == "keyDown" and name in {"Enter", " ", "s"}:
                    params["text"] = "\r" if name == "Enter" else name
                    params["unmodifiedText"] = params["text"]
                await command("Input.dispatchKeyEvent", params)

        async def tab_to(label: str) -> None:
            for _ in range(100):
                current = await evaluate(
                    "document.activeElement.type === 'checkbox' ? 'Capture feedback' : document.activeElement.tagName === 'SELECT' ? 'Review queue' : document.activeElement.textContent.trim()"
                )
                if current == label:
                    return
                await key("Tab", 9)
            raise RuntimeError("Keyboard cannot reach " + label)

        await command("Page.enable")
        await command("Page.navigate", {"url": url})
        await wait("!!document.querySelector('input[type=checkbox]')")
        assert await evaluate(
            "document.body.innerText.includes('Published datasets and trained adapters remain unchanged')"
        )
        assert await evaluate(
            "[...document.querySelectorAll('button')].filter(b=>b.textContent.includes('Helpful')).every(b=>b.disabled)"
        )
        await tab_to("Capture feedback")
        await key(" ", 32)
        await wait(
            "document.querySelector('input[type=checkbox]').checked && ![...document.querySelectorAll('button')].find(b=>b.textContent==='Helpful answer').disabled"
        )
        await tab_to("Helpful answer")
        await key("Enter", 13)
        await wait("window.acceptance.calls.some(c=>c.method==='PUT' && c.body?.judgement==='up')")
        await tab_to("Compare another answer")
        await key("Enter", 13)
        await wait(
            "window.acceptance.calls.some(c=>c.method==='POST' && c.path.endsWith('/comparisons'))"
        )
        await tab_to("Choose alternative answer")
        await key("Enter", 13)
        await wait("document.body.innerText.includes('The alternative answer is active')")
        await tab_to("Skip for now")
        await key("Enter", 13)
        await wait("window.acceptance.calls.some(c=>c.body?.choice==='skip')")
        await tab_to("Review queue")
        await key("s", 83)
        await key("Enter", 13)
        await wait(
            "!![...document.querySelectorAll('button')].find(b=>b.textContent==='Prefer candidate')"
        )
        await tab_to("Prefer candidate")
        await key("Enter", 13)
        await wait("window.acceptance.calls.some(c=>c.body?.choice==='candidate')")
        await tab_to("Capture feedback")
        await key(" ", 32)
        await wait("!document.querySelector('input[type=checkbox]').checked")
        await wait(
            "!document.body.innerText.includes('private original') && !document.body.innerText.includes('private candidate')"
        )
        assert await evaluate(
            "document.body.innerText.includes('Ordinary chat remains available.')"
        )
        calls = await evaluate("window.acceptance.calls.length")
        return {
            "keyboard_only": True,
            "disclosure_before_enable": True,
            "thumb_compare_selection": True,
            "review_skip_revisit": True,
            "disable_erases_copies": True,
            "api_calls": calls,
        }


def main() -> None:
    binary = os.environ.get("COIRE_BROWSER_BIN")
    if not binary or not Path(binary).is_file():
        raise RuntimeError("COIRE_BROWSER_BIN must name an installed Chromium executable")
    directory = WEB / "dist"
    directory.mkdir(exist_ok=True)
    html, jsx = directory / ".018-keyboard.html", directory / ".018-keyboard.jsx"
    html.write_text(
        '<div id="root"></div><script type="module" src="/dist/.018-keyboard.jsx"></script>'
    )
    jsx.write_text(HARNESS)
    with tempfile.TemporaryDirectory(prefix="coire-018-browser-") as private:
        profile = Path(private)
        vite = subprocess.Popen(
            ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", "5198", "--strictPort"],
            cwd=WEB,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (profile / "DevToolsActivePort").is_file():
                    try:
                        if (
                            httpx.get(
                                "http://127.0.0.1:5198/dist/.018-keyboard.html", timeout=1
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Isolated browser or Vite did not become ready")
            port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            targets = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
            socket = next(row["webSocketDebuggerUrl"] for row in targets if row["type"] == "page")
            print(
                json.dumps(
                    asyncio.run(
                        run_browser(socket, "http://127.0.0.1:5198/dist/.018-keyboard.html")
                    )
                )
            )
        finally:
            for process in (browser, vite):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            html.unlink(missing_ok=True)
            jsx.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
