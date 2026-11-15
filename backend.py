#!/usr/bin/env python3
"""Yet Another Plugin Manager: bounded marketplace reads and serialized Omarchy CLI operations."""
import concurrent.futures
from collections import deque
from contextlib import contextmanager
import fcntl
import functools
import gzip
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

PLUGIN_ID = "yapm"
PLUGINS = Path.home() / ".config/omarchy/plugins"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / PLUGIN_ID
CATALOG_URL = "https://plugins.omarchy.org/catalog.json"
MAX_BYTES = 64 * 1024 * 1024
CATALOG_FORMAT = 2
MOVE_ACTIONS = {"move-" + section: section for section in ("left", "centerBegin", "center", "centerEnd", "right", "tray")}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
SHA_RE = re.compile(r"[a-fA-F0-9]{40,64}\Z")
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="ssh -oBatchMode=yes")
ENV.setdefault("OMARCHY_SHELL_IPC_TIMEOUT", "8s")
CACHE_DIR_FD = None


def valid_id(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) and ".." not in value


def text(value, limit=512):
    return value[:limit] if isinstance(value, str) else ""


def repo_url(value):
    if not isinstance(value, str) or len(value) > 512:
        return ""
    # A repository URL, never a transport helper, command, credential or URL option.
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+", value):
        return ""
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.hostname in ("localhost", "127.0.0.1"):
        return ""
    return value.rstrip("/").removesuffix(".git")


def run(args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", env=ENV, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Command failed")[-3000:])
    return result.stdout


class CacheSecurityError(RuntimeError):
    """Unsafe cache objects must never be treated as an empty cache."""


def check_cache_directory(fd, leaf=False):
    info = os.fstat(fd)
    trusted_owner = info.st_uid in (0, os.geteuid())
    writable = info.st_mode & 0o022
    sticky = info.st_mode & stat.S_ISVTX
    if not stat.S_ISDIR(info.st_mode) or not trusted_owner or (writable and not sticky):
        raise CacheSecurityError("Cache path contains an untrusted or writable directory")
    if leaf and (info.st_uid != os.geteuid() or writable):
        raise CacheSecurityError("The yapm cache directory must be owned by you and not writable by others")


@contextmanager
def cache_directory(inherited_fd=None):
    global CACHE_DIR_FD
    if CACHE_DIR_FD is not None:
        yield CACHE_DIR_FD
        return
    fd = None
    try:
        if inherited_fd is not None:
            if inherited_fd < 3:
                raise CacheSecurityError("Invalid inherited cache directory")
            fd = inherited_fd
        else:
            # Walk from a trusted root. Non-sticky shared writable ancestors
            # permit renaming private children and therefore cannot hold locks.
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open("/", flags)
            check_cache_directory(fd)
            remaining = deque(CACHE.absolute().parts[1:])
            symlinks = 0
            while remaining:
                part = remaining.popleft()
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if info.st_uid not in (0, os.geteuid()) or symlinks >= 40:
                        raise CacheSecurityError("Cache path contains an untrusted symlink")
                    symlinks += 1
                    destination = Path(os.readlink(part, dir_fd=fd))
                    parts = destination.parts
                    if destination.is_absolute():
                        os.close(fd)
                        fd = os.open("/", flags)
                        check_cache_directory(fd)
                        parts = parts[1:]
                    remaining.extendleft(reversed(parts))
                    continue
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                check_cache_directory(fd)
        check_cache_directory(fd, leaf=True)
        CACHE_DIR_FD = fd
        yield fd
    except OSError as error:
        raise CacheSecurityError("Cannot safely access the yapm cache: " + str(error)) from error
    finally:
        CACHE_DIR_FD = None
        if fd is not None:
            os.close(fd)


def check_cache_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or info.st_nlink != 1):
        raise CacheSecurityError("Cache files must be regular, singly linked files owned by you and not writable by others")
    return info


