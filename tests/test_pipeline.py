"""End-to-end: synthetic raw files -> processed tables, reports and dashboard bundle."""

import json

import pandas as pd

from dinesafe import cli
from dinesafe.pipeline import build


def load_bundle(path):
    text = path.read_text(encoding="utf-8")
    prefix = "window.DINESAFE_DATA="
    assert text.startswith(prefix)
    return json.loads(text[len(prefix):].rstrip().rstrip(";"))


def test_build_end_to_end(project):
    result = build(project)
    est = pd.read_csv(project.processed / "establishments.csv")
    assert len(est) == 240 and est["est_key"].is_unique
    assert est["p_serious"].between(0, 1).all() and est["p_any"].between(0, 1).all()
    assert set(est["risk_band"].dropna()) <= {"Low", "Average", "Elevated", "High"}
    # archive linkage gives types to the 200 legacy establishments; the rest get inferred/unknown
    assert (est["category_source"] == "archive").sum() == 200
    assert est["category"].notna().all()
    for name in ["inspections.csv", "infractions.csv"]:
        assert (project.processed / name).exists()
    for name in ["data_quality.json", "model_report.json", "model_card.md"]:
        assert (project.reports / name).exists()
    rep = result.model_report
    assert rep["y_serious"]["chosen_model"] in {"logistic", "gbm"}
    assert "roc_auc" in rep["y_serious"]["models"]["logistic"]
    assert result.data_report["historical_quote_repairs"] == {"dinesafe_hist_2021.csv": 2}

    b = load_bundle(project.dashboard_data / "dinesafe-data.js")
    n = len(b["est"]["rows"])
    assert n == 240 and len(b["feat"]["rows"]) == n and len(b["insp"]["count"]) == n
    assert sum(b["insp"]["count"]) == len(b["insp"]["dday"]) == len(b["insp"]["flags"])
    assert sum(f >> 5 for f in b["insp"]["flags"]) == len(b["inf"]["text"]) == len(b["inf"]["code"])
    # dates decode to the right range
    cols = {c: i for i, c in enumerate(b["est"]["cols"])}
    first = b["est"]["rows"][0]
    k0 = 0
    d = 0
    for j in range(b["insp"]["count"][0]):
        d = b["insp"]["dday"][k0 + j] if j == 0 else d + b["insp"]["dday"][k0 + j]
    assert d == first[cols["lastDate"]]


def test_cli_build_and_errors(project, capsys):
    assert cli.main(["--root", str(project.root), "build", "--no-eval"]) == 0
    assert "Built 240 establishments" in capsys.readouterr().out
    # a missing snapshot is a clean error, not a traceback
    for p in project.raw.glob("dinesafe_*.csv"):
        p.unlink()
    assert cli.main(["--root", str(project.root), "build"]) == 1
