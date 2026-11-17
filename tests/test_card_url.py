"""The card's address changes whenever the card file changes.

Browsers and the HA service worker cache the card by its URL, so a card fix
that keeps the same ``?v=`` is never fetched. The version carries a short
hash of the card file as well as the integration version.
"""
from __future__ import annotations

import json
from pathlib import Path

from custom_components import album_slideshow as integration


def _integration(tmp_path: Path, card: str, version: str | None = "1.2.3") -> str:
    (tmp_path / "www").mkdir(parents=True)
    (tmp_path / "www" / integration.CARD_FILE).write_text(card)
    if version is not None:
        (tmp_path / "manifest.json").write_text(json.dumps({"version": version}))
    return str(tmp_path)


def test_version_names_the_release_and_the_card(tmp_path):
    key = integration._read_card_version(_integration(tmp_path, "card one"))
    assert key.startswith("1.2.3-")
    assert len(key) == len("1.2.3-") + 8


def test_a_changed_card_gets_a_new_version(tmp_path):
    one = integration._read_card_version(_integration(tmp_path / "a", "card one"))
    two = integration._read_card_version(_integration(tmp_path / "b", "card two"))
    again = integration._read_card_version(_integration(tmp_path / "c", "card one"))
    assert one != two
    assert one == again


def test_no_manifest_still_versions_by_the_card(tmp_path):
    key = integration._read_card_version(_integration(tmp_path, "card", version=None))
    assert key and "-" not in key


def test_no_card_falls_back_to_the_release(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "1.2.3"}))
    assert integration._read_card_version(str(tmp_path)) == "1.2.3"
