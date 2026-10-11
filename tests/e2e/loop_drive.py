#!/usr/bin/env python3
"""Drive the Claude Code-style loop against a running Sinas over HTTP.

Staging gate for the way-of-working stack. It installs the shipped
packages/way-of-working.yaml, creates a keep-alive chat with its exemplar
agent, uploads a CSV to the workbench and follows one turn through:

  workbench_list -> workbench_read -> [execute_code] -> workbench_write
  -> ask_user (round suspends) -> answer -> resume
  -> workbench_promote (approval gate) -> approve -> resume -> final reply

then checks the transcript, the workbench, the target collection and that
the workbench never shows up as a collection.

Two ways to play the model:

  --stub (default)  a scripted OpenAI-compatible server started in-process
                    and registered as the default LLM provider. Every step
                    is asserted. Pass --sandbox to add a real execute_code
                    step (needs a sandbox executor on the deployment).
  --real            use the deployment's own default provider. The turn is
                    driven by a real model, so only the outcome is checked
                    (something promoted, a final reply); the transcript is
                    printed for a human to read.

Usage:
  python tests/e2e/loop_drive.py --url http://localhost:8000 --key sk-...
  python tests/e2e/loop_drive.py --url https://staging... --key sk-... --sandbox
  python tests/e2e/loop_drive.py --url https://staging... --key sk-... --real

Needs: an admin API key (sinas.*:all). Leaves the installed package, the
e2e/reports collection and the chat in place for inspection.
"""
import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "way-of-working.yaml"
COLLECTION = "e2e/reports"
INPUT_CSV = "id,amount\n1,10\n2,20\n3,30\n"
PROMPT = "Clean input/orders.csv, write out/summary.md and publish it to a collection."


# ---------------------------------------------------------------------------
# Scripted model
# ---------------------------------------------------------------------------

def _suffix(messages):
    """Per-conversation tag for tool-call ids, taken from the run id the driver
    puts in the opening user message. If the opener ever falls out of the
    windowed history mid-turn the tag changes, our own earlier calls stop
    matching and the script restarts from workbench_list — so a loop here
    also means the opener was not pinned."""
    for m in messages:
        if m.get("role") == "user":
            found = re.search(r"\(run ([0-9a-f-]+)\)", str(m.get("content") or ""))
            if found:
                return hashlib.sha1(found.group(1).encode()).hexdigest()[:6]
    return "norun"


_DONE: dict[str, set] = {}


def _answered(messages):
    """Our own calls that have a result, remembered per run: with a small
    history window the early results leave the model's view, and a scripted
    model has no compaction summary to tell it they happened."""
    sfx = _suffix(messages)
    seen = _DONE.setdefault(sfx, set())
    seen |= {m.get("tool_call_id", "").removesuffix("_" + sfx) for m in messages if m.get("role") == "tool"}
    return seen


def _tool_result(messages, call_id):
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") == call_id + "_" + _suffix(messages):
            return m.get("content") or ""
    return ""


def make_wakeups_plan():
    """Turn 1 arms all three wakeup paths and ends; every later turn is a
    delivery ([background job ...], [wakeup ...], [watch ...]) answered with
    text."""
    def plan(messages):
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        if str(last_user.get("content") or "").startswith("["):
            return "text", "Noted the delivery: " + str(last_user.get("content"))[:40]
        done = _answered(messages)
        if "call_watch" not in done:
            return "tool", ("call_watch", "create_watch", {"description": "CI result"})
        if "call_bg" not in done:
            return "tool", ("call_bg", "run_in_background", {"code": "print('hello from the background')\n41 + 1", "description": "count things"})
        if "call_timer" not in done:
            return "tool", ("call_timer", "schedule_wakeup", {"delay_seconds": 1, "note": "check whether anything arrived"})
        return "text", "Armed a watch, a background run and a wakeup; ending my turn."
    return plan


