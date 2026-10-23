#!/usr/bin/env python3
"""MioGit <-> GitHub two-way sync, fast-forward only.

Runs in GitHub Actions (mio-co/miogit-sync, .github/workflows/sync.yml); the
source of truth for this file is sync/github/ in the MioGit repository.

    python sync.py event            # repository_dispatch: payload from $GITHUB_EVENT_PATH
    python sync.py repo <name>      # one repository (MioGit name)
    python sync.py reconcile        # every repository on both sides
    ... --dry-run                   # print the plan, change nothing

Rules (README.md has the long version):
  * a branch moves only by fast-forward; when the two sides diverged the branch
    is skipped and the run fails (red) with a warning. Nothing is ever forced.
  * tags never move: the same tag on two different objects is a conflict.
  * a ref deleted on one side is deleted on the other only while the other side
    still holds exactly the sha recorded at the last sync (push with lease);
    otherwise it is a conflict. Default branches are never deleted.
  * a whole repository deleted on one side is never deleted (nor re-created) on
    the other; that is reported as a notice.
  * a GitHub repository with the same name that did not come from MioGit (no
    `miogit-sync` topic, no shared history) is left alone (conflict warning).
  * visibility: a change on one side since the last sync is copied to the other;
    when unknown or both changed, private wins.

Environment: MIOGIT_SYNC_TOKEN (MioGit sync token), GH_SYNC_PAT (GitHub PAT).
Neither is ever printed; git gets them as http.extraHeader through
GIT_CONFIG_* environment variables, never on a command line or in a URL.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ZERO = "0" * 40
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HERE = Path(__file__).resolve().parent


# ---- report -----------------------------------------------------------------------


@dataclass
class Report:
    """conflicts fail the run (red); notices are informational.

    mio-co/miogit-sync is a public repository, so its logs and run summaries are
    public: every repository name that is not public on both sides is printed
    as "private-<8 hex>" (an HMAC of the name keyed with the sync token, stable
    but not guessable). The full, unmasked lines go to MioGit (per-repository
    state + the run report, see MioGit.put_report) and show in the console."""

    conflicts: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    by_repo: dict[str, list[str]] = field(default_factory=dict)  # unmasked conflicts per MioGit name
    salt: bytes = b""
    public: set[str] = field(default_factory=set)  # lower-case names public on every side
    names: set[str] = field(default_factory=set)  # lower-case names seen (masked unless public)

    def register(self, name: str | None, public: bool = False) -> None:
        if not name:
            return
        low = name.lower()
        self.names.add(low)
        if public:
            self.public.add(low)
        else:
            self.public.discard(low)

    def label(self, name: str) -> str:
        if name.lower() in self.public:
            return name
        return "private-" + hmac.new(self.salt, name.lower().encode(), hashlib.sha256).hexdigest()[:8]

    def mask(self, text: str) -> str:
        for low in sorted(self.names - self.public, key=len, reverse=True):
            text = re.sub(r"(?<![A-Za-z0-9._-])" + re.escape(low) + r"(?![A-Za-z0-9_-])", self.label(low), text, flags=re.I)
        return text

    def _line(self, repo: str, msg: str) -> tuple[str, str]:
        if repo.lower() not in self.names:
            self.register(repo)
        full = f"{repo}: {msg}"
        return full, self.mask(f"{self.label(repo)}: {msg}")

    def conflict(self, repo: str, msg: str) -> None:
        full, shown = self._line(repo, msg)
        self.conflicts.append(full)
        self.by_repo.setdefault(repo, []).append(msg)
        print(f"::warning title=sync conflict::{shown}", flush=True)

    def notice(self, repo: str, msg: str) -> None:
        full, shown = self._line(repo, msg)
        self.notices.append(full)
        print(f"::notice::{shown}", flush=True)

    def did(self, repo: str, msg: str) -> None:
        full, shown = self._line(repo, msg)
        self.actions.append(full)
        print(f"  {shown}", flush=True)

    def summary(self, masked: bool = True) -> str:
        out = ["## MioGit ⇄ GitHub sync", ""]
        if masked:
            out += ["Private repository names are masked here; the full report is in the MioGit console (repository settings).", ""]
        for title, items in (("Conflicts (skipped, need a human)", self.conflicts), ("Done", self.actions), ("Notices", self.notices)):
            out.append(f"### {title} ({len(items)})")
            out += [f"- {self.mask(x) if masked else x}" for x in items] or ["- none"]
            out.append("")
        return "\n".join(out)


# ---- the decision (pure, unit-tested) ------------------------------------------------


@dataclass
class Plan:
    to_github: list[tuple[str, str]] = field(default_factory=list)  # (ref, sha) fast-forward or create
    to_miogit: list[tuple[str, str]] = field(default_factory=list)
    del_github: list[tuple[str, str]] = field(default_factory=list)  # (ref, expected sha on GitHub)
    del_miogit: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    conflict_refs: set[str] = field(default_factory=set)

    def empty(self) -> bool:
        return not (self.to_github or self.to_miogit or self.del_github or self.del_miogit)


def plan_refs(
    m: dict[str, str],
    g: dict[str, str],
    s: dict[str, str],
    is_ancestor: Callable[[str, str], bool],
    protected: set[str] = frozenset(),  # type: ignore[assignment]
) -> Plan:
    """m / g: ref -> sha on MioGit / GitHub; s: ref -> sha at the last sync.
    is_ancestor(a, b): a is an ancestor of (or equal to) b. protected: default
    branches, never deleted."""
    p = Plan()

    def conflict(ref: str, msg: str) -> None:
        p.conflicts.append(f"{ref}: {msg}")
        p.conflict_refs.add(ref)

    for ref in sorted(set(m) | set(g)):
        mv, gv, sv = m.get(ref), g.get(ref), s.get(ref)
        if mv == gv:
            continue
        tag = ref.startswith("refs/tags/")
        if mv and gv:
            if tag:
                conflict(ref, f"tag points at different objects (MioGit {mv[:9]}, GitHub {gv[:9]})")
            elif is_ancestor(gv, mv):
                p.to_github.append((ref, mv))
            elif is_ancestor(mv, gv):
                p.to_miogit.append((ref, gv))
            else:
                conflict(ref, f"diverged (MioGit {mv[:9]}, GitHub {gv[:9]}); not forcing either side")
        elif mv:  # only on MioGit
            if sv is None:
                p.to_github.append((ref, mv))
            elif sv == mv:
                if ref in protected:
                    conflict(ref, "deleted on GitHub, but it is the default branch on MioGit; not deleting")
                else:
                    p.del_miogit.append((ref, mv))
            else:
                conflict(ref, f"deleted on GitHub but moved on MioGit since the last sync ({sv[:9]} -> {mv[:9]})")
        else:  # only on GitHub
            assert gv
            if sv is None:
                p.to_miogit.append((ref, gv))
            elif sv == gv:
                if ref in protected:
                    conflict(ref, "deleted on MioGit, but it is the default branch on GitHub; not deleting")
                else:
                    p.del_github.append((ref, gv))
            else:
                conflict(ref, f"deleted on MioGit but moved on GitHub since the last sync ({sv[:9]} -> {gv[:9]})")
    return p


def next_state_refs(m: dict[str, str], g: dict[str, str], s: dict[str, str]) -> dict[str, str]:
    """after a run: refs equal on both sides are synced; an unresolved ref keeps its old record."""
    out: dict[str, str] = {}
    for ref in set(m) | set(g) | set(s):
        mv, gv = m.get(ref), g.get(ref)
        if mv is not None and mv == gv:
            out[ref] = mv
        elif ref in s and (mv is not None or gv is not None):
            out[ref] = s[ref]
    return dict(sorted(out.items()))


def visibility_change(m_priv: bool, g_priv: bool, s_priv: bool | None) -> tuple[str, bool] | None:
    """(side to change, new private flag) or None. The side that changed since the
    last sync wins; unknown / both changed: private wins."""
    if m_priv == g_priv:
        return None
    if s_priv is not None:
        if m_priv != s_priv and g_priv == s_priv:
            return ("github", m_priv)
        if g_priv != s_priv and m_priv == s_priv:
            return ("miogit", g_priv)
    return ("github", True) if m_priv else ("miogit", True)


# ---- HTTP ------------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


def http(method: str, url: str, headers: dict[str, str], body: object | None = None) -> tuple[int, object | None]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"user-agent": "miogit-sync", **headers}
    if data is not None:
        h["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")[:300]
    return status, parsed


class GitHub:
    def __init__(self, org: str, token: str, api: str = "https://api.github.com"):
        self.org, self.api = org, api.rstrip("/")
        self.h = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}

    def _call(self, method: str, path: str, body: object | None = None, ok=(200, 201, 204)) -> object | None:
        st, data = http(method, self.api + path, self.h, body)
        if st not in ok:
            msg = data.get("message") if isinstance(data, dict) else data
            raise HttpError(st, f"{method} {path}: {msg}")
        return data

    def repo(self, name: str) -> dict | None:
        st, data = http("GET", f"{self.api}/repos/{self.org}/{name}", self.h)
        if st == 404:
            return None
        if st != 200 or not isinstance(data, dict):
            raise HttpError(st, f"GET repo {name}")
        return data

    def repos(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            data = self._call("GET", f"/orgs/{self.org}/repos?type=all&per_page=100&page={page}")
            assert isinstance(data, list)
            out += data
            if len(data) < 100:
                return out
            page += 1

    def create(self, name: str, private: bool, description: str) -> dict:
        data = self._call("POST", f"/orgs/{self.org}/repos", {"name": name, "private": private, "description": description[:350], "auto_init": False})
        assert isinstance(data, dict)
        return data

    def set_private(self, name: str, private: bool) -> None:
        self._call("PATCH", f"/repos/{self.org}/{name}", {"private": private})

    def set_default_branch(self, name: str, branch: str) -> None:
        self._call("PATCH", f"/repos/{self.org}/{name}", {"default_branch": branch})

    def add_topic(self, name: str, topic: str) -> None:
        data = self._call("GET", f"/repos/{self.org}/{name}/topics")
        names = list(data.get("names", [])) if isinstance(data, dict) else []
        if topic not in names:
            self._call("PUT", f"/repos/{self.org}/{name}/topics", {"names": names + [topic]})


class MioGit:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.h = {"authorization": "Basic " + base64.b64encode(f"sync:{token}".encode()).decode()}

    def _call(self, method: str, path: str, body: object | None = None) -> object | None:
        st, data = http(method, f"{self.base}/~sync/api{path}", self.h, body)
        if st != 200:
            msg = data.get("error") if isinstance(data, dict) else data
            raise HttpError(st, f"{method} {path}: {msg}")
        return data

    def repos(self) -> list[dict]:
        data = self._call("GET", "/repos")
        assert isinstance(data, dict)
        return data["repos"]

    def create(self, name: str, private: bool, description: str) -> None:
        self._call("POST", f"/repos/{name}", {"create": True, "private": private, "description": description or ""})

    def set_private(self, name: str, private: bool) -> None:
        self._call("POST", f"/repos/{name}", {"private": private})

    def put_state(self, name: str, state: dict) -> None:
        self._call("PUT", f"/state/{name}", state)

    def put_report(self, report: dict) -> None:
        self._call("PUT", "/report", report)


# ---- git -------------------------------------------------------------------------------


class GitError(Exception):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and r.returncode != 0:
        raise GitError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def setup_git_auth(miogit_git: str, miogit_token: str, github_git: str, gh_token: str) -> None:
    """credentials as http.<url>.extraHeader via GIT_CONFIG_* (never argv, never URLs)."""
    pairs = []
    if miogit_token and miogit_git.startswith("http"):
        pairs.append((f"http.{miogit_git.rstrip('/')}/.extraheader", "Authorization: Basic " + base64.b64encode(f"sync:{miogit_token}".encode()).decode()))
    if gh_token and github_git.startswith("http"):
        pairs.append((f"http.{github_git.rstrip('/')}/.extraheader", "Authorization: Basic " + base64.b64encode(f"x-access-token:{gh_token}".encode()).decode()))
    os.environ["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        os.environ[f"GIT_CONFIG_KEY_{i}"] = k
        os.environ[f"GIT_CONFIG_VALUE_{i}"] = v


def ls_remote(url: str) -> tuple[str | None, dict[str, str]]:
    """(HEAD's branch ref or None, {refs/heads/*, refs/tags/*: sha})"""
    r = git("ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*")
    head, refs = None, {}
    for line in r.stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            head = line[5:-5]
            continue
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}") or not SHA_RE.match(sha):
            continue
        if ref.startswith("refs/heads/") or ref.startswith("refs/tags/"):
            refs[ref] = sha
    return head, refs


