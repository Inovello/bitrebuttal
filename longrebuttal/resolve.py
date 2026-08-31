"""URL validation + manifest resolution.

Accepted inputs:
  * ``org/repo`` (optionally ``org/repo@revision``)
  * ``https://huggingface.co/org/repo`` (or ``/tree/<rev>``)
  * ``https://huggingface.co/org/repo/resolve/<rev>/path/file.gguf``
  * any other direct URL

Sizes and hashes are ALWAYS taken from the source, never from the client
(API contract: "re-resolves server-side, never trusts client sizes").
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx

HF_HOST = "huggingface.co"
HF_API = "https://huggingface.co/api/models/{repo}/tree/{rev}?recursive=true&expand=false"
HF_FILE = "https://huggingface.co/{repo}/resolve/{rev}/{path}"

REPO_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*/[\w.\-]+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class ResolveError(Exception):
    """User-facing resolution failure - ``str(exc)`` goes straight into the API error."""


@dataclass
class ManifestFile:
    name: str                      # relative path under the destination
    url: str                       # ORIGINAL url, re-submitted on every relaunch
    size: int = 0                  # 0 = unknown (server sent no length)
    sha256: Optional[str] = None


@dataclass
class Manifest:
    kind: str                      # "hf" | "direct"
    name: str                      # job name: repo id, or filename for direct urls
    files: List[ManifestFile] = field(default_factory=list)
    repo: Optional[str] = None
    revision: Optional[str] = None
    resolved_at: float = field(default_factory=time.time)
    warnings: List[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def payload(self) -> Dict[str, Any]:
        """POST /api/resolve response body (API contract)."""
        return {
            "repo": self.repo or self.name,
            "revision": self.revision or "-",
            "resolvedAt": time.strftime("%H:%M:%S", time.localtime(self.resolved_at)),
            "files": [
                {"name": f.name, "bytes": f.size, "sha256": bool(f.sha256), "selected": True}
                for f in self.files
            ],
        }


# ---------------------------------------------------------------- input parsing


def parse_input(raw: str) -> Tuple[str, Dict[str, str]]:
    """-> ("hf_repo"|"hf_file"|"direct", details)."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        raise ResolveError("Enter a HuggingFace repo (org/repo) or a direct download URL.")

    if "://" not in s:
        head = s.split("/", 1)[0]
        if REPO_RE.match(s.split("@", 1)[0]) and "." not in head:
            repo, _, rev = s.partition("@")
            return "hf_repo", {"repo": repo, "revision": rev or "main"}
        if "." in head:          # looked like a host, e.g. huggingface.co/org/repo
            s = "https://" + s
        else:
            raise ResolveError(
                f"Could not understand '{raw}'. Use org/repo, a HuggingFace URL, or a direct URL.")

    u = urlparse(s)
    if u.scheme not in ("http", "https"):
        raise ResolveError(f"Unsupported URL scheme '{u.scheme}' - only http(s) is supported.")
    if not u.netloc:
        raise ResolveError(f"'{raw}' is not a valid URL.")

    host = u.netloc.split("@")[-1].split(":")[0].lower()
    parts = [p for p in u.path.split("/") if p]

    if host == HF_HOST or host.endswith("." + HF_HOST):
        if parts and parts[0] in ("datasets", "spaces"):
            raise ResolveError("Only HuggingFace model repos are supported in v1 "
                               "(datasets/spaces are not).")
        if "resolve" in parts:
            i = parts.index("resolve")
            if i >= 2 and len(parts) > i + 2:
                repo = "/".join(parts[:i])
                rev = parts[i + 1]
                path = "/".join(parts[i + 2:])
                return "hf_file", {"repo": repo, "revision": rev, "path": unquote(path),
                                   "url": s}
            raise ResolveError(f"'{raw}' is not a complete HuggingFace file URL.")
        if len(parts) >= 2:
            repo = "/".join(parts[:2])
            rev = "main"
            if len(parts) >= 4 and parts[2] in ("tree", "blob"):
                rev = parts[3]
            return "hf_repo", {"repo": repo, "revision": rev}
        raise ResolveError(f"'{raw}' is not a HuggingFace repo URL (expected /org/repo).")

    return "direct", {"url": s}


# ---------------------------------------------------------------- HF


def _hf_headers() -> Dict[str, str]:
    h = {"User-Agent": "longrebuttal/0.1"}
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _hf_error(resp: httpx.Response, repo: str) -> ResolveError:
    if resp.status_code == 404:
        return ResolveError(f"Repository not found on huggingface.co: {repo} "
                            "(check spelling, or the revision).")
    if resp.status_code in (401, 403):
        return ResolveError(f"Repository {repo} is gated or private - access denied. "
                            "Accept the licence on huggingface.co and set HF_TOKEN.")
    return ResolveError(f"huggingface.co returned HTTP {resp.status_code} for {repo}.")