def make_plan(with_sandbox: bool):
    def plan(messages):
        done = _answered(messages)
        if "call_list" not in done:
            return "tool", ("call_list", "workbench_list", {})
        if "call_read" not in done:
            return "tool", ("call_read", "workbench_read", {"filename": "input/orders.csv"})
        if with_sandbox and "call_code" not in done:
            return "tool", ("call_code", "execute_code", {
                "description": "total the orders",
                "code": (
                    "import csv, pathlib\n"
                    "rows = list(csv.DictReader(open('input/orders.csv')))\n"
                    "total = sum(int(r['amount']) for r in rows)\n"
                    "pathlib.Path('out').mkdir(exist_ok=True)\n"
                    "pathlib.Path('out/computed.txt').write_text(f'{len(rows)} orders, total {total}\\n')\n"
                    "total\n"
                ),
            })
        if "call_write" not in done:
            return "tool", ("call_write", "workbench_write", {"filename": "out/summary.md", "content": "# Summary\n\n3 orders, total 60.\n"})
        if "call_ask" not in done:
            return "tool", ("call_ask", "ask_user", {"question": "Which collection should I publish to?", "options": [COLLECTION]})
        if "call_promote" not in done:
            try:
                target = json.loads(_tool_result(messages, "call_ask")).get("answer") or COLLECTION
            except Exception:
                target = COLLECTION
            return "tool", ("call_promote", "workbench_promote", {"filename": "out/summary.md", "collection": target})
        return "text", "Done. Published out/summary.md to the collection."
    return plan