class Local:
    """a bare cache repository holding both sides under refs/m/* and refs/g/*"""

    def __init__(self, path: Path):
        self.path = path
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            git("init", "-q", "--bare", str(path))
            git("config", "gc.auto", "0", cwd=path)

    def fetch(self, side: str, url: str) -> None:
        git("fetch", "-q", "--no-tags", "--prune", url, f"+refs/heads/*:refs/{side}/heads/*", f"+refs/tags/*:refs/{side}/tags/*", cwd=self.path)

    def has(self, sha: str) -> bool:
        return git("cat-file", "-e", f"{sha}^{{object}}", cwd=self.path, check=False).returncode == 0

    def is_ancestor(self, a: str, b: str) -> bool:
        if a == b:
            return True
        r = git("merge-base", "--is-ancestor", a, b, cwd=self.path, check=False)
        if r.returncode in (0, 1):
            return r.returncode == 0
        raise GitError(f"merge-base: {r.stderr.strip()[-300:]}")

    def related(self, a: list[str], b: list[str]) -> bool:
        for x in a:
            for y in b:
                if x == y or git("merge-base", x, y, cwd=self.path, check=False).returncode == 0:
                    return True
        return False

    def push(self, url: str, src_side: str, ref: str) -> tuple[bool, str]:
        """plain push (no force): the remote refuses anything but a fast-forward / create"""
        local = f"refs/{src_side}/{ref[len('refs/'):]}"
        r = git("push", "--porcelain", url, f"{local}:{ref}", cwd=self.path, check=False)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]

    def delete(self, url: str, ref: str, expect: str) -> tuple[bool, str]:
        """delete only while the remote still has `expect` (lease)"""
        r = git("push", "--porcelain", f"--force-with-lease={ref}:{expect}", url, f":{ref}", cwd=self.path, check=False)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]