def resolve_hf_repo(repo: str, revision: str = "main", client: Optional[httpx.Client] = None
                    ) -> Manifest:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=False)
    url = HF_API.format(repo=repo, rev=revision or "main")
    entries: List[Dict[str, Any]] = []
    try:
        while url:
            resp = client.get(url, headers=_hf_headers())
            if resp.status_code != 200:
                raise _hf_error(resp, repo)
            body = resp.json()
            if not isinstance(body, list):
                raise ResolveError(f"Unexpected response from huggingface.co for {repo}.")
            entries.extend(body)
            url = _next_link(resp.headers.get("link", ""))
    except httpx.HTTPError as exc:
        raise ResolveError("Could not reach huggingface.co - check the URL or your "
                           f"connection. ({type(exc).__name__})") from exc
    finally:
        if own:
            client.close()

    files: List[ManifestFile] = []
    for e in entries:
        if e.get("type") != "file":
            continue
        path = e.get("path")
        if not path:
            continue
        lfs = e.get("lfs") or {}
        oid = lfs.get("oid") or lfs.get("sha256")
        sha = oid.lower() if isinstance(oid, str) and HEX64_RE.match(oid.lower()) else None
        size = int(lfs.get("size") or e.get("size") or 0)
        files.append(ManifestFile(
            name=path, url=HF_FILE.format(repo=repo, rev=revision or "main", path=path),
            size=size, sha256=sha))
    if not files:
        raise ResolveError(f"No downloadable files found in {repo}@{revision}.")
    files.sort(key=lambda f: f.name)
    return Manifest(kind="hf", name=repo, files=files, repo=repo, revision=revision or "main")


def _next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        seg = part.split(";")
        if len(seg) >= 2 and 'rel="next"' in seg[1].replace(" ", "").replace("'", '"'):
            return seg[0].strip().strip("<>")
    return None


# ---------------------------------------------------------------- direct / HF file


def head_direct(url: str, client: Optional[httpx.Client] = None) -> ManifestFile:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=False)
    headers = _hf_headers() if HF_HOST in urlparse(url).netloc.lower() else \
        {"User-Agent": "longrebuttal/0.1"}
    try:
        resp = client.head(url, headers=headers)
        if resp.status_code in (403, 405, 501) or (
                resp.status_code == 200 and not resp.headers.get("content-length")
                and not resp.headers.get("x-linked-size")):
            # Some CDNs refuse HEAD; a 1-byte ranged GET gets us the same metadata.
            resp = client.get(url, headers={**headers, "Range": "bytes=0-0"})
    except httpx.HTTPError as exc:
        raise ResolveError(f"Could not reach {urlparse(url).netloc} - check the URL or your "
                           f"connection. ({type(exc).__name__})") from exc
    finally:
        if own:
            client.close()

    if resp.status_code == 404:
        raise ResolveError(f"File not found (HTTP 404): {url}")
    if resp.status_code in (401, 403):
        raise ResolveError(f"Access denied (HTTP {resp.status_code}) for {url} - the file may be "
                           "gated or need a token.")
    if resp.status_code >= 400:
        raise ResolveError(f"Server returned HTTP {resp.status_code} for {url}")

    h = resp.headers
    size = 0
    for key in ("x-linked-size", "content-length"):
        try:
            if h.get(key):
                size = int(h[key])
                break
        except ValueError:
            pass
    if size <= 1 and h.get("content-range"):
        try:
            size = int(h["content-range"].split("/")[-1])
        except ValueError:
            pass

    sha = None
    for key in ("x-linked-etag", "etag"):
        val = (h.get(key) or "").strip('"').lower()
        if HEX64_RE.match(val):
            sha = val
            break

    name = _filename_from(resp, url)
    return ManifestFile(name=name, url=url, size=size, sha256=sha)


def _filename_from(resp: httpx.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        return unquote(m.group(1)).strip()
    path = urlparse(str(resp.url) if resp.url else url).path
    base = unquote(os.path.basename(path)) or "download.bin"
    return base


# ---------------------------------------------------------------- entry point


def resolve(raw_url: str) -> Manifest:
    """Resolve any accepted input into an authoritative manifest. Raises ResolveError."""
    kind, det = parse_input(raw_url)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=False) as client:
        if kind == "hf_repo":
            return resolve_hf_repo(det["repo"], det.get("revision", "main"), client=client)

        if kind == "hf_file":
            repo, rev, path = det["repo"], det.get("revision", "main"), det["path"]
            mf: Optional[ManifestFile] = None
            try:                                   # tree API gives the LFS sha256
                tree = resolve_hf_repo(repo, rev, client=client)
                for f in tree.files:
                    if f.name == path:
                        mf = ManifestFile(name=os.path.basename(path), url=f.url,
                                          size=f.size, sha256=f.sha256)
                        break
            except ResolveError:
                mf = None
            if mf is None:
                mf = head_direct(det["url"], client=client)
                mf.name = os.path.basename(path) or mf.name
            return Manifest(kind="hf", name=mf.name, files=[mf], repo=repo, revision=rev)

        mf = head_direct(det["url"], client=client)
        warn = [] if mf.size else ["Server did not report a size - disk preflight skipped."]
        return Manifest(kind="direct", name=mf.name, files=[mf], repo=None, revision=None,
                        warnings=warn)
