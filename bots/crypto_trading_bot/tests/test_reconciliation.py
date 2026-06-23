"""
Reconciliation tests — uses mocks so no live exchange connection needed.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from common.models import Position, TradeSlot, Order, EntryLeg


def _make_position(exchange='binance', symbol='BTC/USDT', side='long',
                   qty=0.1, slot_id=None) -> Position:
    return Position(
        exchange=exchange, symbol=symbol, side=side,
        entry_price=50000, current_price=50000,
        qty=qty, slot_id=slot_id,
    )


def _make_slot(exchange='binance', symbol='BTC/USDT', side='long',
               status='active', with_position=False) -> TradeSlot:
    slot = TradeSlot(
        exchange=exchange, symbol=symbol, side=side, status=status,
        entries=[EntryLeg(price=50000, qty=0.1)],
    )
    if with_position:
        slot.position = _make_position(
            exchange=exchange, symbol=symbol, slot_id=slot.id
        )
    return slot


class TestReconciliation:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_mismatches(self):
        from trading_engine.reconciliation import reconcile

        slot    = _make_slot(with_position=True)
        adapter = MagicMock()
        adapter.get_positions   = AsyncMock(return_value=[
            _make_position(slot_id=slot.id)
        ])
        adapter.get_open_orders = AsyncMock(return_value=[])

        slot_mgr = MagicMock()
        slot_mgr.get_active_slots = MagicMock(return_value=[slot])
        slot_mgr.get_all_slots    = MagicMock(return_value=[slot])

        state_pub = MagicMock()
        state_pub.log = MagicMock()

        with patch('trading_engine.reconciliation._save_snapshot'):
            self._run(reconcile({'binance': adapter}, slot_mgr, state_pub))

        # No warning level logs expected
        for call in state_pub.log.call_args_list:
            assert call.kwargs.get('level') != 'warning', \
                f"Unexpected warning: {call}"

    def test_untracked_live_position(self):
        from trading_engine.reconciliation import reconcile

        # Exchange has a position, but local has no active slots
        adapter = MagicMock()
        adapter.get_positions   = AsyncMock(return_value=[
            _make_position(symbol='ETH/USDT')
        ])
        adapter.get_open_orders = AsyncMock(return_value=[])

        slot_mgr = MagicMock()
        slot_mgr.get_active_slots = MagicMock(return_value=[])
        slot_mgr.get_all_slots    = MagicMock(return_value=[])

        state_pub = MagicMock()
        state_pub.log = MagicMock()

        with patch('trading_engine.reconciliation._save_snapshot'):
            self._run(reconcile({'binance': adapter}, slot_mgr, state_pub))

        warning_logs = [
            call for call in state_pub.log.call_args_list
            if call.kwargs.get('level') == 'warning'
        ]
        assert len(warning_logs) >= 1

    def test_local_position_missing_from_exchange(self):
        from trading_engine.reconciliation import reconcile

        slot    = _make_slot(with_position=True)
        adapter = MagicMock()
        adapter.get_positions   = AsyncMock(return_value=[])   # empty — exchange sees nothing
        adapter.get_open_orders = AsyncMock(return_value=[])

        slot_mgr = MagicMock()
        slot_mgr.get_active_slots = MagicMock(return_value=[slot])
        slot_mgr.get_all_slots    = MagicMock(return_value=[slot])

        state_pub = MagicMock()
        state_pub.log = MagicMock()

        with patch('trading_engine.reconciliation._save_snapshot'):
            self._run(reconcile({'binance': adapter}, slot_mgr, state_pub))

        warning_logs = [
            call for call in state_pub.log.call_args_list
            if call.kwargs.get('level') == 'warning'
        ]
        assert len(warning_logs) >= 1

    def test_adapter_failure_does_not_crash(self):
        from trading_engine.reconciliation import reconcile

        adapter = MagicMock()
        adapter.get_positions   = AsyncMock(side_effect=Exception("network error"))
        adapter.get_open_orders = AsyncMock(return_value=[])

        slot_mgr = MagicMock()
        slot_mgr.get_active_slots = MagicMock(return_value=[])
        slot_mgr.get_all_slots    = MagicMock(return_value=[])

        state_pub = MagicMock()
        state_pub.log = MagicMock()

        with patch('trading_engine.reconciliation._save_snapshot'):
            # Should not raise
            self._run(reconcile({'binance': adapter}, slot_mgr, state_pub))
