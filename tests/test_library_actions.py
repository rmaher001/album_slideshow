"""Archive or trash the displayed photo in the Immich library itself."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.album_slideshow import _register_photo_services
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow import immich
from custom_components.album_slideshow.const import (
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_URL,
    PROVIDER_IMMICH,
)
from tests.test_photo_exclusions import _camera, _item


class _Response:
    def __init__(self, status):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, status=204):
        self.status = status
        self.calls = []

    def request(self, method, url, *, headers, json):
        self.calls.append((method, url, headers["x-api-key"], json))
        return _Response(self.status)


def _client(monkeypatch, status=204):
    session = _Session(status)
    monkeypatch.setattr(immich, "async_get_clientsession", lambda hass: session)
    return immich.ImmichClient(object(), "http://immich.test/api/", "secret"), session


def test_archive_sets_archive_visibility(monkeypatch):
    client, session = _client(monkeypatch)
    asyncio.run(client.async_archive_assets(["a", "b"]))
    assert session.calls == [
        ("PUT", "http://immich.test/api/assets", "secret", {"ids": ["a", "b"], "visibility": "archive"})
    ]


def test_trash_never_deletes_permanently(monkeypatch):
    client, session = _client(monkeypatch)
    asyncio.run(client.async_trash_assets(["a"]))
    assert session.calls == [
        ("DELETE", "http://immich.test/api/assets", "secret", {"ids": ["a"], "force": False})
    ]


def test_refused_request_raises(monkeypatch):
    client, _ = _client(monkeypatch, status=403)
    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(client.async_trash_assets(["a"]))


@pytest.mark.parametrize("action, method", [("archive", "async_archive_assets"), ("trash", "async_trash_assets")])
def test_coordinator_sends_the_action_with_its_own_credentials(monkeypatch, action, method):
    calls = []

    class FakeClient:
        def __init__(self, _hass, url, key):
            calls.append(("init", url, key))

        async def async_archive_assets(self, ids):
            calls.append(("async_archive_assets", ids))

        async def async_trash_assets(self, ids):
            calls.append(("async_trash_assets", ids))

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = object()
    coord.provider = PROVIDER_IMMICH
    coord.entry = SimpleNamespace(data={CONF_IMMICH_URL: "http://immich.test", CONF_IMMICH_API_KEY: "secret"})
    asyncio.run(coord.async_change_in_library(action, ["asset-1"]))
    assert calls == [("init", "http://immich.test", "secret"), (method, ["asset-1"])]
    with pytest.raises(ValueError, match="Unknown"):
        asyncio.run(coord.async_change_in_library("delete_forever", ["asset-1"]))


async def _immich_camera(monkeypatch, count=3, *, paired=False):
    items = [_item(f"asset-{index}", f"https://example.com/{index}") for index in range(count)]
    cam = await _camera(items, monkeypatch, paired=paired)
    cam.coordinator.provider = PROVIDER_IMMICH
    cam.coordinator.async_change_in_library = AsyncMock()
    return cam, items


@pytest.mark.parametrize("action", ["archive", "trash"])
def test_photo_leaves_the_slideshow_after_the_library_change(monkeypatch, action):
    async def run():
        cam, items = await _immich_camera(monkeypatch)
        await cam.async_change_in_library(action)
        cam.coordinator.async_change_in_library.assert_awaited_once_with(action, ["asset-0"])
        assert items[0].photo_id in cam.store.hidden_photo_ids
        assert items[0].photo_id not in cam._current_frame.meta["photo_ids"]

    asyncio.run(run())


def test_undo_hide_does_not_bring_back_an_archived_photo(monkeypatch):
    async def run():
        cam, items = await _immich_camera(monkeypatch)
        await cam.async_hide_photo()
        await cam.async_change_in_library("archive")
        assert cam.store.last_hidden_photo_ids == (items[0].photo_id,)
        await cam.async_restore_photos(undo=True)
        assert cam.store.hidden_photo_ids == {items[1].photo_id}

    asyncio.run(run())


@pytest.mark.parametrize("position, asset", [("first", "asset-0"), ("second", "asset-1")])
def test_paired_slide_targets_the_chosen_side(monkeypatch, position, asset):
    async def run():
        cam, _ = await _immich_camera(monkeypatch, 4, paired=True)
        with pytest.raises(ValueError, match="paired"):
            await cam.async_change_in_library("trash")
        await cam.async_change_in_library("trash", position=position)
        cam.coordinator.async_change_in_library.assert_awaited_once_with("trash", [asset])

    asyncio.run(run())


def test_refusal_from_immich_keeps_the_photo(monkeypatch):
    async def run():
        cam, items = await _immich_camera(monkeypatch)
        cam.coordinator.async_change_in_library.side_effect = aiohttp.ClientResponseError(
            None, (), status=403
        )
        with pytest.raises(ValueError, match="Immich refused"):
            await cam.async_change_in_library("archive")
        assert not cam.store.hidden_photo_ids
        assert cam._current_frame.meta["photo_ids"] == [items[0].photo_id]

    asyncio.run(run())


def test_other_sources_refuse_and_advertise_no_actions(monkeypatch):
    async def run():
        cam, _ = await _immich_camera(monkeypatch)
        assert cam.extra_state_attributes["library_actions"] == ["archive", "trash"]
        cam.coordinator.provider = "google_shared"
        assert cam.extra_state_attributes["library_actions"] == []
        with pytest.raises(ValueError, match="Immich"):
            await cam.async_change_in_library("archive")
        cam.coordinator.async_change_in_library.assert_not_awaited()

    asyncio.run(run())


def test_archive_and_trash_services_reach_the_camera():
    from homeassistant.exceptions import ServiceValidationError

    handlers = {}

    class Services:
        def has_service(self, domain, name):
            return name in handlers

        def async_register(self, domain, name, handler, **options):
            handlers[name] = (handler, options)

    cam = SimpleNamespace(async_change_in_library=AsyncMock())
    hass = SimpleNamespace(data={"album_slideshow": {"test": {"camera": cam}}}, services=Services())
    _register_photo_services(hass)

    async def call(name, **data):
        handler, options = handlers[name]
        return await handler(SimpleNamespace(data=options["schema"](data)))

    async def run():
        await call("archive_photo", entry_id="test", position="second", frame_id=4)
        cam.async_change_in_library.assert_awaited_with("archive", photo_ids=None, position="second", frame_id=4)
        await call("trash_photo", entry_id="test", photo_ids=["p"])
        cam.async_change_in_library.assert_awaited_with("trash", photo_ids=["p"], position=None, frame_id=None)
        cam.async_change_in_library.side_effect = ValueError("Immich refused")
        with pytest.raises(ServiceValidationError, match="Immich refused"):
            await call("trash_photo", entry_id="test")

    asyncio.run(run())


# ── how long a photo action takes ───────────────────────────────────────────


def test_library_action_records_how_long_the_server_took(monkeypatch):
    async def run():
        cam, _items = await _immich_camera(monkeypatch)
        assert cam.extra_state_attributes["last_photo_action"] is None
        await cam.async_change_in_library("archive")
        timing = cam.extra_state_attributes["last_photo_action"]
        assert timing["action"] == "archive"
        assert isinstance(timing["server_ms"], int) and timing["server_ms"] >= 0
        assert timing["at"]
        assert timing["answer_ms"] is None and timing["photo_ms"] is None

    asyncio.run(run())


def test_card_timing_joins_the_server_time_of_the_same_action(monkeypatch):
    async def run():
        cam, _items = await _immich_camera(monkeypatch)
        await cam.async_change_in_library("trash")
        server_ms = cam.extra_state_attributes["last_photo_action"]["server_ms"]
        cam.async_record_action_timing(
            "trash", answer_ms=1200, photo_ms=4100, load_ms=800, client="Mozilla/5.0 CrKey",
        )
        timing = cam.extra_state_attributes["last_photo_action"]
        assert timing["action"] == "trash"
        assert timing["server_ms"] == server_ms
        assert (timing["answer_ms"], timing["photo_ms"], timing["load_ms"]) == (1200, 4100, 800)
        assert timing["client"] == "Mozilla/5.0 CrKey"

    asyncio.run(run())


def test_card_timing_for_another_action_does_not_borrow_its_server_time(monkeypatch):
    async def run():
        cam, _items = await _immich_camera(monkeypatch)
        await cam.async_change_in_library("archive")
        cam.async_record_action_timing("trash", answer_ms=50, photo_ms=None, load_ms=None, client="x")
        timing = cam.extra_state_attributes["last_photo_action"]
        assert timing["action"] == "trash"
        assert timing["server_ms"] is None
        assert timing["photo_ms"] is None

    asyncio.run(run())


def test_action_timing_websocket_reaches_the_camera(monkeypatch):
    import sys
    import types

    from custom_components.album_slideshow import async_register_action_timing_websocket
    from custom_components.album_slideshow.const import WS_PHOTO_ACTION_TIMING

    registered = []
    stub = types.ModuleType("homeassistant.components.websocket_api")
    stub.ERR_NOT_FOUND = "not_found"
    stub.websocket_command = lambda schema: (lambda handler: setattr(handler, "schema", schema) or handler)
    stub.async_register_command = lambda hass, handler: registered.append(handler)
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", stub)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", stub, raising=False)
    cam = SimpleNamespace(async_record_action_timing=lambda *a, **k: calls.append((a, k)))
    calls = []
    hass = SimpleNamespace(data={"album_slideshow": {"album": {"camera": cam}}})
    async_register_action_timing_websocket(hass)
    (handler,) = registered
    assert WS_PHOTO_ACTION_TIMING in handler.schema.values()

    class Connection:
        def __init__(self):
            self.results, self.errors = [], []

        def send_result(self, msg_id, result=None):
            self.results.append(msg_id)

        def send_error(self, msg_id, code, message):
            self.errors.append((msg_id, code))

    connection = Connection()
    handler(hass, connection, {
        "id": 3, "type": WS_PHOTO_ACTION_TIMING, "entry_id": "album", "action": "archive",
        "answer_ms": 900, "photo_ms": 2500, "load_ms": 400, "client": "ua",
    })
    assert connection.results == [3]
    assert calls == [(("archive",), {"answer_ms": 900, "photo_ms": 2500, "load_ms": 400, "client": "ua"})]
    handler(hass, connection, {"id": 4, "type": WS_PHOTO_ACTION_TIMING, "entry_id": "missing", "action": "archive"})
    assert connection.errors == [(4, "not_found")]
