import numpy as np
import pandas as pd

from copybot import directional_transfer_study as study


def test_pretraining_only_reads_completed_earlier_labels(monkeypatch):
    older = pd.DataFrame(
        {
            "hour": [12, 24, 36],
            "price_return_12": [-0.04, 0.06, 0.50],
            "exit_hour_12": [25, 37, 49],
            "tech_return": [0.01, -0.03, 0.50],
            "market_breadth": [0.50, 0.35, 0.90],
        }
    )
    current = older.iloc[[2]].copy()
    current.index = [821]

    def capture(train, test, columns, horizon, *, gross_only):
        assert horizon == 12 and gross_only
        assert train.hour.tolist() == [12, 24]
        assert test.hour.tolist() == [36]
        assert list(columns) == ["tech_return"]
        return {key: np.array([0.4]) for key in study.PAST}

    monkeypatch.setattr(study, "models", capture)
    prediction, samples = study.prior_predictions(older, current, 49, 12)
    assert samples == 2
    assert prediction.loc[821, "boost"] == 0.4