def open_cache_file(name, flags, directory_fd):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise CacheSecurityError("Cannot safely open cache file " + name) from error
    try:
        check_cache_file(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def open_cache_lock(directory_fd):
    fd = open_cache_file("operation.lock", os.O_RDWR | os.O_CREAT, directory_fd)
    return os.fdopen(fd, "r+b")


def load(name, default):
    with cache_directory() as directory_fd:
        try:
            filename = name + (".json.gz" if name == "catalog" else ".json")
            legacy = False
            try:
                fd = open_cache_file(filename, os.O_RDONLY, directory_fd)
            except FileNotFoundError:
                if name != "catalog":
                    return default
                fd = open_cache_file("catalog.json", os.O_RDONLY, directory_fd)
                legacy = True
            with os.fdopen(fd, "rb") as stream:
                if name == "catalog" and not legacy:
                    with gzip.GzipFile(fileobj=stream) as compressed:
                        data = compressed.read(MAX_BYTES + 1)
                else:
                    data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("Cache exceeds decoded size limit")
            value = json.loads(data)
            if legacy:
                save("catalog", value)
            return value
        except (OSError, ValueError, EOFError):
            return default


def save(name, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if name == "catalog" and len(raw) > MAX_BYTES:
        raise ValueError("Catalog cache exceeds decoded size limit")
    with cache_directory() as directory_fd:
        temporary = ".state-" + secrets.token_hex(16)
        fd = open_cache_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(gzip.compress(raw, mtime=0) if name == "catalog" else raw)
            filename = name + (".json.gz" if name == "catalog" else ".json")
            os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            if name == "catalog":
                try:
                    os.unlink("catalog.json", dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def commit_id(value):
    if isinstance(value, str) and len(value) in (40, 64) and SHA_RE.fullmatch(value):
        return value.lower()
    return ""


def verified_match(listing, revision, repository):
    verified = commit_id(listing.get("verifiedCommit"))
    return bool(listing.get("verifiedSnapshot") is True and verified
                and verified == commit_id(revision) and repository
                and listing.get("repo") == repository and repo_url(repository) == repository)


def verified_release_match(listing, release, repository):
    tag = release.get("tag")
    return bool(isinstance(tag, str) and 0 < len(tag) <= 128
                and release.get("repo") == repository
                and verified_match(listing, release.get("commit"), repository))


def normalize_catalog(data):
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError("Marketplace response is missing its plugin list")
    if len(data["plugins"]) > 20000:
        raise ValueError("Marketplace plugin count exceeds limit")
    rows, seen = [], set()
    for item in data["plugins"]:
        if not isinstance(item, dict) or not valid_id(item.get("id")) or item["id"] in seen:
            continue
        seen.add(item["id"])
        if item.get("sourceType") == "builtin":
            continue
        url = repo_url(item.get("repo"))
        rows.append(dict(id=item["id"], name=text(item.get("name"), 128) or item["id"],
                         description=text(item.get("description")), author=text(item.get("author"), 128),
                         version=text(item.get("version"), 64), repo=url,
                         installable=bool(url and item.get("installAvailable") is True
                                          and item.get("repositoryLayout") == "root-plugin"),
                         verification=text(item.get("verificationStatus"), 32),
                         verifiedSnapshot=item.get("verificationSnapshotStatus") == "verified",
                         verifiedCommit=commit_id(item.get("verificationCommit")),
                         observed=commit_id(item.get("upstreamObservedCommit"))))
    return sorted(rows, key=lambda row: row["name"].casefold())


def fetch_catalog():
    cached = load("catalog", {"rows": [], "at": 0})
    with tempfile.TemporaryDirectory(prefix="yapm-") as stage:
        path = Path(stage) / "catalog.json"
        headers = Path(stage) / "headers.txt"
        command = ["curl", "--fail", "--silent", "--show-error", "--location", "--compressed", "--proto", "=https",
             "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "45",
             "--max-filesize", str(MAX_BYTES), "--dump-header", str(headers),
             "--write-out", "%{http_code}", "--output", str(path)]
        etag = cached.get("etag", "")
        if cached.get("format") == CATALOG_FORMAT and cached.get("at") and isinstance(etag, str) and 0 < len(etag) <= 512 and "\r" not in etag and "\n" not in etag:
            command.extend(["--header", "If-None-Match: " + etag])
        status = run(command + [CATALOG_URL], timeout=50).strip()
        if status == "304" and cached.get("at"):
            cached["at"] = int(time.time())
            save("catalog", cached)
            return cached
        if status != "200":
            raise RuntimeError("Unexpected marketplace response: " + status)
        # --compressed decodes the response. Bound that decoded file too, so
        # the 64 MiB allowance applies to the catalog, not just its wire size.
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Marketplace response exceeds size limit")
        rows = normalize_catalog(json.loads(path.read_bytes()))
        etag = ""
        for line in headers.read_text(errors="replace").splitlines():
            if line.lower().startswith("http/"):
                etag = ""
            elif line.lower().startswith("etag:"):
                etag = line[5:].strip()[:512]
    result = {"rows": rows, "at": int(time.time()), "etag": etag, "format": CATALOG_FORMAT}
    save("catalog", result)
    return result


@functools.lru_cache(maxsize=1)
def official_metadata():
    root = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "shell/plugins"
    result = {}
    for path in list(root.rglob("manifest.json")) + list(root.rglob("*.manifest.json")):
        try:
            manifest = json.loads(path.read_text())
            if valid_id(manifest.get("id")):
                result[manifest["id"]] = manifest
        except (OSError, ValueError):
            pass
    return result


def installed():
    rows = json.loads(run(["omarchy", "plugin", "list", "--json"]))
    if not isinstance(rows, list):
        raise ValueError("Invalid response from Omarchy shell")
    for row in rows:
        if not valid_id(row.get("id")):
            raise ValueError("Invalid installed plugin ID")
        folder = PLUGINS / row["id"]
        row.update(version="", description="", repo="", git=False, head="")
        if row.get("firstParty"):
            manifest = official_metadata().get(row["id"], {})
            row.update(version=text(manifest.get("version"), 64),
                       description=text(manifest.get("description") or (manifest.get("barWidget") or {}).get("description")))
        if not row.get("firstParty"):
            try:
                manifest = json.loads((folder / "manifest.json").read_text())
                row.update(version=text(manifest.get("version"), 64), description=text(manifest.get("description")))
                if (folder / ".git").is_dir():
                    row.update(git=True,
                               repo=repo_url(run(["git", "-C", str(folder), "remote", "get-url", "origin"]).strip()),
                               head=run(["git", "-C", str(folder), "rev-parse", "HEAD"]).strip())
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
    return rows


def eligible(row):
    return bool(row.get("git") and not row.get("firstParty") and row.get("repo")
                and repo_url(row["repo"]) == row["repo"])


def check_one(row):
    result = {"head": row["head"], "state": "local"}
    if not eligible(row):
        return row["id"], result
    try:
        output = run(["git", "ls-remote", "--exit-code", "--", row["repo"], "HEAD"], timeout=20)
        remote = output.split()[0]
        if not SHA_RE.fullmatch(remote):
            raise ValueError("Invalid remote revision")
        state = "current"
        if remote != row["head"]:
            # A known remote commit behind our HEAD means the local plugin is ahead.
            ancestor = subprocess.run(["git", "-C", str(PLUGINS / row["id"]), "merge-base",
                                       "--is-ancestor", remote, row["head"]],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=ENV)
            state = "ahead" if ancestor.returncode == 0 else "available"
        result.update(state=state, remote=remote)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, IndexError) as error:
        result.update(state="error", error=str(error)[:300])
    return row["id"], result


def github_release_api(repository):
    if not repository or repo_url(repository) != repository:
        return ""
    parsed = urlsplit(repository)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) != 2:
        return ""
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        return ""
    return "https://api.github.com/repos/" + "/".join(parts) + "/releases/latest"


def release_checkable(listing):
    return bool(listing.get("verifiedSnapshot") is True
                and commit_id(listing.get("verifiedCommit"))
                and github_release_api(listing.get("repo")))


def check_release(listing):
    repository = listing["repo"]
    result = {"repo": repository, "tag": "", "commit": "", "at": int(time.time())}
    try:
        with tempfile.TemporaryDirectory(prefix="yapm-release-") as directory:
            path = Path(directory) / "release.json"
            run(["curl", "--fail", "--silent", "--show-error", "--location", "--compressed",
                 "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "5",
                 "--max-time", "15", "--max-filesize", "1048576", "--header",
                 "Accept: application/vnd.github+json", "--output", str(path),
                 github_release_api(repository)], timeout=20)
            if path.stat().st_size > 1048576:
                raise ValueError("Release response exceeds size limit")
            release = json.loads(path.read_bytes())
        tag = release.get("tag_name")
        if (release.get("draft") is not False or release.get("prerelease") is not False
                or not isinstance(tag, str) or not 0 < len(tag) <= 128):
            raise ValueError("No supported published release")
        reference = "refs/tags/" + tag
        run(["git", "check-ref-format", reference])
        output = run(["git", "ls-remote", "--exit-code", "--", repository,
                      reference, reference + "^{}"], timeout=20)
        refs = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] in (reference, reference + "^{}"):
                revision = commit_id(fields[0])
                if not revision:
                    raise ValueError("Invalid release revision")
                refs[fields[1]] = revision
        revision = refs.get(reference + "^{}") or refs.get(reference, "")
        if not revision:
            raise ValueError("Release tag could not be resolved")
        result.update(tag=tag, commit=revision)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, AttributeError):
        pass
    return listing["id"], result


