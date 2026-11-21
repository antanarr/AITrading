from argparse import Namespace

import pytest

from copybot import meme_study


def score(**updates):
    return {
        "count": 25,
        "wins": 14,
        "profit_factor": 1.40,
        "net": 2.0,
        "net_without_best": 1.0,
        "net_without_best_coin": 0.8,
        "net_without_best_trade_and_coin": 0.2,
        "win_rate": 0.56,
        "intrabar_drawdown": 2.1,
        **updates,
    }


@pytest.mark.parametrize(
    "track,updates",
    (
        ("A", {"win_rate": 0.52}),
        ("A", {"profit_factor": 1.20}),
        ("A", {"count": 24}),
        ("B", {"profit_factor": 1.30}),
        ("B", {"count": 19}),
        ("B", {"intrabar_drawdown": 4.01}),
        ("A", {"net_without_best_trade_and_coin": -0.01}),
    ),
)
def test_track_rejects_failed_stress_criterion(track, updates):
    assert not meme_study.qualification(track, score(**updates))["pass"]


def test_momentum_can_qualify_with_fewer_winners_if_payoffs_cover_losses():
    assert meme_study.qualification("B", score(count=20, wins=8, win_rate=0.40))["pass"]


def test_combined_exclusion_does_not_subtract_best_trade_coin_twice():
    metrics = {
        "trades": [
            {"net": 3, "legs": [{"coin": "DOGE", "net": 1}, {"coin": "PENGU", "net": 2}]},
            {"net": 1.5, "legs": [{"coin": "DOGE", "net": 1.5}]},
            {"net": -0.5, "legs": [{"coin": "PENGU", "net": -0.5}]},
            {"net": 0.7, "legs": [{"coin": "WIF", "net": 0.7}]},
        ],
    }
    assert meme_study.net_without_both(metrics) == pytest.approx(0.2)


def test_unfinished_reserve_cannot_open_archive(monkeypatch, tmp_path):
    manifest = tmp_path / "rules.json"
    meme_study.write_json(manifest, {"code_digest": "test", "start": 24, "end": 48})
    monkeypatch.setattr(meme_study, "code_digest", lambda: "test")
    monkeypatch.setattr(meme_study, "current_hour", lambda: 47)

    def unexpected_load(*args, **kwargs):
        pytest.fail("unfinished Reserve attempted to load market data")

    monkeypatch.setattr(meme_study, "load_archive", unexpected_load)
    with pytest.raises(ValueError, match="has not closed"):
        meme_study.execute_reserve(
            Namespace(
                manifest=manifest, archive=tmp_path / "sealed", output=tmp_path / "result.json"
            )
        )


def test_reserve_receipt_prevents_a_second_pass(monkeypatch, tmp_path):
    manifest = tmp_path / "rules.json"
    archive = tmp_path / "sealed"
    archive.mkdir()
    meme_study.write_json(
        manifest,
        {
            "code_digest": "test",
            "start": 24,
            "end": 48,
            "training_archive": str(tmp_path / "train"),
            "taxonomy_digest": "labels",
            "candidates": [{"name": "test", "family": "test", "track": "B", "settings": []}],
            "minimum_turnover": 1_000_000,
            "limits": {},
        },
    )
    monkeypatch.setattr(meme_study, "code_digest", lambda: "test")
    monkeypatch.setattr(meme_study, "taxonomy_digest", lambda archive: "labels")
    monkeypatch.setattr(meme_study, "current_hour", lambda: 48)
    calls = []
    monkeypatch.setattr(meme_study, "study_period", lambda *args: calls.append(args) or {})
    args = Namespace(manifest=manifest, archive=archive, output=tmp_path / "result.json")
    meme_study.execute_reserve(args)
    with pytest.raises(FileExistsError):
        meme_study.execute_reserve(Namespace(**{**vars(args), "output": tmp_path / "other.json"}))
    assert len(calls) == 1
