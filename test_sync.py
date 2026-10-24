"""Tests for sync.py: the pure decision functions, plus whole-repository runs
against two local bare-repository "servers" (file:// URLs) with in-memory
stand-ins for the GitHub and MioGit APIs.

    python3 -m unittest -v test_sync.py
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sync as S

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
ANC = {(A, B), (A, C), (B, C)}  # A <- B <- C ; D unrelated


def is_anc(x, y):
    return x == y or (x, y) in ANC


class PlanTests(unittest.TestCase):
    def test_equal_is_noop(self):
        p = S.plan_refs({"refs/heads/main": A}, {"refs/heads/main": A}, {}, is_anc)
        self.assertTrue(p.empty())
        self.assertEqual(p.conflicts, [])

    def test_fast_forward_both_ways(self):
        p = S.plan_refs({"refs/heads/main": B}, {"refs/heads/main": A}, {}, is_anc)
        self.assertEqual(p.to_github, [("refs/heads/main", B)])
        p = S.plan_refs({"refs/heads/main": A}, {"refs/heads/main": C}, {}, is_anc)
        self.assertEqual(p.to_miogit, [("refs/heads/main", C)])

    def test_diverged_is_conflict_and_not_pushed(self):
        p = S.plan_refs({"refs/heads/main": B}, {"refs/heads/main": D}, {"refs/heads/main": A}, is_anc)
        self.assertTrue(p.empty())
        self.assertIn("refs/heads/main", p.conflict_refs)
        self.assertIn("diverged", p.conflicts[0])

    def test_new_ref_copied(self):
        p = S.plan_refs({"refs/heads/x": A}, {}, {}, is_anc)
        self.assertEqual(p.to_github, [("refs/heads/x", A)])
        p = S.plan_refs({}, {"refs/tags/v1": A}, {}, is_anc)
        self.assertEqual(p.to_miogit, [("refs/tags/v1", A)])

    def test_delete_only_when_unchanged_since_last_sync(self):
        p = S.plan_refs({"refs/heads/x": A}, {}, {"refs/heads/x": A}, is_anc)
        self.assertEqual(p.del_miogit, [("refs/heads/x", A)])
        p = S.plan_refs({}, {"refs/heads/x": A}, {"refs/heads/x": A}, is_anc)
        self.assertEqual(p.del_github, [("refs/heads/x", A)])
        # moved since the last sync: conflict, nothing deleted
        p = S.plan_refs({"refs/heads/x": B}, {}, {"refs/heads/x": A}, is_anc)
        self.assertTrue(p.empty())
        self.assertIn("refs/heads/x", p.conflict_refs)

    def test_default_branch_never_deleted(self):
        p = S.plan_refs({"refs/heads/main": A}, {}, {"refs/heads/main": A}, is_anc, {"refs/heads/main"})
        self.assertTrue(p.empty())
        self.assertIn("refs/heads/main", p.conflict_refs)

    def test_tags_never_move(self):
        p = S.plan_refs({"refs/tags/v1": B}, {"refs/tags/v1": A}, {}, is_anc)
        self.assertTrue(p.empty())
        self.assertIn("refs/tags/v1", p.conflict_refs)

    def test_next_state(self):
        st = S.next_state_refs({"refs/heads/a": A, "refs/heads/b": B}, {"refs/heads/a": A, "refs/heads/b": D}, {"refs/heads/b": A, "refs/heads/gone": C})
        self.assertEqual(st, {"refs/heads/a": A, "refs/heads/b": A})

    def test_visibility(self):
        self.assertIsNone(S.visibility_change(True, True, None))
        self.assertEqual(S.visibility_change(True, False, False), ("github", True))  # MioGit went private
        self.assertEqual(S.visibility_change(False, True, True), ("github", False))  # MioGit went public
        self.assertEqual(S.visibility_change(True, False, True), ("miogit", False))  # GitHub went public
        self.assertEqual(S.visibility_change(False, True, None), ("miogit", True))  # unknown: private wins
        self.assertEqual(S.visibility_change(True, False, None), ("github", True))


# ---- whole-repository runs over file:// ---------------------------------------------------

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_NOSYSTEM": "1"}


def run(*a, cwd=None):
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *a], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    def __init__(self, root: Path, org: str):
        self.root, self.org, self.meta = root, org, {}

    def path(self, name):
        return self.root / self.org / f"{name}.git"

    def repo(self, name):
        for k, v in self.meta.items():
            if k.lower() == name.lower():
                return dict(v)
        return None

    def repos(self):
        return [dict(v) for v in self.meta.values()]

    def create(self, name, private, description):
        self.path(name).parent.mkdir(parents=True, exist_ok=True)
        run("init", "-q", "--bare", str(self.path(name)))
        self.meta[name] = {"name": name, "private": private, "description": description, "topics": [], "created_at": "2099-01-01T00:00:00Z"}
        return dict(self.meta[name])

    def set_private(self, name, private):
        self.meta[name]["private"] = private

    def set_default_branch(self, name, branch):
        self.meta[name]["default_branch"] = branch
        run("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=self.path(name))

    def add_topic(self, name, topic):
        if topic not in self.meta[name]["topics"]:
            self.meta[name]["topics"].append(topic)


class FakeMioGit:
    def __init__(self, root: Path):
        self.root, self.meta, self.states = root, {}, {}

    def path(self, name):
        return self.root / f"{name}.git"

    def repos(self):
        return [{**v, "sync": v.get("sync", True), "state": self.states.get(k)} for k, v in self.meta.items()]

    def create(self, name, private, description):
        run("init", "-q", "--bare", str(self.path(name)))
        self.meta[name] = {"name": name, "private": private, "description": description}

    def set_private(self, name, private):
        self.meta[name]["private"] = private

    def put_state(self, name, state):
        self.states[name] = state


class RepoRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sync-test-"))
        self.cfg = S.Config(org="org", miogit_url=f"file://{self.tmp}/m", github_git=f"file://{self.tmp}/g", topic="miogit-sync", since="2026-01-01T00:00:00Z", aliases={}, exclude={"miogit-sync"})
        (self.tmp / "m").mkdir()
        self.gh = FakeGitHub(self.tmp / "g", "org")
        self.mg = FakeMioGit(self.tmp / "m")
        self.work = self.tmp / "work"
        run("init", "-q", str(self.work))

    def syncer(self, dry=False):
        self.report = S.Report()
        return S.Syncer(self.cfg, self.gh, self.mg, self.tmp / "cache", self.report, dry)

    def commit(self, msg):
        (self.work / "f.txt").write_text(msg)
        run("add", "-A", cwd=self.work)
        run("commit", "-qm", msg, cwd=self.work)
        return run("rev-parse", "HEAD", cwd=self.work)

    def refs(self, path):
        return S.ls_remote(f"file://{path}")[1]

    def test_new_miogit_repo_is_created_on_github_private(self):
        self.mg.create("Proj", True, "d")
        c1 = self.commit("one")
        run("push", "-q", str(self.mg.path("Proj")), "HEAD:refs/heads/main", "HEAD:refs/heads/dev", cwd=self.work)
        S.run_repo(self.syncer(), "Proj", "miogit")
        self.assertEqual(self.report.conflicts, [])
        self.assertTrue(self.gh.meta["Proj"]["private"])
        self.assertIn("miogit-sync", self.gh.meta["Proj"]["topics"])
        self.assertEqual(S.ls_remote(f"file://{self.gh.path('Proj')}")[0], "refs/heads/main")
        self.assertEqual(self.refs(self.gh.path("Proj")), {"refs/heads/main": c1, "refs/heads/dev": c1})
        self.assertEqual(self.mg.states["Proj"]["refs"], {"refs/heads/dev": c1, "refs/heads/main": c1})

        # GitHub moves forward -> comes back to MioGit; nothing forced
        c2 = self.commit("two")
        run("push", "-q", str(self.gh.path("Proj")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(), "Proj", "github")
        self.assertEqual(self.refs(self.mg.path("Proj"))["refs/heads/main"], c2)

        # dev deleted on GitHub while MioGit still has the synced sha -> deleted on MioGit
        run("push", "-q", str(self.gh.path("Proj")), ":refs/heads/dev", cwd=self.work)
        S.run_repo(self.syncer(), "Proj", "github")
        self.assertNotIn("refs/heads/dev", self.refs(self.mg.path("Proj")))
        self.assertEqual(self.report.conflicts, [])

    def test_default_branch_change_on_miogit_follows_to_github(self):
        self.mg.create("D", False, "")
        self.commit("one")
        run("push", "-q", str(self.mg.path("D")), "HEAD:refs/heads/main", "HEAD:refs/heads/dev", cwd=self.work)
        run("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.mg.path("D"))
        S.run_repo(self.syncer(), "D", "miogit")
        self.assertEqual(S.ls_remote(f"file://{self.gh.path('D')}")[0], "refs/heads/main")
        self.assertEqual(self.mg.states["D"]["head"], "refs/heads/main")
        # default changed in MioGit's settings -> GitHub follows
        run("symbolic-ref", "HEAD", "refs/heads/dev", cwd=self.mg.path("D"))
        S.run_repo(self.syncer(), "D", "miogit")
        self.assertEqual(S.ls_remote(f"file://{self.gh.path('D')}")[0], "refs/heads/dev")
        self.assertEqual(self.mg.states["D"]["head"], "refs/heads/dev")
        self.assertEqual(self.report.conflicts, [])
        # default changed only on GitHub -> MioGit and GitHub are left as they are
        run("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.gh.path("D"))
        S.run_repo(self.syncer(), "D", "github")
        self.assertEqual(S.ls_remote(f"file://{self.gh.path('D')}")[0], "refs/heads/main")
        self.assertEqual(S.ls_remote(f"file://{self.mg.path('D')}")[0], "refs/heads/dev")
        self.assertEqual(self.report.conflicts, [])

    def test_divergence_is_reported_and_neither_side_changes(self):
        self.mg.create("P", False, "")
        self.commit("base")
        run("push", "-q", str(self.mg.path("P")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(), "P", "miogit")
        base = run("rev-parse", "HEAD", cwd=self.work)
        m2 = self.commit("mio side")
        run("push", "-q", str(self.mg.path("P")), "HEAD:refs/heads/main", cwd=self.work)
        run("reset", "-q", "--hard", base, cwd=self.work)
        g2 = self.commit("github side")
        run("push", "-q", str(self.gh.path("P")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(), "P", "miogit")
        self.assertTrue(any("diverged" in c for c in self.report.conflicts))
        self.assertEqual(self.refs(self.mg.path("P"))["refs/heads/main"], m2)
        self.assertEqual(self.refs(self.gh.path("P"))["refs/heads/main"], g2)
        self.assertEqual(self.mg.states["P"]["refs"]["refs/heads/main"], base)  # last synced kept
        self.assertEqual(self.report.errors, [])  # a divergence is a warning, not a failed job

    def test_unrelated_github_repo_with_same_name_is_left_alone(self):
        self.mg.create("Same", False, "")
        self.commit("mio")
        run("push", "-q", str(self.mg.path("Same")), "HEAD:refs/heads/main", cwd=self.work)
        self.gh.create("Same", False, "")
        self.gh.meta["Same"]["topics"] = []
        other = self.tmp / "other"
        run("init", "-q", str(other))
        (other / "x").write_text("x")
        run("add", "-A", cwd=other)
        run("commit", "-qm", "unrelated", cwd=other)
        g = run("rev-parse", "HEAD", cwd=other)
        run("push", "-q", str(self.gh.path("Same")), "HEAD:refs/heads/main", cwd=other)
        S.run_repo(self.syncer(), "Same", "miogit")
        self.assertTrue(any("shares no history" in c for c in self.report.conflicts))
        self.assertEqual(self.refs(self.gh.path("Same"))["refs/heads/main"], g)
        self.assertFalse(self.mg.states["Same"]["linked"])  # not linked, conflict recorded for the console
        self.assertIn("shares no history", self.mg.states["Same"]["conflicts"][0])

    def test_sync_off_does_nothing(self):
        self.mg.create("Off", False, "")
        self.mg.meta["Off"]["sync"] = False
        self.commit("x")
        run("push", "-q", str(self.mg.path("Off")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(), "Off", "miogit")
        self.assertNotIn("Off", self.gh.meta)

    def test_reconcile_imports_new_github_repo_but_not_old_ones(self):
        self.gh.create("NewOnGh", True, "")
        self.commit("gh")
        run("push", "-q", str(self.gh.path("NewOnGh")), "HEAD:refs/heads/main", cwd=self.work)
        self.gh.create("OldOnGh", False, "")
        self.gh.meta["OldOnGh"]["created_at"] = "2020-01-01T00:00:00Z"
        S.run_reconcile(self.syncer())
        self.assertIn("NewOnGh", self.mg.meta)
        self.assertTrue(self.mg.meta["NewOnGh"]["private"])
        self.assertNotIn("OldOnGh", self.mg.meta)
        self.assertEqual(self.report.conflicts, [])

    def test_repo_deleted_on_one_side_is_not_recreated(self):
        self.mg.create("Gone", False, "")
        self.commit("x")
        run("push", "-q", str(self.mg.path("Gone")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(), "Gone", "miogit")
        del self.gh.meta["Gone"]  # deleted on GitHub
        S.run_reconcile(self.syncer())
        self.assertNotIn("Gone", self.gh.meta)
        self.assertTrue(any("deleted on GitHub" in n for n in self.report.notices))

    def test_private_names_are_masked_in_the_public_log(self):
        import contextlib, io
        self.mg.create("SecretProj", True, "")
        self.mg.create("OpenProj", False, "")
        self.commit("x")
        for n in ("SecretProj", "OpenProj"):
            run("push", "-q", str(self.mg.path(n)), "HEAD:refs/heads/main", cwd=self.work)
        sy = self.syncer()
        sy.r.salt = b"k"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            S.run_reconcile(sy)
            print(sy.r.summary(masked=True))
        out = buf.getvalue()
        self.assertNotIn("secretproj", out.lower())
        self.assertNotIn("refs/heads/main -> GitHub", out.split("OpenProj")[0] if False else "\n".join(l for l in out.splitlines() if "private-" in l))
        self.assertIn("private-", out)
        self.assertIn("OpenProj", out)
        self.assertIn("SecretProj: created org/SecretProj (private)", sy.r.summary(masked=False))

    def test_dry_run_changes_nothing(self):
        self.mg.create("Dry", False, "")
        self.commit("x")
        run("push", "-q", str(self.mg.path("Dry")), "HEAD:refs/heads/main", cwd=self.work)
        S.run_repo(self.syncer(dry=True), "Dry", "miogit")
        self.assertNotIn("Dry", self.gh.meta)
        self.assertNotIn("Dry", self.mg.states)


if __name__ == "__main__":
    unittest.main()
