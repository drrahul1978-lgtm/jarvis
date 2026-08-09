#!/usr/bin/env python3
"""Headless Jarvis: a small HTTP front-end for a Raspberry Pi on your LAN.

Talk to the Pi from a laptop or phone browser instead of over SSH.

    python3 serve.py                    # localhost only (default, safe)
    JARVIS_BIND=0.0.0.0 python3 serve.py  # reachable from the LAN

SECURITY: this speaks plain HTTP and has no accounts. Anything that can reach
the port can talk to Jarvis and read its memories. Set JARVIS_TOKEN to require
a shared secret, keep it on a trusted network, and do not port-forward it to
the open internet.
"""

import html
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, hardware, persona, tools  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.memory import Memory  # noqa: E402

BIND = os.environ.get("JARVIS_BIND", "127.0.0.1")
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
TOKEN = os.environ.get("JARVIS_TOKEN", "")

_lock = threading.Lock()  # one model, one conversation, one request at a time
_brain = Brain()
_memory = Memory()
_messages: list[dict] = []

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>
 :root{{color-scheme:dark light}}
 body{{font:16px/1.55 system-ui,sans-serif;max-width:44rem;margin:0 auto;padding:1rem}}
 h1{{font-size:1rem;letter-spacing:.18em;text-transform:uppercase;opacity:.6}}
 #log{{white-space:pre-wrap;word-wrap:break-word}}
 .u{{font-weight:600;margin-top:1.4rem}}
 .j{{margin-top:.3rem}}
 .s{{opacity:.55;font-size:.85em;font-style:italic}}
 form{{display:flex;gap:.5rem;position:sticky;bottom:0;padding:.75rem 0;
       background:Canvas}}
 input{{flex:1;padding:.6rem;font:inherit;border:1px solid;border-radius:.4rem;
        background:Canvas;color:CanvasText}}
 button{{padding:.6rem 1rem;font:inherit;border-radius:.4rem;cursor:pointer}}
</style>
<h1>{name}</h1>
<div id=log></div>
<form id=f><input id=q autocomplete=off placeholder="Ask something..." autofocus>
<button>Send</button></form>
<script>
const log=document.getElementById('log'),f=document.getElementById('f'),
      q=document.getElementById('q'),tok=new URLSearchParams(location.search).get('token')||'';
function add(cls,text){{const d=document.createElement('div');d.className=cls;
  d.textContent=text;log.appendChild(d);scrollTo(0,document.body.scrollHeight);return d}}
f.onsubmit=async e=>{{
  e.preventDefault();const text=q.value.trim();if(!text)return;
  q.value='';q.disabled=true;add('u',text);const out=add('j','');
  try{{
    const r=await fetch('/chat',{{method:'POST',headers:{{'Content-Type':'application/json',
      'X-Token':tok}},body:JSON.stringify({{message:text}})}});
    if(!r.ok){{out.textContent='['+r.status+'] '+await r.text();return}}
    const rd=r.body.getReader(),dec=new TextDecoder();
    for(;;){{const{{done,value}}=await rd.read();if(done)break;
      out.textContent+=dec.decode(value,{{stream:true}});
      scrollTo(0,document.body.scrollHeight)}}
  }}catch(err){{out.textContent='Connection lost: '+err.message}}
  finally{{q.disabled=false;q.focus()}}
}};
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Jarvis"

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _authorised(self) -> bool:
        if not TOKEN:
            return True
        supplied = self.headers.get("X-Token", "")
        return secrets.compare_digest(supplied, TOKEN)

    def _send(self, code: int, body: bytes, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            page = PAGE.format(name=html.escape(config.NAME))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/health":
            body = json.dumps(
                {
                    "ok": _brain.is_up(),
                    "model": _brain.model,
                    "hardware": hardware.summary(),
                }
            ).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404, b"Not found.")

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, b"Not found.")
            return
        if not self._authorised():
            self._send(401, b"Bad or missing token.")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            message = str(payload.get("message", "")).strip()
        except (ValueError, json.JSONDecodeError):
            self._send(400, b"Expected JSON: {\"message\": \"...\"}")
            return
        if not message:
            self._send(400, b"Empty message.")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        with _lock:
            self._run_turn(message)

    def _run_turn(self, message: str) -> None:
        if not _messages:
            _messages.append(
                {"role": "system", "content": persona.build_system_prompt(_memory)}
            )
        else:
            _messages[0] = {
                "role": "system",
                "content": persona.build_system_prompt(_memory),
            }

        _messages.append({"role": "user", "content": message})
        _memory.log("user", message)

        def write(text: str) -> None:
            self.wfile.write(text.encode())
            self.wfile.flush()

        answer = ""
        try:
            for kind, data in _brain.ask(_messages):
                if kind == "token":
                    write(data)
                elif kind == "tool":
                    write(f"\n[{data['name']}]\n")
                elif kind == "error":
                    write(f"\n[error] {data}\n")
                    return
                elif kind == "done":
                    answer = data
        except (BrokenPipeError, ConnectionResetError):
            return  # client wandered off mid-answer

        if answer:
            _memory.log("assistant", answer)

        limit = 1 + config.HISTORY_TURNS * 2
        if len(_messages) > limit:
            del _messages[1 : len(_messages) - limit + 1]


def main() -> int:
    _memory.start_session()
    tools.bind_memory(_memory)

    if not _brain.is_up():
        print(f"No Ollama server at {_brain.host}. Start it with: ollama serve")
        return 1
    if not _brain.has_model():
        print(f"Model '{_brain.model}' not downloaded. Run: ollama pull {_brain.model}")
        return 1

    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"{config.NAME} listening on http://{BIND}:{PORT}")
    print(f"  hardware : {hardware.summary()}")
    print(f"  model    : {_brain.model}")
    if BIND != "127.0.0.1" and not TOKEN:
        print("  WARNING  : exposed on the network with no token set.")
        print("             Set JARVIS_TOKEN to require a shared secret.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
        _memory.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
