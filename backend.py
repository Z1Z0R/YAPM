#!/usr/bin/env python3
"""Yet Another Plugin Manager: bounded marketplace reads and serialized Omarchy CLI operations."""
import concurrent.futures
import fcntl
import functools
import gzip
import json
import os
from pathlib import Path
import re
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
MOVE_ACTIONS = {"move-" + section: section for section in ("left", "centerBegin", "center", "centerEnd", "right", "tray")}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
SHA_RE = re.compile(r"[a-fA-F0-9]{40,64}\Z")
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="ssh -oBatchMode=yes")
ENV.setdefault("OMARCHY_SHELL_IPC_TIMEOUT", "8s")


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


def load(name, default):
    try:
        if name == "catalog":
            compressed = CACHE / "catalog.json.gz"
            if compressed.exists():
                with gzip.open(compressed, "rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("Catalog cache exceeds decoded size limit")
                return json.loads(data)
            legacy = CACHE / "catalog.json"
            if legacy.stat().st_size > MAX_BYTES:
                raise ValueError("Catalog cache exceeds size limit")
            value = json.loads(legacy.read_bytes())
            save("catalog", value)
            return value
        return json.loads((CACHE / (name + ".json")).read_text())
    except (OSError, ValueError, EOFError):
        return default


def save(name, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if name == "catalog" and len(raw) > MAX_BYTES:
        raise ValueError("Catalog cache exceeds decoded size limit")
    CACHE.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".state-", dir=CACHE)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(gzip.compress(raw, mtime=0) if name == "catalog" else raw)
        os.replace(path, CACHE / (name + (".json.gz" if name == "catalog" else ".json")))
        if name == "catalog":
            (CACHE / "catalog.json").unlink(missing_ok=True)
    finally:
        if os.path.exists(path):
            os.unlink(path)


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
                         observed=text(item.get("upstreamObservedCommit"), 64)))
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
        if cached.get("at") and isinstance(etag, str) and 0 < len(etag) <= 512 and "\r" not in etag and "\n" not in etag:
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
    result = {"rows": rows, "at": int(time.time()), "etag": etag}
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


def check_updates():
    rows = [row for row in installed() if not row.get("firstParty") and row["id"] != PLUGIN_ID]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(check_one, rows))
    save("updates", {"rows": results, "at": int(time.time())})
    failures = sum(row["state"] == "error" for row in results.values())
    if failures:
        raise RuntimeError(f"{failures} update checks failed. See the affected plugins; try again later.")


def perform(action, plugin_id):
    if action == "catalog":
        fetch_catalog()
        return
    if action == "check":
        check_updates()
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
        command = ["omarchy", "plugin", "add", listing["repo"], "--yes"]
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


def start(action, plugin_id):
    if action not in ("catalog", "check", "install", "install-enable", "enable", "disable", "remove", "update") and action not in MOVE_ACTIONS:
        raise ValueError("Unknown action")
    CACHE.mkdir(parents=True, exist_ok=True)
    lock = open(CACHE / "operation.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another plugin operation is already running")
    save("job", dict(busy=True, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, at=int(time.time())))
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", action, plugin_id, str(lock.fileno())],
                             start_new_session=True, pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    lock.close()
    return {"started": True, "pid": child.pid}


def worker(action, plugin_id, fd):
    try:
        perform(action, plugin_id)
        save("job", dict(busy=False, ok=True, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, at=int(time.time())))
    except Exception as error:
        save("job", dict(busy=False, ok=False, action="move" if action in MOVE_ACTIONS else action, id=plugin_id, error=str(error)[-3000:], at=int(time.time())))
    finally:
        os.close(int(fd))


def view(tab="installed", query="", page="0", origin="all", collapsed=""):
    local = installed()
    catalog = load("catalog", {"rows": [], "at": 0})
    listings = {row["id"]: row for row in catalog["rows"]}
    updates = load("updates", {"rows": {}, "at": 0})
    local_ids = {row["id"] for row in local}
    for row in local:
        listing = listings.get(row["id"], {})
        row["description"] = text(row.get("description") or listing.get("description"), 280)
        checked = updates["rows"].get(row["id"], {})
        row["updateState"] = checked.get("state", "unchecked") if checked.get("head") == row["head"] else "unchecked"
        row["updateError"] = checked.get("error", "")
        row["latest"] = listing.get("version", "")
        row["positionable"] = "bar-widget" in row.get("kinds", []) and "bar" not in row.get("kinds", [])
        row["manageable"] = row["id"] != PLUGIN_ID
        row["updatable"] = eligible(row) and row["manageable"]
    count = sum(row["updateState"] == "available" for row in local)
    if tab == "marketplace":
        rows = [dict(row, installed=row["id"] in local_ids) for row in catalog["rows"]]
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
        with open(CACHE / "operation.lock", "a") as lock:
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
