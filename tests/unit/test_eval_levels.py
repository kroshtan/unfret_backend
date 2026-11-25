from evals.corpus import LabelRecord
from evals.levels import agreement, predict_from_thresholds, thresholds


def _r(i: int, label: str | None = None, level: int | None = None) -> LabelRecord:
    return LabelRecord(id=str(i), label=label, level=level, source="live", origin_dataset="d")  # type: ignore[arg-type]


def test_each_threshold_trains_only_on_pages_whose_labels_settle_it() -> None:
    records = [_r(0, "scam"), _r(1, "junk"), _r(2, "legit"), _r(3, "junk", level=1), _r(4), _r(5, "legit", level=0)]
    got = {t.k: dict(zip([r.id for r in t.records], t.y, strict=True)) for t in thresholds(records)}
    assert got[1] == {"0": 1, "1": 1, "3": 1, "5": 0}  # any concern: legit alone doesn't settle it
    assert got[2] == {"0": 1, "3": 0, "5": 0}  # at least overpaying: junk may be manipulation only
    assert got[3] == {"0": 1, "2": 0, "3": 0, "5": 0}  # at least doesn't work: junk alone doesn't settle it
    assert got[4] == {"0": 1, "1": 0, "2": 0, "3": 0, "5": 0}  # won't arrive


def test_the_level_is_the_highest_threshold_passed_after_making_them_monotone() -> None:
    assert predict_from_thresholds([0.9, 0.8, 0.3, 0.6]) == 2  # a later threshold can't pass when an earlier failed
    assert predict_from_thresholds([0.2, 0.9, 0.9, 0.9]) == 0
    assert predict_from_thresholds([0.9, 0.9, 0.9, 0.9]) == 4


def test_agreement_counts_exact_near_and_any_concern() -> None:
    report = agreement([(1, 1), (2, 3), (0, 4), (3, 0)])
    assert (report["exact"], report["within_one"], report["any_concern"]) == (0.25, 0.5, 0.5)
    assert report["confusion"]["0"] == {"4": 1}