def refresh_release_checks(plugin_ids, listings):
    targets = [listings[plugin_id] for plugin_id in dict.fromkeys(plugin_ids)
               if release_checkable(listings.get(plugin_id, {}))]
    if not targets:
        return
    cached = load("releases", {"rows": {}})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        cached["rows"].update(dict(pool.map(check_release, targets)))
    save("releases", cached)


def check_release_page(plugin_ids):
    ids = plugin_ids.split(",")
    if not 0 < len(ids) <= 20 or not all(valid_id(plugin_id) for plugin_id in ids):
        raise ValueError("Select up to 20 valid plugins for release verification")
    listings = {row["id"]: row for row in load("catalog", {"rows": []})["rows"]}
    refresh_release_checks(ids, listings)


def check_updates():
    rows = [row for row in installed() if not row.get("firstParty") and row["id"] != PLUGIN_ID]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(check_one, rows))
    save("updates", {"rows": results, "at": int(time.time())})
    listings = {row["id"]: row for row in load("catalog", {"rows": []})["rows"]}
    refresh_release_checks([row["id"] for row in rows
                            if row.get("repo") == listings.get(row["id"], {}).get("repo")], listings)
    failures = sum(row["state"] == "error" for row in results.values())
    if failures:
        raise RuntimeError(f"{failures} update checks failed. See the affected plugins; try again later.")


