"""Fail closed on unapproved files, private artifacts, author emails and detected secrets.

Only Git blobs are read. The checker never opens a local source database, browser profile
or credential file, and never prints a detected secret. Local privacy sweeps are separate.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

POLICY = ".public-files.json"
MAX_BYTES = 2_000_000
ROOT_INPUTS = {"seeds.toml", "frontier.toml"}
PRIVATE_DIRS = {
    "captures",
    "observations",
    "research",
    "catalog",
    "catalogs",
    "catalog-workflows",
    "exports",
    "screenshots",
    "shots",
    "media",
    "secrets",
    "logs",
}
PRIVATE_FILES = {
    "manifest.json",
    "cards.json",
    "catalog.json",
    "catalog.md",
    "llms.txt",
    "autopilot-report.json",
    ".automation-checkpoint.txt",
}
PRIVATE_SUFFIXES = {
    ".db",
    ".dump",
    ".log",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".mp3",
    ".wav",
    ".png",
    ".jpg",
    ".jpeg",
    ".zip",
    ".whl",
    ".gz",
    ".bak",
    ".backup",
}


def git(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Git command failed: " + " ".join(args[:2]))
    return result.stdout


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = [p.casefold() for p in path.parts]
    base = parts[-1] if parts else ""
    if base == ".env.example" and len(parts) == 1:
        return False
    directories = parts[:-1]
    if any(
        p in PRIVATE_DIRS
        or p.startswith(("data", ".venv", ".publish", ".security-audit"))
        or "browser-profile" in p
        or p == "__pycache__"
        for p in directories
    ):
        return True
    if base in ROOT_INPUTS | PRIVATE_FILES or base.startswith(".env") or base.endswith(".env"):
        return True
    return path.suffix.casefold() in PRIVATE_SUFFIXES or ".sqlite" in base or ".db-" in base


def read_policy(body: bytes) -> set[str]:
    value = json.loads(body)
    if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
        raise ValueError("publication policy must be a list of exact paths")
    if len(value) != len(set(value)):
        raise ValueError("duplicate publication paths")
    for name in value:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("invalid publication path")
        if private_path(name):
            raise ValueError("publication policy cannot approve private artifact paths")
    return set(value)


def validate_sample(body: bytes):
    sample = json.loads(body)
    if sample.get("library") != "bookmarks":
        raise ValueError("unrecognized synthetic library fixture")
    for item in sample.get("items", []):
        if not item["url"].startswith("https://example.org/"):
            raise ValueError("library sample must use reserved example URLs")
        origin = (item.get("capture") or {}).get("origin", "")
        if not origin.startswith("synthetic fixture"):
            raise ValueError("library sample must declare synthetic evidence")


def validate_file(name: str, mode: str, body: bytes, approved: set[str]):
    if private_path(name) or name not in approved:
        raise ValueError("unapproved or protected file")
    if mode not in {"100644", "100755"}:
        raise ValueError("symlinks/submodules are outside the publication boundary")
    if len(body) > MAX_BYTES or b"\x00" in body or body.startswith(b"SQLite format 3"):
        raise ValueError("binary or oversized artifact")
    if name.endswith(".json") and name not in {POLICY, "examples/distill/library.json"}:
        raise ValueError("unreviewed JSON artifact")
    if name == "examples/distill/library.json":
        validate_sample(body)


def entries(tree: str | None) -> list[tuple[str, str, str]]:
    raw = git("ls-tree", "-rz", tree) if tree else git("ls-files", "--stage", "-z")
    out = []
    for line in raw.split(b"\x00"):
        if not line:
            continue
        metadata, name = line.decode("utf-8").split("\t", 1)
        mode, kind, sha = metadata.split() if tree else (None, None, None)
        if tree:
            if kind != "blob":
                raise ValueError("non-file entry in publication tree")
        else:
            mode, sha, stage = metadata.split()
            if stage != "0":
                raise ValueError("resolve staged merge conflicts before publication")
        out.append((name, mode, sha))
    return out


def validate_tree(tree: str | None) -> tuple[int, list[bytes]]:
    files = entries(tree)
    policy = next((sha for name, _, sha in files if name == POLICY), None)
    if policy is None:
        raise ValueError("tree has no explicit publication policy")
    approved = read_policy(git("cat-file", "blob", policy))
    bodies, issues = [], []
    for name, mode, sha in files:
        body = git("cat-file", "blob", sha)
        try:
            validate_file(name, mode, body, approved)
        except (KeyError, TypeError, ValueError) as err:
            issues.append(f"{name}: {err}")
        bodies.append(body)
    if issues:
        raise ValueError("\n".join(issues))
    return len(files), bodies


def resolve_ref(ref: str) -> str:
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def validate_history(ref: str) -> tuple[str, int, int, bytes]:
    sha = resolve_ref(ref)
    identities = git("log", "--format=%ae%x00%ce", sha).decode().replace("\x00", "\n").splitlines()
    if any(not address.endswith("@users.noreply.github.com") for address in identities):
        raise ValueError("publication history contains a non-GitHub-no-reply author email")
    trees = sorted(set(git("log", "--format=%T", sha).decode().splitlines()))
    count = 0
    for tree in trees:
        files, _ = validate_tree(tree)
        count = max(count, files)
    messages = git("log", "--format=%B", sha)
    commits = len(git("rev-list", sha).splitlines())
    return sha, commits, count, messages


def scanner() -> str | None:
    explicit = os.environ.get("GITLEAKS_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("gitleaks")
    if found:
        return found
    common = Path(git("rev-parse", "--git-common-dir").decode().strip()).resolve()
    local = common / "local-tools" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    return str(local) if local.is_file() else None


def scan_secret(executable: str, sha: str | None, content: bytes):
    with tempfile.TemporaryDirectory(prefix="distill-secret-check-") as temporary:
        report = Path(temporary) / "findings.json"
        config = Path(temporary) / "defaults.toml"
        config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        args = [
            executable,
            "git" if sha else "stdin",
            "--redact=100",
            "--no-banner",
            "--log-level",
            "error",
            "--ignore-gitleaks-allow",
            "--config",
            str(config),
            "--gitleaks-ignore-path",
            temporary,
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        if sha:
            args += ["--log-opts=" + sha]
        result = subprocess.run(
            args, input=None if sha else content, capture_output=True, check=False
        )
        if result.returncode:
            findings = json.loads(report.read_text()) if report.exists() else []
            if findings:
                labels = [
                    f"{f.get('File', 'input')}:{f.get('StartLine', '?')} "
                    f"({f.get('RuleID', 'secret')})"
                    for f in findings
                ]
                raise ValueError("secret scan rejected publication: " + "; ".join(labels))
            raise ValueError("secret scanner failed; publication remains blocked")


def check(ref: str | None, *, staged: bool, require_gitleaks: bool):
    sha, messages = None, b""
    if staged:
        files, bodies = validate_tree(None)
        summary = {"scope": "index", "files": files}
        messages = b"\n".join(bodies)
    else:
        sha, commits, files, messages = validate_history(ref or "HEAD")
        summary = {"scope": "history", "commits": commits, "files": files, "commit": sha}
    executable = scanner()
    if require_gitleaks and not executable:
        raise ValueError(
            "Gitleaks is required before publication; install a verified official release"
        )
    if executable:
        if sha:
            scan_secret(executable, sha, b"")
        scan_secret(executable, None, messages)
    summary["secret_scan"] = "passed" if executable else "not run"
    print(json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--ref")
    selection.add_argument("--staged", action="store_true")
    selection.add_argument("--pre-push", action="store_true")
    parser.add_argument("--require-gitleaks", action="store_true")
    args = parser.parse_args()
    try:
        if args.pre_push:
            for line in sys.stdin:
                _, sha, _, _ = line.split()
                if re.fullmatch(r"0+", sha):
                    continue
                check(sha, staged=False, require_gitleaks=True)
        else:
            check(args.ref, staged=args.staged, require_gitleaks=args.require_gitleaks)
    except (ValueError, OSError) as err:
        print(str(err), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
