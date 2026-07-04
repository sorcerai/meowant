from mw.imgutil import spread_sample


def test_spread_sample_includes_first_and_last():
    out = spread_sample(range(44), 5)
    assert out[0] == 0
    assert out[-1] == 43
    assert len(out) == 5


def test_spread_sample_returns_all_when_fewer_items_than_n():
    assert spread_sample([1, 2, 3], 5) == [1, 2, 3]


def test_spread_sample_exact_fit():
    assert spread_sample([1, 2, 3], 3) == [1, 2, 3]


def test_spread_sample_n_one_returns_first():
    assert spread_sample([1, 2, 3, 4], 1) == [1]


def test_spread_sample_zero_n_returns_empty():
    assert spread_sample([1, 2, 3], 0) == []


def test_spread_sample_empty_items():
    assert spread_sample([], 5) == []


def test_spread_sample_never_skips_tail_across_sizes():
    # regression for the int(i*len/n) bug: for every list length, the last
    # element must be selectable — this never held under the old formula.
    for length in range(2, 40):
        items = list(range(length))
        for n in range(2, min(8, length) + 1):
            out = spread_sample(items, n)
            assert out[-1] == length - 1, (length, n, out)
            assert out[0] == 0
