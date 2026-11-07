from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_fish_tts.config_store import SERVER_USER_ID, ConfigStore
from discord_fish_tts.notifier import ServerNotifier, classify_fish_error, credit_alert_needed

MORGAN = "76bb6ae7b26c41fbbd484514fdb014c2"
OTHER = "906b8b47c30d4ba2a7f1287773e8a3ed"


def test_server_voice_is_its_own_user_and_survives_default_changes(tmp_path: Path):
    path = tmp_path / "c.json"
    cfg = ConfigStore(path)
    cfg.set_voice_id(MORGAN)
    cfg.set_server_voice(MORGAN)
    cfg.set_voice_id(OTHER)  # owner changes the default later
    assert cfg.get_voice_for(SERVER_USER_ID) == MORGAN
    assert ConfigStore(path).get_server_voice() == MORGAN
    assert SERVER_USER_ID not in cfg.list_users()  # not a real Discord user


def test_credit_alert_only_for_paid_models_below_threshold():
    assert credit_alert_needed("s2.1-pro", 0.5, 1.0)
    assert not credit_alert_needed("s2.1-pro", 3.0, 1.0)
    assert not credit_alert_needed("s2.1-pro-free", 0.0, 1.0)
    assert not credit_alert_needed("s2.1-pro", None, 1.0)


def test_classify_fish_errors():
    assert classify_fish_error(Exception("server rejected WebSocket connection: HTTP 402")) == "payment"
    assert classify_fish_error(Exception("HTTP 401")) == "auth"
    assert classify_fish_error(Exception("Insufficient balance")) == "payment"
    assert classify_fish_error(TimeoutError("first_audio")) is None


def _notifier(model="s2.1-pro", credit=0.4):
    n = ServerNotifier(MagicMock(), MagicMock(), MagicMock(), lambda: 1, "k", model, credit_alert_usd=1.0)
    n.fetch_credit = AsyncMock(return_value=credit)
    n.alert = AsyncMock()
    return n


@pytest.mark.asyncio
async def test_low_credit_alerts_once_until_recovered():
    n = _notifier(credit=0.4)
    await n.check_credit_once()
    await n.check_credit_once()
    assert n.alert.await_count == 1
    n.fetch_credit.return_value = 5.0
    await n.check_credit_once()
    n.fetch_credit.return_value = 0.2
    await n.check_credit_once()
    assert n.alert.await_count == 2


@pytest.mark.asyncio
async def test_tts_error_dm_is_rate_limited():
    n = _notifier(model="s2.1-pro-free")
    n.dm_owner = AsyncMock()
    err = Exception("HTTP 402")
    n.on_tts_error(MagicMock(), err)
    n.on_tts_error(MagicMock(), err)
    n.on_tts_error(MagicMock(), TimeoutError("idle"))
    await asyncio.sleep(0)
    assert n.dm_owner.await_count == 1
    assert "free model may have ended" in n.dm_owner.await_args.args[0]
