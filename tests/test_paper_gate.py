from paper.check_data_gate import agreement


def test_identical():
    assert agreement({1, 2}, {1, 2}) == 1.0


def test_partial():
    assert agreement({1, 2, 3}, {2, 3, 4}) == 2 / 4


def test_disjoint():
    assert agreement({1}, {2}) == 0.0


def test_both_empty():
    assert agreement(set(), set()) == 1.0


def test_one_empty():
    assert agreement({1}, set()) == 0.0


from paper.check_data_gate import gate_verdict

_OK = {"a": (1.0, 10), "b": (0.995, 3), "c": (1.0, 50)}


def test_verdict_zero_common_fails():
    ok, fails = gate_verdict(100, 0, {k: (1.0, 0) for k in _OK})
    assert not ok and fails


def test_verdict_89pct_common_fails():
    ok, fails = gate_verdict(100, 89, _OK)
    assert not ok and any("common" in f for f in fails)


def test_verdict_95pct_passes():
    ok, fails = gate_verdict(100, 95, _OK)
    assert ok and not fails


def test_verdict_strategy_without_signals_fails():
    ok, fails = gate_verdict(100, 95, {**_OK, "c": (1.0, 0)})
    assert not ok and any("no signals" in f for f in fails)
