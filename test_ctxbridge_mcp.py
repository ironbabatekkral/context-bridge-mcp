#!/usr/bin/env python3
"""Unit and stdio subprocess tests for ctxbridge_mcp.py."""

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctxbridge  # noqa: E402
import ctxbridge_mcp  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _make_fixtures(root: Path, home: Path) -> tuple[Path, Path, Path]:
    omp_dir = root / "omp_agent"
    agy_dir = root / "agy_root"
    handoff_dir = root / "handoffs"

    proj_dir = home / "projects" / "demo-app"
    proj_dir.mkdir(parents=True, exist_ok=True)

    # OMP blob + session JSONL + history.db
    blobs_dir = omp_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    png_hash = hashlib.sha256(TINY_PNG).hexdigest()
    (blobs_dir / png_hash).write_bytes(TINY_PNG)
    (blobs_dir / f"{png_hash}.png").write_bytes(TINY_PNG)
    blob_ref = f"blob:sha256:{png_hash}"

    sess_dir = omp_dir / "sessions" / "--demo--"
    sess_dir.mkdir(parents=True, exist_ok=True)
    sess_file = sess_dir / "2026-11-19T01-00-00Z_omp12345678.jsonl"
    fake_sk = "sk-" + ("A" * 20)
    lines = [
        {"type": "session", "id": "omp12345678", "cwd": str(proj_dir), "timestamp": "2026-11-19T01:00:00Z"},
        {"type": "title", "title": "Fix auth flow in demo-app"},
        {
            "type": "message",
            "timestamp": "2026-11-19T01:00:10Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Initial check before compaction"},
                    {"type": "image", "data": blob_ref, "mimeType": "image/png"},
                ],
            },
        },
        {"type": "message", "timestamp": "2026-11-19T01:00:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Looked at initial auth files."}]}},
        {"type": "compaction", "timestamp": "2026-11-19T01:01:00Z", "summary": f"OMP checked {proj_dir} and saw token={fake_sk}"},
        {"type": "message", "timestamp": "2026-11-19T01:02:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "Please check login.py"}]}},
        {"type": "message", "timestamp": "2026-11-19T01:02:10Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Inspecting login.py..."}]}},
        {"type": "message", "timestamp": "2026-11-19T01:02:15Z", "message": {"role": "toolResult", "content": [{"type": "image", "data": blob_ref, "mimeType": "image/png"}]}},
        {"type": "message", "timestamp": "2026-11-19T01:02:20Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed CORS bug in login.py"}]}},
    ]
    sess_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    hdb = sqlite3.connect(omp_dir / "history.db")
    hdb.execute("CREATE TABLE session_titles (session_id TEXT, title TEXT, updated_at TEXT)")
    hdb.execute("CREATE TABLE session_recaps (session_id TEXT, cwd TEXT, recap TEXT, created_at TEXT)")
    hdb.execute("INSERT INTO session_titles VALUES (?, ?, ?)", ("omp12345678", "Fix auth flow in demo-app", "2026-11-19T01:05:00Z"))
    hdb.execute("INSERT INTO session_recaps VALUES (?, ?, ?, ?)", ("omp12345678", str(proj_dir), "Resolved CORS issue cleanly", "2026-11-19T01:05:00Z"))
    hdb.commit()
    hdb.close()

    # Antigravity conversation_summaries.db + transcript
    agy_dir.mkdir(parents=True, exist_ok=True)
    adb = sqlite3.connect(agy_dir / "conversation_summaries.db")
    adb.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, source TEXT, "
        "project_id TEXT, agent_name TEXT, raw_summary TEXT)"
    )
    adb.execute(
        "CREATE TABLE conversations_fts (conversation_id TEXT, title TEXT, workspace TEXT, project TEXT, user_body TEXT, agent_body TEXT)"
    )
    fake_ghp = "ghp_" + ("B" * 24)
    adb.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "agy-conv-99998888",
            "Build MCP bridge",
            "Preview text",
            12,
            "2026-11-19T02:00:00Z",
            json.dumps([proj_dir.as_uri()]),
            "DONE",
            "ide",
            "proj1",
            "antigravity",
            f"Built stdio MCP server at {proj_dir} with key {fake_ghp}",
        ),
    )
    adb.execute(
        "INSERT INTO conversations_fts VALUES (?,?,?,?,?,?)",
        ("agy-conv-99998888", "Build MCP bridge", str(proj_dir), "demo-app", "Please make it MCP", "Added stdio tools"),
    )
    adb.commit()
    adb.close()

    brain_conv = agy_dir / "brain" / "agy-conv-99998888"
    brain_conv.mkdir(parents=True, exist_ok=True)
    agy_img = brain_conv / "shot.png"
    agy_img.write_bytes(TINY_PNG)
    tdir = brain_conv / ".system_generated" / "logs"
    tdir.mkdir(parents=True, exist_ok=True)
    tlines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-19T02:00:00Z",
            "content": "<USER_REQUEST>\nMake it two-way MCP\n</USER_REQUEST>",
            "media": [{"mime_type": "image/png", "uri": agy_img.as_posix()}],
        },
        {
            "step_index": 1,
            "type": "CHECKPOINT",
            "created_at": "2026-11-19T02:00:30Z",
            "content": f"Checkpoint summary with key {fake_ghp}",
        },
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-19T02:01:00Z",
            "content": f"Implemented 7 tools using {home / 'secret.txt'}",
        },
    ]
    (tdir / "transcript_full.jsonl").write_text("\n".join(json.dumps(x) for x in tlines) + "\n", encoding="utf-8")

    return omp_dir, agy_dir, handoff_dir


class McpUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "fakehome"
        self.home.mkdir()
        self.omp_dir, self.agy_dir, self.handoff_dir = _make_fixtures(root, self.home)
        self.env_patch = {
            "USERPROFILE": str(self.home),
            "CTXBRIDGE_OMP_DIR": str(self.omp_dir),
            "CTXBRIDGE_AGY_DIR": str(self.agy_dir),
            "CTXBRIDGE_HANDOFF_DIR": str(self.handoff_dir),
        }
        self.old_env = {k: os.environ.get(k) for k in self.env_patch}
        os.environ.update(self.env_patch)
        ctxbridge.set_agy_override(None)

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_lifecycle_and_unknown_method(self):
        init_resp = ctxbridge_mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "context-bridge-mcp")
        self.assertIsNone(ctxbridge_mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        ping_resp = ctxbridge_mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(ping_resp["result"], {})
        tools_resp = ctxbridge_mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = [t["name"] for t in tools_resp["result"]["tools"]]
        self.assertEqual(
            names,
            ["list_sessions", "get_context_pack", "search_sessions", "save_handoff", "get_handoff", "import_omp_to_agy", "import_agy_to_omp"],
        )
        unk = ctxbridge_mcp.handle_request({"jsonrpc": "2.0", "id": 4, "method": "unknown/foo"})
        self.assertEqual(unk["error"]["code"], -32601)

    def test_list_sessions_and_path_scrubbing(self):
        resp = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {"source": "all", "limit": 5}}}
        )
        self.assertFalse(resp["result"]["isError"])
        data = json.loads(resp["result"]["content"][0]["text"])
        sources = {s["source"] for s in data["sessions"]}
        self.assertEqual(sources, {"omp", "agy"})
        for s in data["sessions"]:
            self.assertNotIn(str(self.home), s["project"])
            self.assertIn("~", s["project"])

    def test_get_context_pack_redacts_and_handles_missing_id(self):
        omp_pack = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "get_context_pack", "arguments": {"source": "omp", "id": "latest", "budget_tokens": 1000}}}
        )
        self.assertFalse(omp_pack["result"]["isError"])
        text = omp_pack["result"]["content"][0]["text"]
        self.assertIn("source: \"omp\"", text)
        self.assertIn("[REDACTED:", text)
        self.assertNotIn("sk-" + ("A" * 20), text)
        self.assertNotIn(str(self.home), text)

        agy_pack = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "get_context_pack", "arguments": {"source": "agy", "id": "latest"}}}
        )
        self.assertFalse(agy_pack["result"]["isError"])
        atext = agy_pack["result"]["content"][0]["text"]
        self.assertIn("[REDACTED:github_token]", atext)
        self.assertNotIn("ghp_" + ("B" * 24), atext)

        bad = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "get_context_pack", "arguments": {"source": "omp", "id": "nonexistent123"}}}
        )
        self.assertTrue(bad["result"]["isError"])

    def test_search_sessions_across_both_harnesses(self):
        resp = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "search_sessions", "arguments": {"query": "CORS", "source": "all"}}}
        )
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["source"], "omp")

        resp2 = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "search_sessions", "arguments": {"query": "MCP", "source": "agy"}}}
        )
        data2 = json.loads(resp2["result"]["content"][0]["text"])
        self.assertEqual(len(data2["matches"]), 1)
        self.assertEqual(data2["matches"][0]["id"], "agy-conv-99998888")

    def test_save_and_get_handoff_isolated_and_redacted(self):
        fake_ai = "AIza" + ("C" * 32)
        save_resp = ctxbridge_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "save_handoff",
                    "arguments": {
                        "from_tool": "omp",
                        "project": str(self.home / "projects" / "demo-app"),
                        "summary": f"Finished API with {fake_ai}",
                        "next_steps": "Run integration tests",
                    },
                },
            }
        )
        self.assertFalse(save_resp["result"]["isError"])
        saved = json.loads(save_resp["result"]["content"][0]["text"])
        self.assertEqual(saved["status"], "saved")
        self.assertFalse((self.home / "projects" / "demo-app" / "AGENTS.md").exists())
        self.assertFalse((self.home / ".gemini" / "GEMINI.md").exists())

        get_resp = ctxbridge_mcp.handle_request(
            {"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "get_handoff", "arguments": {"project": "demo-app", "limit": 3}}}
        )
        got = json.loads(get_resp["result"]["content"][0]["text"])
        self.assertEqual(len(got["handoffs"]), 1)
        item = got["handoffs"][0]
        self.assertIn("[REDACTED:google_key]", item["summary"])
        self.assertNotIn(fake_ai, item["summary"])
        self.assertNotIn(str(self.home), item["project"])

    def test_import_omp_to_agy_creates_valid_db_and_summary(self):
        resp = ctxbridge_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "import_omp_to_agy",
                    "arguments": {"id": "latest", "max_messages": 0, "apply": True},
                },
            }
        )
        self.assertFalse(resp["result"]["isError"])
        res = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(res["status"], "imported")
        self.assertTrue(res["summary_db_updated"])
        self.assertEqual(res["checkpoints"], 1)
        self.assertEqual(res["user_turns"], 2)
        self.assertGreaterEqual(res["images_copied"], 2)
        cid = res["conversation_id"]
        conv_db = self.agy_dir / "conversations" / f"{cid}.db"
        self.assertTrue(conv_db.is_file())
        con = sqlite3.connect(str(conv_db))
        u_ver = con.execute("PRAGMA user_version").fetchone()[0]
        tmb = con.execute("SELECT id, data FROM trajectory_metadata_blob").fetchall()
        steps = con.execute("SELECT idx, step_type, step_payload FROM steps ORDER BY idx").fetchall()
        con.close()
        self.assertEqual(u_ver, 1)
        self.assertEqual(len(tmb), 1)
        self.assertEqual(tmb[0][0], "main")
        self.assertGreater(len(tmb[0][1]), 10)
        # Timeline: pre-user(14), pre-asst(15), compaction CHECKPOINT(23), post-user(14), merged post-asst(15)
        self.assertEqual([s[1] for s in steps], [14, 15, 23, 14, 15])
        self.assertIn(b"omp_media", steps[0][2])
        self.assertNotIn(b"sk-" + (b"A" * 20), steps[2][2])
        self.assertIn(b"[REDACTED:", steps[2][2])
        self.assertIn(b"Inspecting login.py...", steps[4][2])
        self.assertIn(b"Fixed CORS bug in login.py", steps[4][2])
        self.assertIn(b"![OMP image", steps[4][2])

        media_files = list((self.agy_dir / "brain" / cid / "omp_media").glob("*.png"))
        self.assertGreaterEqual(len(media_files), 2)

        tpath = self.agy_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        t_records = [json.loads(x) for x in tpath.read_text(encoding="utf-8").splitlines() if x.strip()]
        t_types = [r["type"] for r in t_records]
        self.assertEqual(t_types, ["USER_INPUT", "PLANNER_RESPONSE", "CHECKPOINT", "USER_INPUT", "PLANNER_RESPONSE"])
        self.assertTrue(t_records[0].get("media"))

        annot = self.agy_dir / "annotations" / f"{cid}.pbtxt"
        self.assertTrue(annot.is_file())
        self.assertTrue(annot.read_text(encoding="utf-8").startswith("title:"))

        scon = sqlite3.connect(str(self.agy_dir / "conversation_summaries.db"))
        row = scon.execute("SELECT title, step_count, raw_summary FROM conversation_summaries WHERE conversation_id=?", (cid,)).fetchone()
        scon.close()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].startswith("[OMP]"))
        self.assertEqual(row[1], len(steps))
        self.assertIsInstance(row[2], bytes)
        self.assertGreater(len(row[2]), 30)

        batch_resp = ctxbridge_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "import_omp_to_agy",
                    "arguments": {"id": "all", "limit": 5, "apply": True},
                },
            }
        )
        self.assertFalse(batch_resp["result"]["isError"])
        bres = json.loads(batch_resp["result"]["content"][0]["text"])
        self.assertEqual(bres["imported_count"], 1)

    def test_import_agy_to_omp_creates_valid_jsonl_blob_and_history(self):
        resp = ctxbridge_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 24,
                "method": "tools/call",
                "params": {
                    "name": "import_agy_to_omp",
                    "arguments": {"id": "latest", "apply": True},
                },
            }
        )
        self.assertFalse(resp["result"]["isError"])
        res = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(res["status"], "imported")
        self.assertTrue(res["history_db_updated"])
        self.assertEqual(res["images_copied"], 1)
        omp_sid = res["omp_session_id"]

        jsonl_files = list((self.omp_dir / "sessions").rglob(f"*_{omp_sid}.jsonl"))
        self.assertEqual(len(jsonl_files), 1)
        entries = [json.loads(x) for x in jsonl_files[0].read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(entries[0]["type"], "title")
        self.assertTrue(entries[0]["title"].startswith("[AGY]"))
        self.assertEqual(entries[1]["type"], "session")
        self.assertEqual(entries[2]["type"], "message")
        self.assertEqual(entries[2]["message"]["role"], "user")
        img_blocks = [b for b in entries[2]["message"]["content"] if b.get("type") == "image"]
        self.assertEqual(len(img_blocks), 1)
        self.assertTrue(img_blocks[0]["data"].startswith("blob:sha256:"))
        digest = img_blocks[0]["data"].split(":")[-1]
        self.assertTrue((self.omp_dir / "blobs" / digest).is_file())
        self.assertEqual(entries[3]["type"], "compaction")
        self.assertIn("[REDACTED:github_token]", entries[3]["summary"])
        self.assertNotIn("ghp_" + ("B" * 24), entries[3]["summary"])
        self.assertEqual(entries[4]["type"], "message")
        self.assertEqual(entries[4]["message"]["role"], "assistant")

        hcon = sqlite3.connect(str(self.omp_dir / "history.db"))
        trow = hcon.execute("SELECT title FROM session_titles WHERE session_id=?", (omp_sid,)).fetchone()
        hcon.close()
        self.assertIsNotNone(trow)
        self.assertTrue(trow[0].startswith("[AGY]"))

        # Verify ping-pong prevention: batch import_omp_to_agy skips [AGY] sessions
        batch_back = ctxbridge_mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 25,
                "method": "tools/call",
                "params": {"name": "import_omp_to_agy", "arguments": {"id": "all", "limit": 10, "apply": True}},
            }
        )
        bres = json.loads(batch_back["result"]["content"][0]["text"])
        self.assertEqual(bres["imported_count"], 1)


class McpStdioSubprocessTests(unittest.TestCase):
    def test_real_stdio_process_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            omp_dir, agy_dir, handoff_dir = _make_fixtures(root, home)
            env = os.environ.copy()
            env.update(
                {
                    "USERPROFILE": str(home),
                    "CTXBRIDGE_OMP_DIR": str(omp_dir),
                    "CTXBRIDGE_AGY_DIR": str(agy_dir),
                    "CTXBRIDGE_HANDOFF_DIR": str(handoff_dir),
                }
            )
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {"source": "all", "limit": 5}}},
            ]
            stdin_data = "\n".join(json.dumps(m) for m in msgs) + "\n"
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "ctxbridge_mcp.py")],
                input=stdin_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=15,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["id"], 1)
            self.assertEqual(len(lines[1]["result"]["tools"]), 7)
            sessions = json.loads(lines[2]["result"]["content"][0]["text"])["sessions"]
            self.assertEqual(len(sessions), 2)


if __name__ == "__main__":
    unittest.main()