def perform(action, plugin_id, repository=""):
    if action == "catalog":
        fetch_catalog()
        return
    if action == "check":
        check_updates()
        return
    if action == "releases":
        check_release_page(plugin_id)
        return
    if not valid_id(plugin_id) or (plugin_id == PLUGIN_ID and action not in MOVE_ACTIONS):
        raise ValueError("This plugin cannot modify itself")
    catalog = {row["id"]: row for row in load("catalog", {"rows": []})["rows"]}
    rows = {row["id"]: row for row in installed()}
    row = rows.get(plugin_id)
    if action in ("install", "install-enable"):
        listing = catalog.get(plugin_id)
        if row or not listing or not listing["installable"] or not repo_url(listing["repo"]):
            raise ValueError("This marketplace entry is not installable")
        if not repository or repo_url(repository) != repository or listing["repo"] != repository:
            raise ValueError("Plugin repository changed or was not confirmed; review and confirm installation again")
        command = ["omarchy", "plugin", "add", repository, "--yes"]
        if action == "install-enable":
            command.append("--enable")
        run(command, timeout=180)
        return
    if not row:
        raise ValueError("Plugin is no longer installed")
    if action in MOVE_ACTIONS:
        kinds = row.get("kinds", [])
        if "bar-widget" not in kinds or "bar" in kinds:
            raise ValueError("This plugin does not provide a positionable bar widget")
        section = MOVE_ACTIONS[action]
        if row.get("enabled"):
            run(["omarchy", "bar", "move", plugin_id, "--section", section])
        else:
            run(["omarchy", "plugin", "enable", plugin_id, "--section", section])
    elif action == "enable":
        run(["omarchy", "plugin", "enable", plugin_id])
    elif action == "disable":
        if row.get("canDisable") is not True:
            raise ValueError("Omarchy does not allow disabling this plugin")
        run(["omarchy", "plugin", "disable", plugin_id])
    elif action == "remove":
        if row.get("firstParty"):
            raise ValueError("Built-in plugins cannot be removed")
        run(["omarchy", "plugin", "remove", plugin_id, "--yes"])
    elif action == "update":
        if not eligible(row):
            raise ValueError("Only installed HTTPS Git repositories can be updated")
        if run(["git", "-C", str(PLUGINS / plugin_id), "status", "--porcelain"]).strip():
            raise ValueError("Local changes found; save or commit them before updating")
        run(["omarchy", "plugin", "update", plugin_id, "--yes"], timeout=180)
        updates = load("updates", {"rows": {}})
        updates["rows"].pop(plugin_id, None)
        save("updates", updates)
    else:
        raise ValueError("Unknown action")


