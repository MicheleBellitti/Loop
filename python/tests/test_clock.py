from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loop.domain.clock import (
    at_local_time,
    is_quiet_hour,
    next_deliverable_at,
    parse_quiet_hours,
)

ROME = "Europe/Rome"
Q = parse_quiet_hours("21:00-08:00")


def rome(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(ROME))


class TestQuietHours:
    def test_covers_the_window_that_wraps_past_midnight(self) -> None:
        assert is_quiet_hour(rome(2026, 7, 30, 22), ROME, Q)
        assert is_quiet_hour(rome(2026, 7, 31, 3), ROME, Q)
        assert is_quiet_hour(rome(2026, 7, 31, 7, 59), ROME, Q)
        assert not is_quiet_hour(rome(2026, 7, 31, 8), ROME, Q)
        assert not is_quiet_hour(rome(2026, 7, 30, 18), ROME, Q)
        assert not is_quiet_hour(rome(2026, 7, 30, 20, 59), ROME, Q)

    def test_defers_a_late_night_notification_to_eight_the_next_morning(self) -> None:
        when = next_deliverable_at(rome(2026, 7, 30, 23, 30), ROME, Q)
        local = when.astimezone(ZoneInfo(ROME))
        assert (local.day, local.hour, local.minute) == (31, 8, 0)

    def test_defers_an_early_notification_to_eight_the_same_morning(self) -> None:
        when = next_deliverable_at(rome(2026, 7, 31, 3), ROME, Q)
        local = when.astimezone(ZoneInfo(ROME))
        assert (local.day, local.hour) == (31, 8)

    def test_passes_straight_through_outside_the_window(self) -> None:
        noon = rome(2026, 7, 30, 12)
        assert next_deliverable_at(noon, ROME, Q) == noon


class TestTheDailySlot:
    def test_lands_on_the_local_hour_whatever_the_server_timezone(self) -> None:
        now = datetime(2026, 7, 30, 4, 12, tzinfo=ZoneInfo("UTC"))
        assert at_local_time(now, ROME, "18:00").astimezone(ZoneInfo(ROME)).hour == 18
        tokyo = at_local_time(now, "Asia/Tokyo", "18:00")
        assert tokyo.astimezone(ZoneInfo("Asia/Tokyo")).hour == 18


class TestDaylightSaving:
    def test_the_dormancy_hour_survives_the_autumn_fold(self) -> None:
        # Clocks go back on 25 October 2026 at 03:00 local.
        d = at_local_time(rome(2026, 10, 25, 12), ROME, "03:00")
        assert d.astimezone(ZoneInfo(ROME)).hour == 3

    def test_a_nonexistent_spring_hour_resolves_just_after_the_jump(self) -> None:
        # 29 March 2026: 02:00–03:00 local does not exist.
        d = at_local_time(rome(2026, 3, 29, 12), ROME, "02:30")
        assert d.astimezone(ZoneInfo(ROME)).hour == 3

    def test_summer_and_winter_offsets_differ(self) -> None:
        summer = at_local_time(rome(2026, 7, 30, 12), ROME, "03:00")
        winter = at_local_time(rome(2026, 1, 30, 12), ROME, "03:00")
        assert summer.astimezone(ZoneInfo("UTC")).hour == 1  # CEST, +2
        assert winter.astimezone(ZoneInfo("UTC")).hour == 2  # CET, +1
