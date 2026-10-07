"""Publishing must reject even forcibly tracked private artifacts and old metadata."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_publish.py"
spec = importlib.util.spec_from_file_location("check_publish", SCRIPT)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


@pytest.mark.parametrize(
    "path",
    [
        "data/catalog/index.html",
        "custom/manifest.json",
        "exports/private.md",
        "sources/seeds.toml",
        ".env",
        ".env.production",
        "keys.env",
        "profile/browser-profile/Preferences",
        "queue.sqlite",
        "renamed.db-wal",
        "captured/movie.mp4",
        "logs/run.txt",
        ".publish/checkout/README.md",
    ],
)
def test_private_artifact_paths_are_rejected_even_if_approved(path):
    with pytest.raises(ValueError):
        publish.validate_file(path, "100644", b"private", {path})


def test_database_disguised_as_source_is_rejected():
    with pytest.raises(ValueError, match="binary"):
        publish.validate_file(
            "src/distiller/fake.py",
            "100644",
            b"SQLite format 3\x00private",
            {"src/distiller/fake.py"},
        )


def test_symlink_to_private_material_is_rejected():
    with pytest.raises(ValueError, match="symlinks"):
        publish.validate_file("README.md", "120000", b"../private", {"README.md"})


def test_policy_cannot_approve_private_or_escaping_paths():
    for path in ("data/cards.json", "../secret.py", "C:/keys.py"):
        with pytest.raises(ValueError):
            publish.read_policy(json.dumps([path]).encode())


def test_unknown_public_filename_needs_explicit_review():
    with pytest.raises(ValueError, match="unapproved"):
        publish.validate_file("notes.md", "100644", b"unexpected personal notes", {"README.md"})


def test_example_library_requires_synthetic_capture_and_reserved_url():
    sample = {
        "library": "bookmarks",
        "items": [
            {"url": "https://example.org/source", "capture": {"origin": "synthetic fixture"}}
        ],
    }
    publish.validate_sample(json.dumps(sample).encode())
    sample["items"][0]["url"] = "https://private.example/source"
    with pytest.raises(ValueError, match="reserved"):
        publish.validate_sample(json.dumps(sample).encode())


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


def repository(tmp_path, email="123+example@users.noreply.github.com"):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Example")
    git(tmp_path, "config", "user.email", email)
    (tmp_path / "README.md").write_text("Public synthetic project", encoding="utf-8")
    (tmp_path / publish.POLICY).write_text(
        json.dumps([publish.POLICY, "README.md"]), encoding="utf-8"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Synthetic initial commit")
    return tmp_path


def test_full_history_rejects_data_removed_from_latest_commit(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    (repo / "cards.json").write_text('{"private":"derived material"}', encoding="utf-8")
    git(repo, "add", "cards.json")
    git(repo, "commit", "-m", "Accidental artifact")
    git(repo, "rm", "cards.json")
    git(repo, "commit", "-m", "Remove artifact")
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match=r"cards\.json"):
        publish.validate_history("HEAD")


def test_personal_author_email_prevents_publication(tmp_path, monkeypatch):
    repo = repository(tmp_path, "private@example.org")
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="no-reply"):
        publish.validate_history("HEAD")


def test_code_only_history_and_index_pass_policy(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.chdir(repo)
    _, commits, files, _ = publish.validate_history("HEAD")
    assert commits == 1 and files == 2
    assert publish.validate_tree(None)[0] == 2