class _Stub(BaseHTTPRequestHandler):
    plan = None

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        kind, payload = self.plan(body["messages"])
        cid = "chatcmpl-" + uuid.uuid4().hex[:8]
        if kind == "tool":
            call_id, name, args = payload
            payload = (call_id + "_" + _suffix(body["messages"]), name, args)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def chunk(delta, finish=None):
                self.wfile.write(("data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode())

            if kind == "text":
                chunk({"role": "assistant", "content": payload}); chunk({}, "stop")
            else:
                call_id, name, args = payload
                chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
                chunk({}, "tool_calls")
            self.wfile.write(("data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if kind == "text":
            msg, finish = {"role": "assistant", "content": payload}, "stop"
        else:
            call_id, name, args = payload
            msg, finish = {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls"
        out = json.dumps({"id": cid, "object": "chat.completion", "created": int(time.time()), "model": body["model"], "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_stub(host: str, port: int, with_sandbox: bool, wakeups: bool = False) -> HTTPServer:
    _Stub.plan = staticmethod(make_wakeups_plan() if wakeups else make_plan(with_sandbox))
    server = HTTPServer((host, port), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class Driver:
    def __init__(self, url: str, key: str, real: bool, with_sandbox: bool):
        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=300)
        self.h = {"X-API-Key": key}
        self.real = real
        self.with_sandbox = with_sandbox
        self.chat_id = None

    def ok(self, r, *codes):
        allowed = codes or (200, 201, 202)
        if r.status_code not in allowed:
            sys.exit(f"FAIL {r.request.method} {r.request.url} -> {r.status_code}\n{r.text[:800]}")
        return r.json() if r.content else None

    def step(self, n, msg):
        print(f"[{n:>2}] {msg}", flush=True)

    def consume(self, resp):
        seen, ev = [], None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:") and ev not in (None, "ping"):
                seen.append((ev, line[5:].strip()))
                if ev in ("done", "error"):
                    break
        for e, d in seen:
            if e != "message":
                print("      ", e, d[:140])
        if any(e == "error" for e, _ in seen):
            sys.exit("FAIL stream error")
        return seen

    def state(self):
        return self.ok(self.c.get(f"/chats/{self.chat_id}", headers=self.h))

    def setup(self, stub_endpoint):
        if stub_endpoint:
            provs = self.ok(self.c.get("/api/v1/llm-providers", headers=self.h))
            if not any(p["name"] == "e2e-stub" for p in provs):
                self.ok(self.c.post("/api/v1/llm-providers", headers=self.h, json={"name": "e2e-stub", "provider_type": "openai", "api_key": "stub", "api_endpoint": stub_endpoint, "default_model": "stub", "is_default": True}))
            self.step(1, f"stub provider registered at {stub_endpoint}")
        else:
            self.step(1, "using the deployment's default provider")
        ns, name = COLLECTION.split("/")
        r = self.c.post("/api/v1/collections", headers=self.h, json={"namespace": ns, "name": name})
        if r.status_code not in (201, 400, 409):
            self.ok(r)
        self.step(2, f"collection {COLLECTION} ready")
        inst = self.ok(self.c.post("/api/v1/packages/install", headers=self.h, json={"source": PACKAGE.read_text()}))
        self.step(3, f"package installed: {inst['package']['name']} {inst['package']['version']}")
        chat = self.ok(self.c.post("/agents/way-of-working/assistant/chats", headers=self.h, json={"title": "e2e loop drive", "keep_alive": True}))
        self.chat_id = chat["id"]
        self.step(4, f"chat {self.chat_id} (keep_alive)")
        self.ok(self.c.post(f"/chats/{self.chat_id}/workbench/files", headers=self.h, json={"name": "input/orders.csv", "content_base64": base64.b64encode(INPUT_CSV.encode()).decode()}))
        self.step(5, "uploaded input/orders.csv to the workbench")

    def turn(self):
        with self.c.stream("POST", f"/chats/{self.chat_id}/messages/stream", headers=self.h, json={"content": f"{PROMPT} (run {self.chat_id})"}) as resp:
            if resp.status_code != 200:
                sys.exit(f"FAIL stream {resp.status_code}: {resp.read()[:400]}")
            self.step(6, "turn 1 streamed:")
            self.consume(resp)
        # Follow suspensions until the turn is over: answer questions, approve gates.
        for _ in range(6):
            st = self.state()
            if st["pending_inputs"]:
                q = st["pending_inputs"][0]
                ans = self.ok(self.c.post(f"/chats/{self.chat_id}/pending-input/{q['tool_call_id']}", headers=self.h, json={"answer": COLLECTION}))
                self.step(7, f"answered '{q['question'][:50]}' -> resumed={ans.get('resumed')} channel={ans.get('channel_id')}")
                if ans.get("channel_id"):
                    with self.c.stream("GET", f"/chats/{self.chat_id}/stream/{ans['channel_id']}", headers=self.h) as resp:
                        self.consume(resp)
                continue
            if st["pending_approvals"]:
                a = st["pending_approvals"][0]
                ap = self.ok(self.c.post(f"/chats/{self.chat_id}/approve-tool/{a['tool_call_id']}", headers=self.h, json={"approved": True}))
                self.step(8, f"approved {a['function_name']} -> channel={ap.get('channel_id')}")
                if ap.get("channel_id"):
                    with self.c.stream("GET", f"/chats/{self.chat_id}/stream/{ap['channel_id']}", headers=self.h) as resp:
                        self.consume(resp)
                continue
            break

    def verify(self):
        st = self.state()
        msgs = st["messages"]
        tools_seen = [m["name"] for m in msgs if m["role"] == "tool"]
        print("      transcript tools:", tools_seen)
        final = next((m for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), None)
        print("      final reply:", (final or {}).get("content", "")[:160])
        wb = sorted(f["name"] for f in self.ok(self.c.get(f"/chats/{self.chat_id}/workbench/files", headers=self.h)))
        ns, name = COLLECTION.split("/")
        published = sorted(f["name"] for f in self.ok(self.c.get(f"/files/{ns}/{name}", headers=self.h)))
        cols = [f"{x['namespace']}/{x['name']}" for x in self.ok(self.c.get("/api/v1/collections", headers=self.h))]
        print("      workbench:", wb)
        print("      published:", published)
        failures = []
        if final is None:
            failures.append("no final assistant reply")
        if not any(n.endswith("summary.md") for n in published):
            failures.append(f"nothing promoted into {COLLECTION}")
        if any(c.startswith("_chat/") for c in cols):
            failures.append("workbench leaked into the collections listing")
        if not self.real:
            expected = ["workbench_list", "workbench_read"] + (["execute_code"] if self.with_sandbox else []) + ["workbench_write", "ask_user", "workbench_promote"]
            if tools_seen != expected:
                failures.append(f"tool sequence {tools_seen} != {expected}")
            if "out/summary.md" not in wb:
                failures.append("out/summary.md missing from the workbench")
            if self.with_sandbox and "out/computed.txt" not in wb:
                failures.append("execute_code output out/computed.txt did not persist to the workbench")
            if not st["pending_inputs"] == [] or not st["pending_approvals"] == []:
                failures.append("turn ended with pending items")
        if failures:
            print("E2E FAIL"); [print("   -", f) for f in failures]
            sys.exit(1)
        print("E2E PASS")


class WakeupsDriver(Driver):
    """--wakeups: the agent arms a watch, a background run and a timer, ends
    its turn, and each delivery wakes it (a new turn, or an injection if one
    is already running). Checks all three deliveries and replies landed."""

    def turn(self):
        with self.c.stream("POST", f"/chats/{self.chat_id}/messages/stream", headers=self.h, json={"content": f"Arm the wakeups. (run {self.chat_id})"}) as resp:
            if resp.status_code != 200:
                sys.exit(f"FAIL stream {resp.status_code}: {resp.read()[:400]}")
            self.step(6, "turn 1 streamed:")
            self.consume(resp)
        msgs = self.state()["messages"]
        watch = next((json.loads(m["content"]) for m in msgs if m["role"] == "tool" and m["name"] == "create_watch"), None)
        if not watch or "url" not in watch:
            sys.exit(f"FAIL create_watch gave no url: {watch}")
        self.step(7, f"watch url {watch['url']}")
        # The URL is built from the deployment's public base; post to the same
        # path on the base we are talking to.
        url = "/watch/" + watch["url"].rsplit("/", 1)[1]
        r = self.c.post(url, json={"conclusion": "success", "run": 42})
        self.step(8, f"POST watch -> {r.status_code} {r.text[:80]}")
        if r.status_code != 202:
            sys.exit("FAIL watch delivery rejected")
        # Wait for the three deliveries and the agent's replies to them.
        deadline = time.time() + 150
        while time.time() < deadline:
            msgs = self.state()["messages"]
            users = [m["content"] for m in msgs if m["role"] == "user" and (m["content"] or "").startswith("[")]
            prefixes = {u.split(" ", 1)[0] for u in users}
            replies = [m for m in msgs if m["role"] == "assistant" and (m["content"] or "").startswith("Noted the delivery")]
            if {"[watch", "[background", "[wakeup"} <= prefixes and len(replies) >= 1 and not self.state()["active_channel_id"] is None and len(replies) >= len(users) - 0:
                break
            if {"[watch", "[background", "[wakeup"} <= prefixes and len(replies) >= 1:
                # injected deliveries share one reply; give the running turn a moment to finish
                time.sleep(3)
                msgs = self.state()["messages"]
                break
            time.sleep(3)
        self.deliveries = [m["content"] for m in msgs if m["role"] == "user" and (m["content"] or "").startswith("[")]
        self.replies = [m["content"] for m in msgs if m["role"] == "assistant" and (m["content"] or "").startswith("Noted the delivery")]

    def verify(self):
        print("      deliveries:")
        for d in self.deliveries:
            print("        ", d.replace("\n", " | ")[:120])
        print("      replies:", len(self.replies))
        failures = []
        prefixes = {d.split(" ", 1)[0] for d in self.deliveries}
        for p in ("[watch", "[background", "[wakeup"):
            if p not in prefixes:
                failures.append(f"no {p} delivery arrived")
        if not self.replies:
            failures.append("the agent never replied to a delivery")
        if any("conclusion" not in d for d in self.deliveries if d.startswith("[watch")):
            failures.append("watch payload missing from the delivery")
        if failures:
            print("E2E FAIL"); [print("   -", f) for f in failures]
            sys.exit(1)
        print("E2E PASS")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", required=True, help="admin API key (sinas.*:all)")
    ap.add_argument("--real", action="store_true", help="drive with the deployment's default provider instead of the stub")
    ap.add_argument("--sandbox", action="store_true", help="add an execute_code step (needs a sandbox executor)")
    ap.add_argument("--wakeups", action="store_true", help="instead of the loop, arm a watch, a background run and a timer and check each wakes the agent")
    ap.add_argument("--stub-host", default="127.0.0.1", help="interface the stub listens on; must be reachable from the backend")
    ap.add_argument("--stub-port", type=int, default=8999)
    ap.add_argument("--stub-endpoint", help="URL the backend should use for the stub, if not http://<stub-host>:<port>/v1")
    a = ap.parse_args()

    stub_endpoint = None
    if not a.real:
        start_stub(a.stub_host, a.stub_port, a.sandbox, wakeups=a.wakeups)
        stub_endpoint = a.stub_endpoint or f"http://{a.stub_host}:{a.stub_port}/v1"
    if a.wakeups and a.real:
        sys.exit("--wakeups needs the scripted model")
    d = (WakeupsDriver if a.wakeups else Driver)(a.url, a.key, a.real, a.sandbox)
    d.setup(stub_endpoint)
    d.turn()
    d.verify()


if __name__ == "__main__":
    main()