def start(action, plugin_id, repository=""):
    if action not in ("catalog", "check", "releases", "install", "install-enable", "enable", "disable", "remove", "update") and action not in MOVE_ACTIONS:
        raise ValueError("Unknown action")
    with cache_directory() as directory_fd:
        with open_cache_lock(directory_fd) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another plugin operation is already running")
            save("job", dict(busy=True, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, at=int(time.time())))
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", action,
                                      plugin_id, str(lock.fileno()), repository, str(directory_fd)],
                                     start_new_session=True, pass_fds=(lock.fileno(), directory_fd),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=ENV)
    return {"started": True, "pid": child.pid}


def worker(action, plugin_id, fd, repository="", directory_fd=None):
    if directory_fd is None or int(fd) < 3:
        raise CacheSecurityError("Worker requires inherited cache and lock descriptors")
    fd = int(fd)
    with cache_directory(int(directory_fd)) as directory_fd:
        try:
            info = check_cache_file(fd)
            lock_info = os.stat("operation.lock", dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(lock_info.st_mode) or (info.st_dev, info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise CacheSecurityError("Worker lock does not belong to its cache directory")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                perform(action, plugin_id, repository)
                save("job", dict(busy=False, ok=True, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, at=int(time.time())))
            except Exception as error:
                save("job", dict(busy=False, ok=False, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, error=str(error)[-3000:], at=int(time.time())))
        finally:
            os.close(fd)


def view(tab="installed", query="", page="0", origin="all", collapsed=""):
    local = installed()
    catalog = load("catalog", {"rows": [], "at": 0})
    listings = {row["id"]: row for row in catalog["rows"]}
    updates = load("updates", {"rows": {}, "at": 0})
    local_ids = {row["id"] for row in local}
    releases = load("releases", {"rows": {}})["rows"]
    for row in local:
        listing = listings.get(row["id"], {})
        row["description"] = text(row.get("description") or listing.get("description"), 280)
        checked = updates["rows"].get(row["id"], {})
        row["updateState"] = checked.get("state", "unchecked") if checked.get("head") == row["head"] else "unchecked"
        row["updateError"] = checked.get("error", "")
        release = releases.get(row["id"], {})
        row["verifiedMatch"] = verified_release_match(listing, release, row.get("repo"))
        row["verifiedReleaseTag"] = release.get("tag", "") if row["verifiedMatch"] else ""
        row["latest"] = listing.get("version", "")
        row["positionable"] = "bar-widget" in row.get("kinds", []) and "bar" not in row.get("kinds", [])
        row["manageable"] = row["id"] != PLUGIN_ID
        row["updatable"] = eligible(row) and row["manageable"]
    count = sum(row["updateState"] == "available" for row in local)
    if tab == "marketplace":
        rows = []
        for listing in catalog["rows"]:
            release = releases.get(listing["id"], {})
            matched = verified_release_match(listing, release, listing.get("repo"))
            rows.append(dict(listing, installed=listing["id"] in local_ids,
                             releaseCheckable=release_checkable(listing), verifiedMatch=matched,
                             verifiedReleaseTag=release.get("tag", "") if matched else ""))
    elif tab == "updates":
        rows = [row for row in local if row["updateState"] in ("available", "error")]
    else:
        rows = sorted(local, key=lambda row: (row.get("firstParty", False), row.get("enabled") is not True, row["name"].casefold()))
        if origin == "official":
            rows = [row for row in rows if row.get("firstParty") is True]
        elif origin == "third-party":
            rows = [row for row in rows if row.get("firstParty") is not True]
    query = query[:128].casefold()
    rows = [row for row in rows if query in (row["name"] + " " + row["id"] + " " + row.get("description", "") + " " + row.get("author", "")).casefold()]
    total = len(rows)
    enabled_count = sum(row.get("enabled") is True for row in rows)
    if tab == "installed" and total:
        collapsed = set(collapsed.split(","))
        display_rows = []
        for section, enabled in (("enabled", True), ("disabled", False)):
            group = [row for row in rows if (row.get("enabled") is True) == enabled]
            if section in collapsed or not group:
                display_rows.append(dict(id="", name="", version="", description="", enabled=enabled,
                                         sectionOnly=True, stateSection=section))
            else:
                display_rows.extend(group)
        rows = display_rows
    pages = max(1, (len(rows) + 19) // 20)
    page = min(max(0, int(page)), pages - 1)
    visible_rows = rows[page * 20:(page + 1) * 20]
    for index, row in enumerate(visible_rows):
        enabled = row.get("enabled") is True
        row["stateSection"] = "enabled" if enabled else "disabled"
        row["sectionStart"] = tab == "installed" and (index == 0 or enabled != (visible_rows[index - 1].get("enabled") is True))
        row["sectionCount"] = enabled_count if enabled else total - enabled_count
    job = load("job", {"busy": False})
    if job.get("busy"):
        with cache_directory() as directory_fd:
            with open_cache_lock(directory_fd) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    job.update(busy=False, ok=False, error="Previous operation was interrupted; refresh and try again")
                except BlockingIOError:
                    pass
    return dict(rows=visible_rows, total=total, page=page, pages=pages, updates=count,
                officialCount=sum(row.get("firstParty") is True for row in local),
                thirdPartyCount=sum(row.get("firstParty") is not True for row in local),
                catalogAt=catalog.get("at", 0), checkedAt=updates.get("at", 0), job=job)


def main():
    try:
        command, *args = sys.argv[1:]
        if command == "view":
            result = view(*args)
        elif command == "start":
            result = start(*args)
        elif command == "worker":
            worker(*args)
            return
        else:
            raise ValueError("Unknown command")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)[-3000:]}))
        sys.exit(1)


if __name__ == "__main__":
    main()
