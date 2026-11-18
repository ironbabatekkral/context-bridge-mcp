import json
import contextlib
import io
import os
import sqlite3
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import ctxbridge as cb
import ctxbridge_gui as gui


class RedactTests(unittest.TestCase):
    def test_secrets_are_redacted(self):
        cases = {
            "sk-abcdefghijklmnopqrstu": "[REDACTED:openai_key]",
            "AIzaSyAbcdefghijklmnopqrstuvwxyz1234567": "[REDACTED:google_key]",
            "ghp_abcdefghijklmnopqrstuvwxyz0123": "[REDACTED:github_token]",
            "github_pat_11ABCDEFGHIJKLMNOPQRST": "[REDACTED:github_pat]",
            "Authorization: Bearer abc.def-ghi_1234567890": "[REDACTED:bearer]",
        }
        for raw, marker in cases.items():
            with self.subTest(raw=raw):
                out = cb.redact(raw)
                self.assertIn(marker, out)
                self.assertNotIn(raw.split()[-1], out)

    def test_pem_block_is_redacted(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj\n-----END RSA PRIVATE KEY-----"
        out = cb.redact("key:\n" + pem + "\nend")
        self.assertIn("[REDACTED:pem]", out)
        self.assertNotIn("MIIBOgIBAAJBAKj", out)

    def test_assignments_keep_key_name(self):
        out = cb.redact("password=hunter2 and token: abc123 and api_key=xyz")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abc123", out)
        self.assertNotIn("xyz", out)
        self.assertIn("password=[REDACTED:assignment]", out)

    def test_normal_text_unchanged(self):
        samples = [
            "max_tokens=5 in config",
            "tokenizer: bpe",
            "skill sk tools",
            "AIza is a prefix only",
            "Bearer of bad news",
            "Merhaba dünya, ilk adım.",
        ]
        for s in samples:
            with self.subTest(s=s):
                self.assertEqual(cb.redact(s), s)


class TrimTests(unittest.TestCase):
    META = {"ctxbridge": "1", "source": "omp", "id": "abcdef12", "title": "t", "project": "p",
            "updated": "2026-01-01T00:00:00", "budget_tokens": 0}

    def test_estimate(self):
        self.assertEqual(cb.est_tokens(""), 0)
        self.assertEqual(cb.est_tokens("abcd"), 1)
        self.assertEqual(cb.est_tokens("abcde"), 2)

    def test_truncate_respects_cap(self):
        text = "x" * 1000
        out = cb.truncate_tokens(text, 50)
        self.assertLessEqual(cb.est_tokens(out), 50)
        self.assertTrue(out.endswith(cb.TRUNC_MARK))
        self.assertEqual(cb.truncate_tokens("short", 50), "short")

    def test_pack_within_budget_and_keeps_newest(self):
        msgs = [("user" if i % 2 == 0 else "assistant", f"mesaj numarası {i} " * 20) for i in range(60)]
        budget = 400
        pack = cb.render_pack(self.META, "önceki özet " * 100, msgs, budget)
        self.assertLessEqual(cb.est_tokens(pack), budget + 5)
        self.assertIn("mesaj numarası 59 ", pack)
        self.assertNotIn("mesaj numarası 0 ", pack)

    def test_chronological_order_preserved(self):
        msgs = [("user", "birinci"), ("assistant", "ikinci"), ("user", "üçüncü")]
        pack = cb.render_pack(self.META, None, msgs, 4000)
        self.assertLess(pack.index("birinci"), pack.index("ikinci"))
        self.assertLess(pack.index("ikinci"), pack.index("üçüncü"))

    def test_summary_capped_to_share(self):
        budget = 500
        pack = cb.render_pack(self.META, "s" * 10000, [], budget)
        summary_section = pack.split("## Özet\n", 1)[1].split("\n\n## Son konuşma", 1)[0]
        self.assertLessEqual(cb.est_tokens(summary_section), int(budget * cb.SUMMARY_SHARE) + 1)
        self.assertIn(cb.TRUNC_MARK, summary_section)

    def test_oversized_newest_message_is_truncated(self):
        pack = cb.render_pack(self.META, None, [("user", "y" * 50000)], 300)
        self.assertIn(cb.TRUNC_MARK, pack)
        self.assertLessEqual(cb.est_tokens(pack), 300 + 5)

    def test_front_matter_roundtrip(self):
        pack = cb.render_pack(self.META, "özet", [("user", "merhaba")], 1000)
        meta, body = cb.split_front(pack)
        self.assertEqual(meta["ctxbridge"], "1")
        self.assertEqual(meta["id"], "abcdef12")
        self.assertIn("## Özet", body)


class PathScrubTests(unittest.TestCase):
    def test_home_is_masked_in_all_forms(self):
        home = Path.home()
        self.assertEqual(cb.scrub_paths(str(home) + "\\proj\\a.md"), "~\\proj\\a.md")
        self.assertEqual(cb.scrub_paths(home.as_posix() + "/proj/a.md"), "~/proj/a.md")
        self.assertEqual(cb.scrub_paths("file:///" + home.as_posix() + "/proj"), "~/proj")
        self.assertEqual(cb.scrub_paths(home.as_posix().upper() + "/proj"), "~/proj")

    def test_other_users_masked(self):
        self.assertEqual(cb.scrub_paths("C:\\Users\\someone\\x"), "<home>\\x")
        self.assertEqual(cb.scrub_paths("/Users/someone/x"), "<home>/x")
        self.assertEqual(cb.scrub_paths("/home/someone/x"), "<home>/x")

    def test_unrelated_text_unchanged(self):
        for s in ["Users of the app", "C:\\Program Files\\x", "/usr/bin/python", "C:\\Users", "merhaba dünya"]:
            self.assertEqual(cb.scrub_paths(s), s)


class PackFormatTests(unittest.TestCase):
    def test_empty_summary_omits_section(self):
        pack = cb.render_pack(TrimTests.META, None, [("user", "merhaba")], 1000)
        self.assertNotIn("## Özet", pack)
        self.assertNotIn("(özet yok)", pack)
        self.assertIn("## Son konuşma", pack)


class AgentsBlockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self.out = self.root / "out"
        patcher = mock.patch.object(cb, "OUT_DIR", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agents = self.proj / "AGENTS.md"

    def pack(self, msg):
        path = self.root / "pack.md"
        path.write_text(cb.render_pack(TrimTests.META, None, [("user", msg)], 1000), encoding="utf-8", newline="\n")
        return str(path)

    def plan(self, msg, source="agy"):
        return cb.plan_inject(source, self.pack(msg), str(self.proj), "project")

    def test_create_when_absent(self):
        plan = self.plan("ilk")
        self.assertEqual(plan.mode, "create")
        cb.apply_inject(plan)
        text = self.agents.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<!-- ctxbridge:begin id=abcdef12 -->\n"))
        self.assertTrue(text.endswith("<!-- ctxbridge:end -->\n"))

    def test_append_keeps_existing_bytes(self):
        prior = "# Kurallar\r\nBir\r\n".encode("utf-8")
        self.agents.write_bytes(prior)
        plan = self.plan("ilk")
        self.assertEqual(plan.mode, "append")
        cb.apply_inject(plan)
        self.assertTrue(self.agents.read_bytes().startswith(prior))
        backups = list(self.out.glob("AGENTS.md.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), prior)

    def test_reapply_replaces_block_only(self):
        self.agents.write_bytes(b"# Kurallar\n")
        cb.apply_inject(self.plan("birinci"))
        plan = self.plan("ikinci")
        self.assertEqual(plan.mode, "replace")
        cb.apply_inject(plan)
        text = self.agents.read_text(encoding="utf-8")
        self.assertEqual(text.count("ctxbridge:begin id=abcdef12"), 1)
        self.assertIn("ikinci", text)
        self.assertNotIn("birinci", text)
        self.assertTrue(text.startswith("# Kurallar\n"))
        self.assertTrue(text.endswith("<!-- ctxbridge:end -->\n"))

    def test_same_pack_is_unchanged(self):
        cb.apply_inject(self.plan("ilk"))
        before = self.agents.read_bytes()
        plan = self.plan("ilk")
        self.assertEqual(plan.mode, "unchanged")
        cb.apply_inject(plan)
        self.assertEqual(self.agents.read_bytes(), before)

    def test_other_blocks_untouched(self):
        other = "<!-- ctxbridge:begin id=zzzzzzzz -->\nESKI\n<!-- ctxbridge:end -->\n"
        self.agents.write_text(other, encoding="utf-8", newline="")
        cb.apply_inject(self.plan("ilk"))
        self.assertIn(other, self.agents.read_text(encoding="utf-8"))

    def test_malformed_block_refused(self):
        self.agents.write_text("<!-- ctxbridge:begin id=abcdef12 -->\nyarım\n", encoding="utf-8", newline="")
        with self.assertRaises(cb.CtxError):
            self.plan("ilk")

    def test_changed_since_plan_refused(self):
        self.agents.write_bytes(b"# A\n")
        plan = self.plan("ilk")
        self.agents.write_bytes(b"# B\n")
        with self.assertRaises(cb.TargetExists):
            cb.apply_inject(plan)
        self.assertEqual(self.agents.read_bytes(), b"# B\n")

    def test_omp_never_overwrites(self):
        omp = self.proj / ".omp" / "AGENTS.md"
        omp.parent.mkdir()
        omp.write_text("elle", encoding="utf-8")
        plan = self.plan("ilk", source="omp")
        self.assertEqual(plan.mode, "fallback")
        cb.apply_inject(plan)
        self.assertEqual(omp.read_text(encoding="utf-8"), "elle")
        self.assertTrue((self.out / "omp-abcdef12.md").is_file())


class FakeHomeTests(unittest.TestCase):
    FAKE = "C:\\Users\\alice"

    def setUp(self):
        env = mock.patch.dict(os.environ, {"USERPROFILE": self.FAKE})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CTXBRIDGE_OMP_DIR", None)
        os.environ.pop("CTXBRIDGE_AGY_DIR", None)

    def test_userprofile_drives_roots(self):
        self.assertEqual(cb.user_home(), Path(self.FAKE))
        self.assertEqual(cb.omp_sessions_dir(), Path(self.FAKE) / ".omp" / "agent" / "sessions")
        self.assertEqual(cb.agy_summaries_path(), Path(self.FAKE) / ".gemini" / "antigravity" / "conversation_summaries.db")

    def test_scrub_follows_userprofile(self):
        self.assertEqual(cb.scrub_paths(self.FAKE + "\\proj\\a.md"), "~\\proj\\a.md")
        self.assertEqual(cb.scrub_paths("C:\\Users\\bob\\x"), "<home>\\x")

    def test_env_overrides_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CTXBRIDGE_OMP_DIR": tmp}):
                self.assertEqual(cb.omp_sessions_dir(), Path(tmp) / "sessions")


class UriTests(unittest.TestCase):
    def test_spaces_decoded(self):
        self.assertEqual(cb.file_uri_to_path("file:///C:/Users/alice/My%20Proj"), Path("C:/Users/alice/My Proj"))

    def test_encoded_drive_colon(self):
        self.assertEqual(cb.file_uri_to_path("file:///c%3A/x"), Path("c:/x"))

    def test_non_file_scheme_rejected(self):
        self.assertIsNone(cb.file_uri_to_path("https://example.com/y"))

    def test_workspace_parsing(self):
        self.assertEqual(cb.parse_workspace_uris('["file:///C:/a"]'), ["file:///C:/a"])
        self.assertEqual(cb.parse_workspace_uris("file:///C:/a"), ["file:///C:/a"])
        self.assertEqual(cb.parse_workspace_uris(None), [])
        self.assertEqual(cb.parse_workspace_uris(""), [])


class ProjectChoiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_temp = (self.root / "fake-temp").resolve()
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.sub = self.repo / "a" / "b"
        self.sub.mkdir(parents=True)
        self.det = self.root / "detected"
        self.det.mkdir()
        self.packp = self.root / "packp"
        self.packp.mkdir()
        patchers = [mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)}),
                    mock.patch.object(cb, "temp_roots", lambda: [self.fake_temp])]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_git_root_walk_up(self):
        self.assertEqual(cb.find_git_root(self.sub), self.repo)
        choice, _ = cb.pick_project([("omp cwd", self.sub)], "auto")
        self.assertEqual((choice.source, choice.dir, choice.root), ("omp cwd", self.sub.resolve(), self.repo.resolve()))

    def test_first_valid_candidate_wins(self):
        choice, _ = cb.pick_project([("fts", self.det), ("pack", self.packp)], "auto")
        self.assertEqual((choice.source, choice.dir), ("fts", self.det.resolve()))

    def test_missing_candidate_skipped(self):
        choice, _ = cb.pick_project([("fts", self.root / "nope"), ("pack", self.packp)], "auto")
        self.assertEqual((choice.source, choice.dir), ("pack", self.packp.resolve()))

    def test_all_missing_gives_none_with_reason(self):
        choice, why = cb.pick_project([("fts", self.root / "nope")], "auto")
        self.assertIsNone(choice)
        self.assertIn("dizin yok", why)

    def test_temp_and_home_skipped_in_auto(self):
        temp = self.fake_temp / "proj"
        temp.mkdir(parents=True)
        choice, why = cb.pick_project([("fts", temp)], "auto")
        self.assertIsNone(choice)
        self.assertIn("geçici klasör", why)
        choice, why = cb.pick_project([("pack", self.home)], "auto")
        self.assertIsNone(choice)
        self.assertIn("ev dizini", why)

    def test_home_ancestor_skipped_in_auto(self):
        choice, why = cb.pick_project([("izler(metin)", self.home.parent)], "auto")
        self.assertIsNone(choice)
        self.assertIn("üst dizin", why)

    def test_project_scope_allows_temp(self):
        temp = self.fake_temp / "proj"
        temp.mkdir(parents=True)
        choice, _ = cb.pick_project([("elle", temp)], "project")
        self.assertEqual(choice.dir, temp.resolve())

    def test_project_scope_rejects_missing(self):
        choice, _ = cb.pick_project([("elle", self.root / "nope")], "project")
        self.assertIsNone(choice)

    def test_pack_file_uri_decoded(self):
        cands = cb.project_candidates({"project": self.packp.as_uri(), "source": "x", "id": "y"}, None)
        self.assertEqual(cands, [("pack", self.packp.resolve())])

    def test_explicit_project_replaces_pack(self):
        cands = cb.project_candidates({"project": str(self.packp), "source": "x", "id": "y"}, str(self.det))
        self.assertEqual(cands, [("elle", self.det)])

    def test_other_tool(self):
        self.assertEqual(cb.other_tool("omp"), "agy")
        self.assertEqual(cb.other_tool("agy"), "omp")


def make_omp_session(omp_dir: Path, sid: str, cwd: Path) -> None:
    sdir = omp_dir / "sessions" / "proj"
    sdir.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "id": sid, "cwd": str(cwd)}
    (sdir / f"2026-01-01_{sid}.jsonl").write_text(json.dumps(header) + "\n", encoding="utf-8")


