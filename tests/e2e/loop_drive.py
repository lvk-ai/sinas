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


def start_stub(host: str, port: int, with_sandbox: bool) -> HTTPServer:
    _Stub.plan = staticmethod(make_plan(with_sandbox))
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

    @staticmethod
    def _json(text):
        try:
            value = json.loads(text or "")
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def setup(self, stub_endpoint):
        # The stub is never the deployment's default provider: it is bound to
        # the test agent only, so other chats keep their model when the stub
        # goes away. On a repeat run the provider's endpoint is refreshed.
        stub_provider_id = None
        if stub_endpoint:
            provs = self.ok(self.c.get("/api/v1/llm-providers", headers=self.h))
            existing = next((p for p in provs if p["name"] == "e2e-stub"), None)
            if existing:
                stub_provider_id = existing["id"]
                self.ok(self.c.patch(f"/api/v1/llm-providers/{stub_provider_id}", headers=self.h, json={"api_endpoint": stub_endpoint, "default_model": "stub", "is_active": True}))
            else:
                created = self.ok(self.c.post("/api/v1/llm-providers", headers=self.h, json={"name": "e2e-stub", "provider_type": "openai", "api_key": "stub", "api_endpoint": stub_endpoint, "default_model": "stub", "is_default": False}))
                stub_provider_id = created["id"]
            self.step(1, f"stub provider at {stub_endpoint} (not the default)")
        else:
            self.step(1, "using the deployment's default provider")
        ns, name = COLLECTION.split("/")
        r = self.c.post("/api/v1/collections", headers=self.h, json={"namespace": ns, "name": name})
        if r.status_code not in (201, 400, 409):
            self.ok(r)
        self.step(2, f"collection {COLLECTION} ready")
        inst = self.ok(self.c.post("/api/v1/packages/install", headers=self.h, json={"source": PACKAGE.read_text()}))
        self.step(3, f"package installed: {inst['package']['name']} {inst['package']['version']}")
        # The scripted run drives a COPY of the package's agent bound to the
        # stub; the package agent itself is never touched, so a later --real
        # run (and real users) keep its provider and the package keeps
        # managing it.
        agent_ref = "way-of-working/assistant"
        if stub_provider_id:
            source = self.ok(self.c.get("/api/v1/agents/way-of-working/assistant", headers=self.h))
            copy = {k: source.get(k) for k in (
                "description", "temperature", "max_tokens", "system_prompt", "input_schema", "output_schema",
                "initial_messages", "enabled_functions", "enabled_agents", "enabled_skills", "function_parameters",
                "status_templates", "enabled_queries", "query_parameters", "enabled_stores", "enabled_collections",
                "enabled_components", "enabled_connectors", "enabled_pipelines", "hooks", "icon", "system_tools",
                "tool_approvals", "default_job_timeout", "default_keep_alive",
            ) if source.get(k) is not None}
            copy.update({"namespace": "e2e", "name": "assistant", "llm_provider_id": stub_provider_id, "model": "stub"})
            r = self.c.put("/api/v1/agents/e2e/assistant", headers=self.h, json={k: v for k, v in copy.items() if k not in ("namespace", "name")})
            if r.status_code == 404:
                self.ok(self.c.post("/api/v1/agents", headers=self.h, json=copy))
            else:
                self.ok(r)
            agent_ref = "e2e/assistant"
            self.step(3, f"stub-bound copy of the package agent ready as {agent_ref}")
        chat = self.ok(self.c.post(f"/agents/{agent_ref}/chats", headers=self.h, json={"title": "e2e loop drive", "keep_alive": True}))
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
        # Follow suspensions until the turn is over: answer questions, approve
        # gates. A real model may pause more often than the scripted one;
        # whatever is still parked when this gives up fails verify().
        for _ in range(12 if self.real else 6):
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
        # The turn is over only when the last assistant message is a plain
        # reply: text, no tool calls, nothing parked behind it.
        last_assistant = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
        final = last_assistant if last_assistant and last_assistant.get("content") and not last_assistant.get("tool_calls") else None
        print("      final reply:", (final or {}).get("content", "")[:160])
        wb = sorted(f["name"] for f in self.ok(self.c.get(f"/chats/{self.chat_id}/workbench/files", headers=self.h)))
        ns, name = COLLECTION.split("/")
        published_files = self.ok(self.c.get(f"/files/{ns}/{name}", headers=self.h))
        published = sorted(f["name"] for f in published_files)
        cols = [f"{x['namespace']}/{x['name']}" for x in self.ok(self.c.get("/api/v1/collections", headers=self.h))]
        print("      workbench:", wb)
        print("      published:", published)
        failures = []
        if final is None:
            failures.append("turn did not end with a plain assistant reply")
        if st["pending_inputs"] or st["pending_approvals"]:
            failures.append(f"turn ended with pending items: {len(st['pending_inputs'])} question(s), {len(st['pending_approvals'])} approval(s)")
        # Judge THIS run's promote, not whatever an earlier run left in the
        # collection: the promote tool result must be a success and the
        # published file must be at the version that result reports.
        promote_results = [m for m in msgs if m["role"] == "tool" and m["name"] == "workbench_promote"]
        failed_tools = [m["name"] for m in msgs if m["role"] == "tool" and "error" in (self._json(m["content"]) or {})]
        if failed_tools:
            failures.append(f"tool calls failed this run: {failed_tools}")
        if not promote_results:
            failures.append("no workbench_promote ran this run")
        else:
            res = self._json(promote_results[-1]["content"]) or {}
            if "error" in res:
                failures.append(f"promote failed: {res['error']}")
            else:
                target = next((f for f in published_files if f["name"] == res.get("filename")), None)
                if target is None:
                    failures.append(f"promoted file {res.get('filename')!r} not found in {COLLECTION}")
                elif target.get("current_version") != res.get("version"):
                    failures.append(f"promoted file is at version {target.get('current_version')}, promote reported {res.get('version')}")
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
    ap.add_argument("--stub-host", default="127.0.0.1", help="interface the stub listens on; must be reachable from the backend")
    ap.add_argument("--stub-port", type=int, default=8999)
    ap.add_argument("--stub-endpoint", help="URL the backend should use for the stub, if not http://<stub-host>:<port>/v1")
    a = ap.parse_args()

    stub_endpoint = None
    if not a.real:
        start_stub(a.stub_host, a.stub_port, a.sandbox)
        stub_endpoint = a.stub_endpoint or f"http://{a.stub_host}:{a.stub_port}/v1"
    d = Driver(a.url, a.key, a.real, a.sandbox)
    d.setup(stub_endpoint)
    d.turn()
    d.verify()


if __name__ == "__main__":
    main()
