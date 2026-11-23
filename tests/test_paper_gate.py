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
