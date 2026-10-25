"""Persistence for the bot. GitHub is the database.

The bot runs inside GitHub Actions whose file system is discarded after
every run, so everything that must survive lives on GitHub:

    <repo>/data/state.json         users, configs, runtime info  (Contents API)
    secret gist per user           subscription file -> stable public raw URL
                                   https://gist.githubusercontent.com/<user>/<id>/raw/<file>
    repository secrets             KILLDPI_PRIVATE_KEY (created on first run if missing)
    workflow_dispatch              relaunches the bot before the 6 h job limit

Writes use the file SHA so overlapping runs cannot silently clobber each
other (409/422 -> re-read -> retry). Unchanged content is never committed.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from typing import Any

import httpx

log = logging.getLogger("storage")
API = "https://api.github.com"


class StorageError(RuntimeError):
    pass


class Storage:
    def __init__(self, token: str, repo_full_name: str, branch: str = "main"):
        self.token = token
        self.repo = repo_full_name
        self.owner = repo_full_name.split("/")[0]
        self.branch = branch
        self._cache: dict[str, tuple[str, str]] = {}  # path -> (sha, content)
        self.http = httpx.Client(
            base_url=API, timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "killdpi-bot"})

    # ------------------------------------------------------------ low level
    def _req(self, method: str, url: str, retries: int = 3, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.http.request(method, url, **kw)
                if r.status_code in (502, 503, 504) or (r.status_code == 403 and "rate limit" in r.text.lower()):
                    raise StorageError(f"{r.status_code}: {r.text[:200]}")
                return r
            except (httpx.HTTPError, StorageError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise StorageError(f"{method} {url} failed: {last}")

    # ------------------------------------------------------------ repo files
    def read(self, path: str, default: str | None = None) -> str | None:
        r = self._req("GET", f"/repos/{self.repo}/contents/{path}", params={"ref": self.branch})
        if r.status_code == 404:
            self._cache.pop(path, None)
            return default
        if r.status_code != 200:
            raise StorageError(f"read {path}: {r.status_code} {r.text[:200]}")
        d = r.json()
        content = base64.b64decode(d["content"]).decode()
        self._cache[path] = (d["sha"], content)
        return content

    def write(self, path: str, content: str, message: str, retries: int = 4) -> bool:
        """Return True if a commit was made."""
        for attempt in range(retries):
            if path not in self._cache:
                self.read(path)
            cached = self._cache.get(path)
            if cached and cached[1] == content:
                return False
            body = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": self.branch,
                    "committer": {"name": "killdpi-bot", "email": "killdpi-bot@users.noreply.github.com"}}
            if cached:
                body["sha"] = cached[0]
            r = self._req("PUT", f"/repos/{self.repo}/contents/{path}", json=body)
            if r.status_code in (200, 201):
                self._cache[path] = (r.json()["content"]["sha"], content)
                return True
            if r.status_code in (409, 422) and attempt < retries - 1:
                self._cache.pop(path, None)
                time.sleep(1 + attempt)
                continue
            raise StorageError(f"write {path}: {r.status_code} {r.text[:300]}")
        return False

    # ------------------------------------------------------------ state
    @staticmethod
    def empty_state() -> dict[str, Any]:
        return {"version": 2, "users": {}, "configs": {}, "settings": {}, "runtime": {}, "stats": {}}

    def load_state(self) -> dict[str, Any]:
        raw = self.read("data/state.json")
        st = json.loads(raw) if raw else self.empty_state()
        for k, v in self.empty_state().items():
            st.setdefault(k, v)
        return st

    def save_state(self, state: dict[str, Any], message: str = "bot: update state") -> bool:
        return self.write("data/state.json", json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", message)

    # ------------------------------------------------------------ gists (subscriptions)
    def gist_create(self, filename: str, content: str, description: str = "KILLDPI subscription") -> tuple[str, str]:
        r = self._req("POST", "/gists", json={"description": description, "public": False,
                                               "files": {filename: {"content": content}}})
        if r.status_code != 201:
            raise StorageError(f"gist create: {r.status_code} {r.text[:200]}")
        gid = r.json()["id"]
        return gid, self.gist_raw_url(gid, filename)

    def gist_update(self, gist_id: str, filename: str, content: str) -> bool:
        r = self._req("PATCH", f"/gists/{gist_id}", json={"files": {filename: {"content": content}}})
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise StorageError(f"gist update: {r.status_code} {r.text[:200]}")
        return True

    def gist_delete(self, gist_id: str) -> None:
        self._req("DELETE", f"/gists/{gist_id}")

    def gist_raw_url(self, gist_id: str, filename: str) -> str:
        return f"https://gist.githubusercontent.com/{self.owner}/{gist_id}/raw/{filename}"

    def publish_subscription(self, user: dict, content: str) -> str:
        """Create the user's gist on first use, then update it in place. Returns the stable URL."""
        fname = user.get("sub_file") or f"killdpi-{secrets.token_hex(4)}.txt"
        user["sub_file"] = fname
        if user.get("sub_gist"):
            if self.gist_update(user["sub_gist"], fname, content):
                return self.gist_raw_url(user["sub_gist"], fname)
            log.warning("gist %s vanished, recreating", user["sub_gist"])
        gid, url = self.gist_create(fname, content)
        user["sub_gist"] = gid
        return url

    # ------------------------------------------------------------ actions
    def dispatch_workflow(self, workflow_file: str, inputs: dict[str, str] | None = None) -> bool:
        r = self._req("POST", f"/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
                      json={"ref": self.branch, "inputs": inputs or {}})
        if r.status_code != 204:
            log.error("dispatch failed: %s %s", r.status_code, r.text[:200])
            return False
        return True

    def list_runs(self, workflow_file: str, status: str = "in_progress") -> list[dict]:
        r = self._req("GET", f"/repos/{self.repo}/actions/workflows/{workflow_file}/runs",
                      params={"status": status, "per_page": 20})
        if r.status_code != 200:
            return []
        return r.json().get("workflow_runs", [])

    def cancel_run(self, run_id: int) -> bool:
        r = self._req("POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel")
        return r.status_code == 202

    def set_secret(self, name: str, value: str) -> bool:
        """Create/update a repository Actions secret (libsodium sealed box)."""
        try:
            from nacl import encoding, public  # type: ignore
        except ImportError:
            log.error("PyNaCl missing; cannot store secret %s", name)
            return False
        r = self._req("GET", f"/repos/{self.repo}/actions/secrets/public-key")
        if r.status_code != 200:
            log.error("public-key: %s %s", r.status_code, r.text[:200])
            return False
        pk = r.json()
        sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())).encrypt(value.encode())
        r = self._req("PUT", f"/repos/{self.repo}/actions/secrets/{name}",
                      json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]})
        return r.status_code in (201, 204)

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(12)


