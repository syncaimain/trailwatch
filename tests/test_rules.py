from datetime import datetime

from trailwatch import rules
from trailwatch.models import Conditions

NOW = datetime(2026, 10, 10, 14, 0)


def test_afternoon_five_means_seventeen():
    assert rules.hhmm_to_minutes_ahead("05:00", NOW) == 180
    assert rules.hhmm_to_minutes_ahead("17:30", NOW) == 210
    assert rules.hhmm_to_minutes_ahead("nonsense", NOW) is None


def test_model_cannot_lower_safety():
    raw = {"route": "Ridge", "solo": True, "risk": "low", "expected_return_local": "19:00", "checkin_every_min": 240}
    cond = Conditions(available=True, sunset_local="17:45", minutes_to_sunset=225, max_wind_kmh=45)
    p = rules.finalize_plan(raw, NOW, cond, "gemma")
    assert p.return_in_min == 300
    assert p.checkin_every_min == 45  # model asked for 240; solo caps at 60, high risk at 45
    assert p.risk == "high"  # after sunset (low->medium) + wind (medium->high)
    assert any("sunset" in w for w in p.warnings)


def test_contradictory_return_time_uses_the_earlier_one():
    # Real Gemma 3 4B output for "back in 40 minutes" at 23:00.
    late_night = datetime(2026, 10, 9, 23, 0)
    p = rules.finalize_plan({"expected_return_local": "12:39", "duration_min": 40, "turnaround_in_min": 1440},
                            late_night, Conditions(), "gemma")
    assert p.return_in_min == 40
    assert p.turnaround_in_min == 20
    assert any("ambiguous" in w for w in p.warnings)


def test_garbage_model_output_still_gives_a_plan():
    p = rules.finalize_plan({"risk": "yolo", "activity": "teleport"}, NOW, Conditions(), "gemma")
    assert p.return_in_min == rules.DEFAULT_TRIP_MIN
    assert p.risk == "medium" and p.activity == "other"
    assert p.warnings


def test_fallback_parses_common_phrasings():
    assert rules.fallback_plan("Doi Suthep solo, back by 5:30pm", NOW, Conditions()).return_in_min == 210
    assert rules.fallback_plan("back by 5", NOW, Conditions()).return_in_min == 180
    p = rules.fallback_plan("walk for 90 min alone", NOW, Conditions())
    assert p.return_in_min == 90 and p.solo and p.activity == "walk"
