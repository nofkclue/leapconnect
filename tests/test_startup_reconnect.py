"""Startup retries recover from an unavailable VPN/cloud without a restart."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from leapconnect.container import AppContainer


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    cert = tmp_path / "app.crt"
    key = tmp_path / "app.key"
    cert.touch()
    key.touch()
    account = {
        "username": "driver@example.test",
        "password": "test-password",
        "cert_path": str(cert),
        "key_path": str(key),
    }
    app = AppContainer()
    app.live_refresh_interval = 0
    app.repo = SimpleNamespace(
        get_account=AsyncMock(return_value=account),
        get_setting=AsyncMock(return_value="1234"),
        close=AsyncMock(),
    )
    app.scheduler = Mock(stop=AsyncMock())
    sync = Mock()
    cloud = SimpleNamespace(
        login=AsyncMock(),
        get_vehicle_list=AsyncMock(return_value=[Mock(vin="TEST-VIN")]),
    )
    monkeypatch.setattr(
        "leapconnect.container.LeapmotorApiClient", Mock(return_value=sync)
    )
    monkeypatch.setattr(
        "leapconnect.container.AsyncLeapmotorApiClient", Mock(return_value=cloud)
    )
    return app, cloud, sync


def fast_retries(monkeypatch):
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("leapconnect.container.asyncio.sleep", sleep)
    return delays


async def test_startup_returns_offline_then_recovers_and_starts_recording(
    runtime, monkeypatch
):
    app, cloud, sync = runtime
    cloud.login.side_effect = [OSError("VPN not ready"), None]
    delays = fast_retries(monkeypatch)

    await app.auto_connect()

    assert not app.connected
    app.scheduler.start.assert_not_called()
    task = app._auto_connect_task
    assert task is not None
    await task

    assert delays == [5]
    assert app.connected
    app.scheduler.set_client.assert_called_with(app.client, app.vehicles)
    app.scheduler.start.assert_called_once()
    assert sync.operation_password == "1234"
    assert app._auto_connect_task is None


async def test_backoff_grows_and_stays_capped_at_five_minutes(runtime, monkeypatch):
    app, cloud, _ = runtime
    cloud.login.side_effect = [OSError("offline")] * 9 + [None]
    delays = fast_retries(monkeypatch)

    await app.auto_connect()
    await app._auto_connect_task

    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert app.connected
    assert cloud.login.await_count == 10


async def test_successful_startup_does_not_schedule_retries(runtime):
    app, cloud, _ = runtime

    await app.auto_connect()

    assert app.connected
    cloud.login.assert_awaited_once()
    assert app._auto_connect_task is None


@pytest.mark.parametrize("missing", ["repo", "account", "cert", "key"])
async def test_incomplete_setup_does_not_schedule_retries(runtime, missing):
    app, cloud, _ = runtime
    if missing == "repo":
        app.repo = None
    elif missing == "account":
        app.repo.get_account.return_value = None
    else:
        app.repo.get_account.return_value[f"{missing}_path"] = "nonexistent-file"

    await app.auto_connect()

    cloud.login.assert_not_awaited()
    assert app._auto_connect_task is None


async def test_pin_restore_failure_does_not_repeat_successful_login(runtime):
    app, cloud, _ = runtime
    app.repo.get_setting.side_effect = OSError("PIN read unavailable")

    await app.auto_connect()

    assert app.connected
    cloud.login.assert_awaited_once()
    assert app._auto_connect_task is None


async def test_retry_reloads_saved_account(runtime, monkeypatch):
    app, cloud, _ = runtime
    cloud.login.side_effect = [OSError("offline"), None]
    fast_retries(monkeypatch)
    await app.auto_connect()
    app.repo.get_account.return_value["password"] = "updated-password"

    await app._auto_connect_task

    from leapconnect.container import LeapmotorApiClient

    assert LeapmotorApiClient.call_args.kwargs["password"] == "updated-password"
    assert app.repo.get_account.await_count == 2


async def test_removed_account_ends_pending_retries(runtime, monkeypatch):
    app, cloud, _ = runtime
    cloud.login.side_effect = OSError("offline")
    fast_retries(monkeypatch)
    await app.auto_connect()
    app.repo.get_account.return_value = None

    await app._auto_connect_task

    cloud.login.assert_awaited_once()
    assert app._auto_connect_task is None


async def test_disconnect_cancels_backoff_without_reconnecting(runtime):
    app, cloud, _ = runtime
    cloud.login.side_effect = OSError("offline")
    await app.auto_connect()
    task = app._auto_connect_task
    # Let the retry task enter its real five-second backoff.
    await asyncio.sleep(0)

    app.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not app.connected
    assert app._auto_connect_task is None
    cloud.login.assert_awaited_once()


async def test_manual_connect_takes_over_from_pending_retry(runtime):
    app, cloud, _ = runtime
    cloud.login.side_effect = [OSError("offline"), None]
    await app.auto_connect()
    task = app._auto_connect_task
    await asyncio.sleep(0)

    await app.connect(**app.repo.get_account.return_value)

    assert task.cancelled()
    assert app.connected
    assert cloud.login.await_count == 2
    assert app._auto_connect_task is None


async def test_manual_connect_waits_for_cancelled_inflight_retry(runtime, monkeypatch):
    app, cloud, sync = runtime
    retry_started = asyncio.Event()
    retry_cleaned_up = asyncio.Event()
    never = asyncio.Event()

    async def login():
        attempt = cloud.login.await_count
        if attempt == 1:
            raise OSError("offline")
        if attempt == 2:
            retry_started.set()
            try:
                await never.wait()
            finally:
                retry_cleaned_up.set()
        else:
            assert retry_cleaned_up.is_set()

    cloud.login.side_effect = login
    fast_retries(monkeypatch)
    await app.auto_connect()
    task = app._auto_connect_task
    await asyncio.wait_for(retry_started.wait(), timeout=1)

    # A disconnect followed immediately by manual connect must not let the
    # cancelled retry reset the new connection's state afterwards.
    app.disconnect()
    await app.connect(**app.repo.get_account.return_value)

    assert task.cancelled()
    assert app.connected
    assert app.client is not None
    assert cloud.login.await_count == 3
    assert sync.close.call_count >= 2
    assert app._auto_connect_task is None


async def test_shutdown_cancels_retry_before_closing_database(runtime, monkeypatch):
    app, cloud, sync = runtime
    retry_started = asyncio.Event()
    retry_cancelled = asyncio.Event()
    never = asyncio.Event()

    async def login():
        if cloud.login.await_count == 1:
            raise OSError("offline")
        retry_started.set()
        try:
            await never.wait()
        finally:
            retry_cancelled.set()

    async def close_repo():
        assert retry_cancelled.is_set()

    cloud.login.side_effect = login
    app.repo.close.side_effect = close_repo
    fast_retries(monkeypatch)
    await app.auto_connect()
    task = app._auto_connect_task
    await asyncio.wait_for(retry_started.wait(), timeout=1)

    await app.shutdown()

    assert task.cancelled()
    assert not app.connected
    assert app._auto_connect_task is None
    app.repo.close.assert_awaited_once()
    sync.close.assert_called()


async def test_vehicle_list_failure_is_retried_and_failed_client_closed(
    runtime, monkeypatch
):
    app, cloud, sync = runtime
    cloud.get_vehicle_list.side_effect = [OSError("cloud unavailable"), []]
    fast_retries(monkeypatch)

    await app.auto_connect()
    sync.close.assert_called_once()
    assert app.sync_client is None
    await app._auto_connect_task

    assert app.connected
    assert cloud.login.await_count == 2
    assert cloud.get_vehicle_list.await_count == 2


async def test_restarting_auto_connect_replaces_pending_retry(runtime):
    app, cloud, _ = runtime
    cloud.login.side_effect = OSError("offline")
    await app.auto_connect()
    first = app._auto_connect_task

    await app.auto_connect()
    second = app._auto_connect_task

    assert first.cancelled()
    assert second is not first
    assert not second.done()
    await app._stop_auto_connect()


async def test_existing_connection_is_not_replaced_by_auto_connect(runtime):
    app, cloud, _ = runtime
    app.connected = True

    await app.auto_connect()

    cloud.login.assert_not_awaited()
    assert app._auto_connect_task is None