# ---- config ---------------------------------------------------------------------------


@dataclass
class Config:
    org: str
    miogit_url: str
    github_git: str
    topic: str
    since: str
    aliases: dict[str, str]
    exclude: set[str]

    @staticmethod
    def load(path: Path) -> "Config":
        c = json.loads(path.read_text())
        return Config(
            org=c["org"],
            miogit_url=os.environ.get("MIOGIT_URL", c["miogit_url"]).rstrip("/"),
            github_git=os.environ.get("GITHUB_GIT_BASE", "https://github.com").rstrip("/"),
            topic=c.get("topic", "miogit-sync"),
            since=c.get("since", ""),
            aliases=dict(c.get("aliases", {})),
            exclude={x.lower() for x in c.get("exclude", [])},
        )

    def gh_name(self, mname: str) -> str:
        return self.aliases.get(mname, mname)

    def m_name(self, gname: str, known: list[str]) -> str:
        for m, g in self.aliases.items():
            if g.lower() == gname.lower():
                return m
        if gname in known:
            return gname
        low = [k for k in known if k.lower() == gname.lower()]
        return low[0] if low else gname

    def miogit_git(self, mname: str) -> str:
        return f"{self.miogit_url}/{mname}.git"

    def github_git_url(self, gname: str) -> str:
        return f"{self.github_git}/{self.org}/{gname}.git"