class LocalStorage(Storage):
    """Drop-in replacement for local development / tests (no GitHub)."""

    def __init__(self, root: str = "data_local"):
        self.root = root
        self.repo = "local/local"
        self.owner = "local"
        self.branch = "local"
        self._cache = {}
        self.dispatched: list[dict] = []
        self.secrets: dict[str, str] = {}
        os.makedirs(root, exist_ok=True)

    def _p(self, *parts: str) -> str:
        p = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def read(self, path: str, default: str | None = None) -> str | None:
        p = self._p(path)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else default

    def write(self, path: str, content: str, message: str, retries: int = 3) -> bool:
        p = self._p(path)
        if os.path.exists(p) and open(p, encoding="utf-8").read() == content:
            return False
        open(p, "w", encoding="utf-8").write(content)
        return True

    def gist_create(self, filename: str, content: str, description: str = "") -> tuple[str, str]:
        gid = secrets.token_hex(8)
        open(self._p("gists", gid, filename), "w", encoding="utf-8").write(content)
        return gid, self.gist_raw_url(gid, filename)

    def gist_update(self, gist_id: str, filename: str, content: str) -> bool:
        if not os.path.isdir(os.path.join(self.root, "gists", gist_id)):
            return False
        open(self._p("gists", gist_id, filename), "w", encoding="utf-8").write(content)
        return True

    def gist_delete(self, gist_id: str) -> None:
        import shutil
        shutil.rmtree(os.path.join(self.root, "gists", gist_id), ignore_errors=True)

    def gist_raw_url(self, gist_id: str, filename: str) -> str:
        return f"file://{os.path.abspath(os.path.join(self.root, 'gists', gist_id, filename))}"

    def dispatch_workflow(self, workflow_file: str, inputs: dict[str, str] | None = None) -> bool:
        self.dispatched.append({"workflow": workflow_file, "inputs": inputs or {}})
        return True

    def list_runs(self, workflow_file: str, status: str = "in_progress") -> list[dict]:
        return []

    def cancel_run(self, run_id: int) -> bool:
        return True

    def set_secret(self, name: str, value: str) -> bool:
        self.secrets[name] = value
        return True