def make_agy_db(path: Path, cid: str, workspace: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, last_modified_time INTEGER, "
        "step_count INTEGER, workspace_uris TEXT, raw_summary TEXT)"
    )
    con.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?)", (cid, "t", 1, 3, workspace, None))
    con.commit()
    con.close()


class DetectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.omp_dir = self.root / "omp"
        self.agy_dir = self.root / "agy"
        self.work = self.root / "work"
        self.work.mkdir()
        env = {"CTXBRIDGE_OMP_DIR": str(self.omp_dir), "CTXBRIDGE_AGY_DIR": str(self.agy_dir), "USERPROFILE": str(self.home)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        fake_temp = (self.root / "fake-temp").resolve()
        tpatch = mock.patch.object(cb, "temp_roots", lambda: [fake_temp])
        tpatch.start()
        self.addCleanup(tpatch.stop)

    def transcript(self, cid: str, records: list) -> None:
        logs = self.agy_dir / "brain" / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    def db(self) -> Path:
        return self.agy_dir / "conversation_summaries.db"

    def test_omp_session_cwd(self):
        make_omp_session(self.omp_dir, "abcdef1234567890", Path("C:/Users/alice/proj"))
        d = cb.detect_project("omp", "abcdef1234567890")
        self.assertEqual((d.dir, d.via), (Path("C:/Users/alice/proj"), "omp cwd"))

    def test_unknown_session_is_empty(self):
        d = cb.detect_project("omp", "ffffffffffff")
        self.assertEqual((d.dir, d.via), (None, ""))

    def test_agy_workspace_uri(self):
        proj = self.work / "My Proj"
        proj.mkdir()
        make_agy_db(self.db(), "agy-conv-0001", json.dumps([proj.as_uri()]))
        d = cb.detect_project("agy", "agy-conv-0001")
        self.assertEqual((d.dir, d.via), (proj.resolve(), "workspace_uris"))

    def test_agy_fts_fallback(self):
        proj = self.work / "fts-proj"
        proj.mkdir()
        make_agy_db(self.db(), "agy-conv-0002", "[]")
        con = sqlite3.connect(self.db())
        con.execute("CREATE TABLE conversations_fts (conversation_id TEXT, workspace TEXT, project TEXT)")
        con.execute("INSERT INTO conversations_fts VALUES (?,?,?)", ("agy-conv-0002", proj.as_uri(), ""))
        con.commit()
        con.close()
        d = cb.detect_project("agy", "agy-conv-0002")
        self.assertEqual((d.dir, d.via), (proj.resolve(), "fts"))

    def test_arg_vote_skips_home_and_relative(self):
        proj = self.work / "argproj"
        proj.mkdir()
        make_agy_db(self.db(), "agy-conv-0003", "[]")
        self.transcript("agy-conv-0003", [
            {"tool_calls": [{"args": {"Cwd": str(self.home)}}]},
            {"tool_calls": [{"args": {"Cwd": str(proj)}}]},
            {"tool_calls": [{"args": {"Path": str(proj / "a.py")}}]},
            {"tool_calls": [{"arguments": json.dumps({"Cwd": "relative/dir"})}]},
        ])
        d = cb.detect_project("agy", "agy-conv-0003")
        self.assertEqual((d.dir, d.via), (proj.resolve(), "izler(argüman)"))

    def test_text_vote_from_transcript(self):
        proj = self.work / "textproj"
        proj.mkdir()
        make_agy_db(self.db(), "agy-conv-0004", "[]")
        self.transcript("agy-conv-0004", [{"role": "agent", "content": f"Dosya: {proj.as_uri()}/main.py okundu"}])
        d = cb.detect_project("agy", "agy-conv-0004")
        self.assertEqual((d.dir, d.via), (proj.resolve(), "izler(metin)"))

    def test_only_home_evidence_is_empty(self):
        make_agy_db(self.db(), "agy-conv-0005", "[]")
        self.transcript("agy-conv-0005", [
            {"tool_calls": [{"args": {"Cwd": str(self.home)}}]},
            {"role": "agent", "content": f"{self.home}\\notes.txt"},
        ])
        d = cb.detect_project("agy", "agy-conv-0005")
        self.assertEqual((d.dir, d.via), (None, ""))

    def test_marker_walk_up_stops_at_project_root(self):
        repo = self.work / "repo"
        (repo / ".git").mkdir(parents=True)
        deep = repo / "src" / "pkg"
        deep.mkdir(parents=True)
        make_agy_db(self.db(), "agy-conv-0006", "[]")
        self.transcript("agy-conv-0006", [{"tool_calls": [{"args": {"Cwd": str(deep)}}]}])
        d = cb.detect_project("agy", "agy-conv-0006")
        self.assertEqual((d.dir, d.via), (repo.resolve(), "izler(argüman)"))

    def test_pack_inject_without_project_uses_detection(self):
        proj = self.work / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        make_omp_session(self.omp_dir, "abcdef1234567890", proj)
        meta = {"ctxbridge": "1", "source": "omp", "id": "abcdef1234567890", "title": "t", "project": "",
                "updated": "2026-01-01T00:00:00", "budget_tokens": 1000}
        pack = self.root / "pack.md"
        pack.write_text(cb.render_pack(meta, None, [("user", "merhaba")], 1000), encoding="utf-8", newline="\n")
        plan = cb.plan_inject("agy", str(pack), None, "auto")
        self.assertEqual((plan.scope, plan.target), ("project", proj.resolve() / "AGENTS.md"))



class AgyRootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.gem = self.home / ".gemini"
        for name in ("antigravity", "antigravity-old", "antigravity-ide"):
            (self.gem / name).mkdir(parents=True)
        self.new_root = self.gem / "antigravity"
        self.old_root = self.gem / "antigravity-old"
        for root in (self.new_root, self.old_root):
            (root / "conversation_summaries.db").write_bytes(b"")
        os.utime(self.old_root / "conversation_summaries.db", (1_000_000, 1_000_000))
        os.utime(self.new_root / "conversation_summaries.db", (2_000_000, 2_000_000))
        patcher = mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CTXBRIDGE_AGY_DIR", None)
        cb.set_agy_override(None)
        self.addCleanup(cb.set_agy_override, None)

    def test_candidates_need_summaries_db(self):
        self.assertEqual([p.name for p in cb.agy_root_candidates()], ["antigravity", "antigravity-old"])

    def test_newest_db_auto_picked(self):
        self.assertEqual(cb.agy_root_info(), (self.new_root, "otomatik"))

    def test_newer_older_root_wins_after_touch(self):
        os.utime(self.old_root / "conversation_summaries.db", (3_000_000, 3_000_000))
        self.assertEqual(cb.agy_root_info(), (self.old_root, "otomatik"))

    def test_manual_override(self):
        cb.set_agy_override(self.old_root)
        self.assertEqual(cb.agy_root_info(), (self.old_root, "elle"))

    def test_env_beats_override(self):
        os.environ["CTXBRIDGE_AGY_DIR"] = str(self.old_root)
        cb.set_agy_override(self.new_root)
        self.assertEqual(cb.agy_root_info(), (self.old_root, "CTXBRIDGE_AGY_DIR"))


class ManualOverrideTests(unittest.TestCase):
    def test_manual_pick_survives_until_new_row(self):
        s = gui.AutoSelect()
        self.assertTrue(s.fill_allowed())
        s.on_pick()
        self.assertFalse(s.fill_allowed())
        s.on_row()
        self.assertTrue(s.fill_allowed())


class ScopeTests(unittest.TestCase):
    """inject kapsamı auto/project/global; global yazım yalnızca test klasöründe."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_temp = self.root / "fake-temp"
        self.proj = self.root / "work" / "proj"
        (self.proj / ".git").mkdir(parents=True)
        self.out = self.root / "out"
        self.gem = self.home / ".gemini"
        self.global_file = self.gem / "GEMINI.md"
        env = {"USERPROFILE": str(self.home), "CTXBRIDGE_OMP_DIR": str(self.root / "omp"),
               "CTXBRIDGE_AGY_DIR": str(self.gem / "antigravity")}
        patchers = [mock.patch.dict(os.environ, env),
                    mock.patch.object(cb, "OUT_DIR", self.out),
                    mock.patch.object(cb, "temp_roots", lambda: [self.fake_temp])]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("CTXBRIDGE_AGY_GLOBAL_DIR", None)

    def pack(self, project: str, source: str = "agy", body: str = "merhaba") -> str:
        meta = {"ctxbridge": "1", "source": source, "id": "abcdef12", "title": "t", "project": project,
                "updated": "2026-01-01T00:00:00", "budget_tokens": 1000}
        path = self.root / f"pack-{source}-{abs(hash(body))}.md"
        path.write_text(cb.render_pack(meta, None, [("user", body)], 1000), encoding="utf-8", newline="\n")
        return str(path)

    def test_temp_project_goes_global(self):
        temp = self.fake_temp / "proj"
        temp.mkdir(parents=True)
        plan = cb.plan_inject("agy", self.pack(str(temp)), None, "auto")
        self.assertEqual((plan.scope, plan.target), ("global", self.global_file))
        self.assertIn("geçici klasör", plan.reason)

    def test_home_project_goes_global(self):
        plan = cb.plan_inject("agy", self.pack(str(self.home)), None, "auto")
        self.assertEqual((plan.scope, plan.target), ("global", self.global_file))
        self.assertIn("ev dizini", plan.reason)

    def test_valid_project_agy_target(self):
        plan = cb.plan_inject("agy", self.pack(str(self.proj)), None, "auto")
        self.assertEqual((plan.scope, plan.target), ("project", self.proj / "AGENTS.md"))

    def test_valid_project_omp_target(self):
        plan = cb.plan_inject("omp", self.pack(str(self.proj), source="omp"), None, "auto")
        self.assertEqual((plan.scope, plan.target), ("project", self.proj / ".omp" / "AGENTS.md"))
        self.assertEqual(plan.mode, "create")

    def test_global_scope_ignores_project(self):
        plan = cb.plan_inject("agy", self.pack(str(self.proj)), str(self.proj), "global")
        self.assertEqual((plan.scope, plan.target), ("global", self.global_file))

    def test_global_omp_target(self):
        plan = cb.plan_inject("omp", self.pack(""), None, "global")
        self.assertEqual(plan.target, self.root / "omp" / "AGENTS.md")

    def test_project_scope_without_candidate_raises(self):
        with self.assertRaises(cb.CtxError):
            cb.plan_inject("agy", self.pack(""), None, "project")

    def test_explicit_project_scope_allows_temp(self):
        temp = self.fake_temp / "proj"
        temp.mkdir(parents=True)
        plan = cb.plan_inject("agy", self.pack(""), str(temp), "project")
        self.assertEqual((plan.scope, plan.target), ("project", temp / "AGENTS.md"))

    def test_invalid_scope_rejected(self):
        with self.assertRaises(cb.CtxError):
            cb.plan_inject("agy", self.pack(str(self.proj)), None, "bogus")

    def test_existing_global_file_keeps_bytes_outside_block(self):
        prior = "# Genel kurallar\r\nÖnceki içerik ğüş\r\n".encode("utf-8")
        self.global_file.parent.mkdir(parents=True)
        self.global_file.write_bytes(prior)
        plan = cb.plan_inject("agy", self.pack(""), None, "global")
        self.assertEqual(plan.mode, "append")
        self.assertIsNotNone(plan.backup)
        cb.apply_inject(plan)
        data = self.global_file.read_bytes()
        self.assertTrue(data.startswith(prior))
        self.assertIn(b"ctxbridge:begin id=abcdef12", data)
        backups = list(self.out.glob("GEMINI.md.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), prior)

    def test_replace_keeps_text_around_block(self):
        head = "# Üst\n"
        self.global_file.parent.mkdir(parents=True)
        self.global_file.write_bytes(head.encode("utf-8"))
        cb.apply_inject(cb.plan_inject("agy", self.pack("", body="birinci"), None, "global"))
        tail = "\n# Alt\n"
        self.global_file.write_bytes(self.global_file.read_bytes() + tail.encode("utf-8"))
        plan = cb.plan_inject("agy", self.pack("", body="ikinci"), None, "global")
        self.assertEqual(plan.mode, "replace")
        cb.apply_inject(plan)
        data = self.global_file.read_text(encoding="utf-8")
        self.assertTrue(data.startswith(head))
        self.assertTrue(data.endswith(tail))
        self.assertIn("ikinci", data)
        self.assertNotIn("birinci", data)

    def test_reapply_is_unchanged_without_new_backup(self):
        cb.apply_inject(cb.plan_inject("agy", self.pack(""), None, "global"))
        once = self.global_file.read_bytes()
        plan = cb.plan_inject("agy", self.pack(""), None, "global")
        self.assertEqual(plan.mode, "unchanged")
        cb.apply_inject(plan)
        self.assertEqual(self.global_file.read_bytes(), once)
        self.assertEqual(list(self.out.glob("GEMINI.md.bak-*")), [])

    def test_create_absent_global_file_without_backup(self):
        plan = cb.plan_inject("agy", self.pack(""), None, "global")
        self.assertEqual(plan.mode, "create")
        self.assertIsNone(plan.backup)
        cb.apply_inject(plan)
        self.assertTrue(self.global_file.is_file())
        self.assertFalse(self.out.exists())

    def test_cli_dry_run_writes_nothing_and_masks_target(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cb.main(["inject", "agy", self.pack("")])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("kapsam: global", out)
        self.assertNotIn(str(self.root), out)
        self.assertFalse(self.global_file.exists())


if __name__ == "__main__":
    unittest.main()
