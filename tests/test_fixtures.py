from backend.fixtures import all_fixtures, get_fixture


def test_fixtures_are_deterministic_and_complete():
    first = get_fixture("main")
    second = get_fixture("main")
    assert first.model_dump() == second.model_dump()
    assert len(first.technicians) == 4
    assert len(first.work_orders) == 24
    assert set(all_fixtures()) == {
        "main", "skill-shortage", "window-conflict", "emergency",
        "strategy-medium", "strategy-stress",
    }
    assert len(get_fixture("strategy-medium").technicians) == 8
    assert len(get_fixture("strategy-medium").work_orders) == 60
    assert len(get_fixture("strategy-stress").technicians) == 12
    assert len(get_fixture("strategy-stress").work_orders) == 100


def test_main_fixture_uses_only_offline_coordinates():
    scenario = get_fixture("main")
    for technician in scenario.technicians:
        assert 0 <= technician.start_location.x <= 100
        assert 0 <= technician.start_location.y <= 100
    for order in scenario.work_orders:
        assert 0 <= order.location.x <= 100
        assert 0 <= order.location.y <= 100