# ---- one repository ------------------------------------------------------------------


class Syncer:
    def __init__(self, cfg: Config, gh: GitHub, mg: MioGit, cache: Path, report: Report, dry: bool = False, single_cache: bool = False):
        self.cfg, self.gh, self.mg, self.cache, self.r, self.dry, self.single_cache = cfg, gh, mg, cache, report, dry, single_cache

    def save_conflicts(self, mname: str, state: dict | None) -> None:
        """record this run's conflicts (unmasked) on a repository the run stopped early on"""
        if self.dry:
            return
        st = dict(state or {"linked": False})
        st["conflicts"] = list(self.r.by_repo.get(mname, []))
        st["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self.mg.put_state(mname, st)
        except HttpError:
            pass

    def local(self, gname: str) -> Local:
        return Local(self.cache if self.single_cache else self.cache / f"{gname.lower()}.git")

    def sync(self, mname: str, mrepo: dict | None, gname: str, grepo: dict | None, allow_import: bool) -> None:
        cfg, r = self.cfg, self.r
        if gname.lower() in cfg.exclude:
            return
        # names are masked in the public log unless the repository is public everywhere it exists
        m_pub = mrepo is None or (not mrepo.get("deleted") and mrepo.get("private") is False)
        g_pub = grepo is None or grepo.get("private") is False
        pub = bool(mrepo or grepo) and m_pub and g_pub
        for n in (mname, gname, (grepo or {}).get("name")):
            r.register(n, pub)
        if not NAME_RE.match(mname) or not NAME_RE.match(gname):
            r.notice(gname, "name cannot be used on MioGit; skipped")
            return
        state = (mrepo or {}).get("state") or None
        linked = bool(state and state.get("linked"))
        if mrepo and not mrepo.get("sync", True):
            r.notice(mname, "sync is off for this repository")
            return
        if mrepo and mrepo.get("deleted"):
            if grepo:
                r.notice(mname, "deleted on MioGit (GitHub copy kept, not re-created)")
            mrepo = None
            if linked:
                return
        if grepo and grepo.get("name", "").lower() != gname.lower():
            r.conflict(gname, f"GitHub name redirects to renamed repository {grepo.get('name')}; not touching it")
            return
        created_m = created_g = False

        if not mrepo:
            if not grepo:
                return
            if not allow_import:
                r.notice(gname, "exists only on GitHub (created before sync started); not imported")
                return
            if self.dry:
                r.did(mname, f"would create on MioGit ({'private' if grepo['private'] else 'public'})")
                return
            self.mg.create(mname, bool(grepo["private"]), grepo.get("description") or "")
            mrepo = {"name": mname, "private": bool(grepo["private"]), "sync": True, "state": None}
            created_m = True
            r.did(mname, f"created on MioGit ({'private' if grepo['private'] else 'public'})")

        if not grepo:
            if linked:
                r.notice(mname, "deleted on GitHub (MioGit copy kept, not re-created)")
                return
            if self.dry:
                r.did(mname, f"would create {cfg.org}/{gname} ({'private' if mrepo['private'] else 'public'})")
                return
            grepo = self.gh.create(gname, bool(mrepo["private"]), mrepo.get("description") or "")
            self.gh.add_topic(gname, cfg.topic)
            created_g = True
            r.did(mname, f"created {cfg.org}/{gname} ({'private' if mrepo['private'] else 'public'})")

        m_url, g_url = cfg.miogit_git(mname), cfg.github_git_url(gname)
        m_head, m_refs = ls_remote(m_url)
        g_head, g_refs = ls_remote(g_url)
        local: Local | None = None

        def fetch_both() -> Local:
            nonlocal local
            if local is None:
                local = self.local(gname)
                if m_refs:
                    local.fetch("m", m_url)
                if g_refs:
                    local.fetch("g", g_url)
            return local

        if not linked and not created_m and not created_g:
            topics = grepo.get("topics") or []
            ok = not m_refs or not g_refs or cfg.topic in topics or bool(set(m_refs.items()) & set(g_refs.items()))
            if not ok:
                lc = fetch_both()
                heads = lambda refs: [v for k, v in refs.items() if k.startswith("refs/heads/")]
                ok = lc.related(heads(m_refs), heads(g_refs))
            if not ok:
                r.conflict(mname, f"{cfg.org}/{gname} already exists and shares no history with MioGit; not syncing it")
                self.save_conflicts(mname, state)
                return
            if not self.dry and cfg.topic not in topics:
                self.gh.add_topic(gname, cfg.topic)
            r.did(mname, f"linked with existing {cfg.org}/{gname}")

        # visibility
        m_priv, g_priv = bool(mrepo["private"]), bool(grepo["private"])
        if not created_m and not created_g:
            ch = visibility_change(m_priv, g_priv, state.get("private") if state else None)
            if ch:
                side, priv = ch
                label = "private" if priv else "public"
                if self.dry:
                    r.did(mname, f"would make {side} copy {label}")
                else:
                    try:
                        if side == "github":
                            self.gh.set_private(gname, priv)
                            g_priv = priv
                        else:
                            self.mg.set_private(mname, priv)
                            m_priv = priv
                        r.did(mname, f"made {side} copy {label} (visibility follows the side that changed)")
                    except HttpError as e:
                        r.conflict(mname, f"cannot make {side} copy {label}: {e}")

        s_refs = dict((state or {}).get("refs") or {})
        protected = {x for x in (m_head, g_head) if x}
        if m_refs != g_refs:
            lc = fetch_both()
            plan = plan_refs(m_refs, g_refs, s_refs, lc.is_ancestor, protected)
        else:
            plan = Plan()
        for c in plan.conflicts:
            r.conflict(mname, c)

        if self.dry:
            for ref, sha in plan.to_github:
                r.did(mname, f"would push {ref} {sha[:9]} -> GitHub")
            for ref, sha in plan.to_miogit:
                r.did(mname, f"would push {ref} {sha[:9]} -> MioGit")
            for ref, _ in plan.del_github:
                r.did(mname, f"would delete {ref} on GitHub")
            for ref, _ in plan.del_miogit:
                r.did(mname, f"would delete {ref} on MioGit")
            return

        changed = False
        # branches before tags, so a tag's commit is already there; default branch first on a new repository
        order = lambda items: sorted(items, key=lambda x: (x[0].startswith("refs/tags/"), x[0] != m_head))
        for ref, sha in order(plan.to_github):
            ok, msg = fetch_both().push(g_url, "m", ref)
            changed |= ok
            (r.did(mname, f"{ref} -> GitHub {sha[:9]}") if ok else r.conflict(mname, f"push {ref} to GitHub failed: {msg}"))
        for ref, sha in order(plan.to_miogit):
            ok, msg = fetch_both().push(m_url, "g", ref)
            changed |= ok
            (r.did(mname, f"{ref} -> MioGit {sha[:9]}") if ok else r.conflict(mname, f"push {ref} to MioGit failed: {msg}"))
        for ref, expect in plan.del_github:
            ok, msg = fetch_both().delete(g_url, ref, expect)
            changed |= ok
            (r.did(mname, f"deleted {ref} on GitHub (deleted on MioGit)") if ok else r.conflict(mname, f"delete {ref} on GitHub failed: {msg}"))
        for ref, expect in plan.del_miogit:
            ok, msg = fetch_both().delete(m_url, ref, expect)
            changed |= ok
            (r.did(mname, f"deleted {ref} on MioGit (deleted on GitHub)") if ok else r.conflict(mname, f"delete {ref} on MioGit failed: {msg}"))

        if created_g and m_head and m_head.startswith("refs/heads/") and changed:
            try:
                self.gh.set_default_branch(gname, m_head[len("refs/heads/"):])
            except HttpError as e:
                r.notice(mname, f"could not set the GitHub default branch: {e}")

        if changed:
            _, m_refs = ls_remote(m_url)
            _, g_refs = ls_remote(g_url)
        new_state = {
            "linked": True,
            "github": f"{cfg.org}/{gname}",
            "private": m_priv if m_priv == g_priv else (state or {}).get("private"),
            "refs": next_state_refs(m_refs, g_refs, s_refs),
            "conflicts": list(r.by_repo.get(mname, [])),
        }
        old_cmp = {k: v for k, v in (state or {}).items() if k != "at"}
        if new_state != old_cmp:
            new_state["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.mg.put_state(mname, new_state)


# ---- entry points ----------------------------------------------------------------------


def after_since(grepo: dict, since: str) -> bool:
    return bool(since) and (grepo.get("created_at") or "") >= since


def pre_register(r: Report, mrepos: list[dict], grepos: list[dict]) -> None:
    """mask every name up front; a name is public only if no side lists it as private / deleted"""
    pub: dict[str, bool] = {}
    for x in mrepos:
        low = x["name"].lower()
        pub[low] = pub.get(low, True) and not x.get("deleted") and x.get("private") is False
    for g in grepos:
        low = g["name"].lower()
        pub[low] = pub.get(low, True) and g.get("private") is False
    for low, p in pub.items():
        r.register(low, p)


def run_repo(sy: Syncer, name: str, origin: str, miogit_hint: str | None = None) -> None:
    cfg = sy.cfg
    sy.r.register(name)
    sy.r.register(miogit_hint)
    mrepos = sy.mg.repos()
    pre_register(sy.r, mrepos, [])
    by_name = {x["name"]: x for x in mrepos}
    if origin == "github":
        gname = name
        mname = miogit_hint if miogit_hint in by_name else cfg.m_name(gname, list(by_name))
    else:
        mname = name
        gname = cfg.gh_name(mname)
    grepo = sy.gh.repo(gname)
    pre_register(sy.r, mrepos, [grepo] if grepo else [])
    allow_import = origin == "github" and grepo is not None and (after_since(grepo, cfg.since) or cfg.topic in (grepo.get("topics") or []))
    sy.sync(mname, by_name.get(mname), gname, grepo, allow_import)


def run_reconcile(sy: Syncer) -> None:
    cfg = sy.cfg
    mrepos = sy.mg.repos()
    glist = sy.gh.repos()
    pre_register(sy.r, mrepos, glist)
    grepos = {g["name"].lower(): g for g in glist}
    seen: set[str] = set()
    lower_m: dict[str, str] = {}
    for m in sorted(mrepos, key=lambda x: x["name"]):
        mname = m["name"]
        gname = cfg.gh_name(mname)
        key = gname.lower()
        if key in lower_m:
            sy.r.conflict(mname, f"maps to the same GitHub name as {lower_m[key]}; skipped")
            continue
        lower_m[key] = mname
        seen.add(key)
        grepo = grepos.get(key)
        try:
            sy.sync(mname, m, grepo["name"] if grepo else gname, grepo, allow_import=False)
        except (GitError, HttpError, OSError) as e:
            sy.r.conflict(mname, f"error: {e}")
    for key, g in sorted(grepos.items()):
        if key in seen or key in cfg.exclude:
            continue
        allow = after_since(g, cfg.since) or cfg.topic in (g.get("topics") or [])
        mname = cfg.m_name(g["name"], [m["name"] for m in mrepos])
        try:
            sy.sync(mname, None, g["name"], g, allow_import=allow)
        except (GitError, HttpError, OSError) as e:
            sy.r.conflict(g["name"], f"error: {e}")


def dispatch_mode(a, ap, cfg: Config, gh: GitHub, mg: MioGit, report: Report) -> None:
    if a.mode == "event":
        ev = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        p = ev.get("client_payload") or {}
        name = str(p.get("repo") or "")
        origin = "github" if p.get("origin") == "github" else "miogit"
        if not NAME_RE.match(name):
            report.conflict("sync", "bad repository name in payload")
            return
        sy = Syncer(cfg, gh, mg, Path(a.cache), report, a.dry_run, single_cache=True)
        hint = p.get("miogit") if isinstance(p.get("miogit"), str) else None
        try:
            run_repo(sy, name, origin, hint)
        except (GitError, HttpError, OSError) as e:
            report.conflict(name, f"error: {e}")
    elif a.mode == "repo":
        if not a.name or not NAME_RE.match(a.name):
            ap.error("repo mode needs a repository name")
        sy = Syncer(cfg, gh, mg, Path(a.cache), report, a.dry_run)
        try:
            run_repo(sy, a.name, a.origin)
        except (GitError, HttpError, OSError) as e:
            report.conflict(a.name, f"error: {e}")
    else:
        sy = Syncer(cfg, gh, mg, Path(a.cache), report, a.dry_run)
        run_reconcile(sy)



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["event", "repo", "reconcile"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--origin", choices=["miogit", "github"], default="miogit")
    ap.add_argument("--config", default=str(HERE / "sync.json"))
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    cfg = Config.load(Path(a.config))
    m_tok = os.environ.get("MIOGIT_SYNC_TOKEN", "")
    g_tok = os.environ.get("GH_SYNC_PAT", "")
    if not m_tok or not g_tok:
        print("::error::MIOGIT_SYNC_TOKEN / GH_SYNC_PAT are not set (repository secrets)")
        return 2
    setup_git_auth(cfg.miogit_url, m_tok, cfg.github_git, g_tok)
    report = Report(salt=m_tok.encode())
    gh = GitHub(cfg.org, g_tok, os.environ.get("GITHUB_API_BASE", "https://api.github.com"))
    mg = MioGit(cfg.miogit_url, m_tok)

    try:
        dispatch_mode(a, ap, cfg, gh, mg, report)
    except Exception as e:  # never let a traceback print unmasked names
        report.conflict("sync", f"unexpected error: {type(e).__name__}: {e}")

    summary = report.summary(masked=True)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary + "\n")
    if not a.dry_run and (report.conflicts or a.mode == "reconcile"):
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        try:
            mg.put_report({
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mode": a.mode,
                "run": run_url,
                "conflicts": report.conflicts[:200],
                "actions": report.actions[:200],
                "notices": report.notices[:200],
            })
        except (HttpError, OSError) as e:
            print(f"::warning::could not store the full report in MioGit: {report.mask(str(e))}")
    return 1 if report.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
