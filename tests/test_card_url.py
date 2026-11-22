"""The card's address changes whenever the card file changes.

Browsers and the HA service worker cache the card by its URL, so a card fix
that keeps the same ``?v=`` is never fetched. The version carries a short
hash of the card file as well as the integration version.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

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


# A cached card changes only when its file name changes.
# Change the card: append to CARD_BUILDS, then put the build and hash here.
CARD_AT_BUILD = ("20261122175733", "129ea601")


def test_a_changed_card_gets_a_new_file_name():
    card = Path(integration.__file__).parent / "www" / integration.CARD_FILE
    digest = hashlib.sha256(card.read_bytes()).hexdigest()[:8]
    assert (integration.CARD_BUILD, digest) == CARD_AT_BUILD


def test_the_card_address_carries_the_build():
    path = f"/album_slideshow_static/album-slideshow-card-{integration.CARD_BUILD}.js"
    assert integration._card_url("1.2.3-abcd") == f"{path}?v=1.2.3-abcd"
    assert integration._card_url(None) == path


def test_registering_replaces_every_older_card_name_and_nothing_else():
    old = [
        "/album_slideshow_static/album-slideshow-card.js?v=1",
        "/album_slideshow_static/album-slideshow-card-20250101000000.js?v=2",
    ]
    keep = [
        "/local/another-card.js",
        "/album_slideshow_static/album-slideshow-card.js.map",
    ]
    items = [{"id": str(i), "url": url} for i, url in enumerate([*old, *keep])]

    created = []

    class Resources:
        def async_items(self):
            return list(items)

        async def async_delete_item(self, item_id):
            items[:] = [i for i in items if i["id"] != item_id]

        async def async_create_item(self, item):
            created.append(item)
            items.append({"id": f"new{len(created)}", "url": item["url"]})

    hass = SimpleNamespace(data={"lovelace": {"resources": Resources()}})
    url = integration._card_url("3")
    assert asyncio.run(integration._try_register_lovelace_resource(hass, url))
    assert [i["url"] for i in items] == [*keep, url]
    before = [dict(i) for i in items]
    assert asyncio.run(integration._try_register_lovelace_resource(hass, url))
    assert items == before, "registering again changes nothing"


def test_the_new_name_is_served_before_it_is_registered(monkeypatch):
    from homeassistant.components import http

    events = []

    async def serve(configs):
        events.append(("serve", [c[0] for c in configs]))

    async def register(hass, url):
        events.append(("register", url.split("?")[0]))
        return True

    async def run(function, *args):
        return function(*args)

    monkeypatch.setattr(integration, "CARD_BUILDS", ("20250101000000", integration.CARD_BUILD))
    monkeypatch.setattr(http, "StaticPathConfig", lambda *a: a, raising=False)
    monkeypatch.setattr(integration, "_try_register_lovelace_resource", register)
    hass = SimpleNamespace(
        data={},
        http=SimpleNamespace(async_register_static_paths=serve),
        async_add_executor_job=run,
    )
    asyncio.run(integration._async_register_card(hass))
    new = integration._card_url(None)
    older = "/album_slideshow_static/album-slideshow-card-20250101000000.js"
    assert events == [
        ("serve", [older, new, integration.CARD_STATIC_PATH]),
        ("register", new),
    ], "an older name keeps answering, so a cached page never gets a 404"
