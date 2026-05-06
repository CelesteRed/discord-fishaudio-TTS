import json
from pathlib import Path

from discord_fish_tts.whitelist import UserWhitelist


def test_owner_is_always_allowed(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.is_allowed(1) is True
    assert wl.is_owner(1) is True


def test_non_owner_not_allowed_by_default(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.is_allowed(2) is False
    assert wl.is_owner(2) is False


def test_add_then_allowed(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.add(2) is True
    assert wl.is_allowed(2) is True


def test_add_idempotent(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.add(2) is True
    assert wl.add(2) is False  # already present


def test_owner_cannot_be_added(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.add(1) is False


def test_remove(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    wl.add(2)
    assert wl.remove(2) is True
    assert wl.is_allowed(2) is False


def test_remove_nonexistent(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.remove(2) is False


def test_owner_cannot_be_removed(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    assert wl.remove(1) is False
    assert wl.is_allowed(1) is True


def test_list_users_excludes_owner_and_is_sorted(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "wl.json")
    wl.add(5)
    wl.add(3)
    wl.add(7)
    assert wl.list_users() == [3, 5, 7]


def test_persistence_across_reload(tmp_path: Path):
    path = tmp_path / "wl.json"
    wl1 = UserWhitelist(owner_id=1, store_path=path)
    wl1.add(2)
    wl1.add(3)

    wl2 = UserWhitelist(owner_id=1, store_path=path)
    assert wl2.list_users() == [2, 3]


def test_handles_missing_file(tmp_path: Path):
    wl = UserWhitelist(owner_id=1, store_path=tmp_path / "missing.json")
    assert wl.list_users() == []


def test_handles_corrupt_json_gracefully(tmp_path: Path):
    path = tmp_path / "wl.json"
    path.write_text("{ not valid json")
    wl = UserWhitelist(owner_id=1, store_path=path)
    assert wl.list_users() == []  # falls back to empty set, doesn't crash


def test_save_creates_parent_dir(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "wl.json"
    wl = UserWhitelist(owner_id=1, store_path=nested)
    wl.add(2)
    assert nested.exists()


def test_save_format_is_readable_json(tmp_path: Path):
    path = tmp_path / "wl.json"
    wl = UserWhitelist(owner_id=1, store_path=path)
    wl.add(2)
    wl.add(3)
    data = json.loads(path.read_text())
    assert data == {"allowed_users": [2, 3]}
