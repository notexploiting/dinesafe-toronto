import io

import pandas as pd

from dinesafe import clean
from conftest import make_historical, write_historical


def s(*values):
    return pd.Series(list(values), dtype="string")


def test_normalize_text_turns_placeholders_into_na():
    out = clean.normalize_text(s("  Foo   bar ", "None", "", "undefined", "a\nb"))
    assert out.tolist()[0] == "Foo bar"
    assert out.isna().tolist() == [False, True, True, True, False]
    assert out.iloc[4] == "a b"


def test_parse_address_variants():
    out = clean.parse_address(s(
        "1871 O'Connor Dr None M4A 1X1",
        "1085 Bellamy Rd N 12 M1H 3C7",
        "Victoria Park None None",
        "2641 LAWRENCE AVE E None M1P 22",      # truncated postal code
        "3280 DUFFERIN ST, Unit-0 None M6A 2T5",  # legacy-style unit suffix
        "1055 The Queensway undefined None M8Z 0G1",
        "511 Rogers Rd Bldg-1 M6M 1B4",
    ))
    assert out["street"].tolist() == ["1871 O'Connor Dr", "1085 Bellamy Rd N", "Victoria Park", "2641 LAWRENCE AVE E",
                                      "3280 DUFFERIN ST", "1055 The Queensway", "511 Rogers Rd"]
    assert out["postal_code"].tolist()[:2] == ["M4A 1X1", "M1H 3C7"]
    assert out["postal_code"].isna().tolist()[2:4] == [True, True]
    assert out["unit"].tolist()[1] == "12"
    assert out["unit"].tolist()[6] == "Bldg 1"
    assert out["unit"].isna().iloc[0]


def test_format_address_title_cases_legacy_uppercase():
    out = clean.format_address(s("291 SILVERTHORN AVE", "1871 O'Connor Dr"), s(None, "Unit 2"), s("M6N 3K2", None))
    assert out.tolist() == ["291 Silverthorn Ave, Toronto ON M6N 3K2", "1871 O'Connor Dr, Unit 2, Toronto ON"]


def test_clean_phone():
    out = clean.clean_phone(s("4167522786", "0000000000", "4160000000", "1-416-752-2786", "None", "12345"))
    assert out.tolist()[0] == "(416) 752-2786"
    assert out.tolist()[3] == "(416) 752-2786"
    assert out.isna().tolist() == [False, True, True, False, True, True]


def test_severity_and_outcome_normalization():
    assert clean.normalize_severity(s("M - Minor", "S - Significant", "C - Crucial", "NA", "NA - Not Applicable",
                                      "None")).tolist()[:5] == ["Minor", "Significant", "Crucial", "Not Applicable",
                                                                "Not Applicable"]
    assert clean.normalize_outcome(s("Conviction: Fined", "Conviction - Fined", "Pending")).tolist() == [
        "Conviction - Fined", "Conviction - Fined", "Pending"]


def test_deficiency_code_handles_both_coding_schemes():
    out = clean.deficiency_code(s("5C. Proper maintenance", "05. MAINTENANCE / SANITATION", "08. PEST CONTROL",
                                  "A. OPERATOR FAIL TO MEET", "ADMINISTRATIVE REQUIREMENTS", "None"))
    assert out.tolist()[:5] == ["5", "5", "8", "A", "A"]
    assert out.isna().iloc[5]


def test_canonical_text_merges_case_and_space_variants():
    out = clean.canonical_text(s("Fail to protect  ", "FAIL TO PROTECT", "Fail to protect", "other"))
    assert out.tolist() == ["Fail to protect"] * 3 + ["other"]


def test_repair_csv_text_fixes_only_the_known_defects():
    # The real 2020-2022 files: the whole file wrapped in one extra pair of quotes, so the
    # header starts with "" and the last line (whose last field is empty) ends with """.
    broken = '""a","b"\n"1","2"\n"3","""\n'
    fixed, n = clean.repair_csv_text(broken)
    assert n == 2
    df = pd.read_csv(io.StringIO(fixed), dtype=str, keep_default_na=False)
    assert df.columns.tolist() == ["a", "b"] and df.values.tolist() == [["1", "2"], ["3", ""]]
    ok = '"a","b"\n"1","2"\n'
    assert clean.repair_csv_text(ok) == (ok, 0)


def test_read_historical_repairs_and_parses_mixed_dates(tmp_path):
    write_historical(tmp_path, make_historical(n_est=10), break_quotes_year=2021)
    raw, repairs = clean.read_historical(sorted(tmp_path.glob("*.csv")))
    assert repairs == {"dinesafe_hist_2021.csv": 2}
    assert len(raw) == 30
    dates = clean.parse_date(raw["Inspection Date"])
    assert dates.notna().all()
    assert sorted(dates.dt.year.unique()) == [2019, 2021, 2023]


def test_clean_current_merges_legacy_and_new_duplicates(current_df):
    records, est, report = clean.clean_current(current_df.astype("string"))
    # every establishment ends up exactly once, keyed by its legacy ID where it has one
    assert est["est_key"].is_unique
    assert len(est) == 240
    assert report["current_establishments_with_legacy_and_new_ids"] == 60
    assert report["current_duplicate_inspections_removed"] > 0
    # no (establishment, date) inspection is listed under two source IDs any more
    assert not records.duplicated(["est_key", "inspection_date", "infraction", "severity"]).any()
    dup = est[est["est_key"] == "10000000"].iloc[0]
    assert dup["new_id"] == "001SYN000000AA" and bool(dup["in_new_system"])
    assert dup["address"] == "100 Queen St W, Toronto ON M5V 1J4"  # new-system spelling preferred


def test_build_tables_inspection_grain_and_followups(current_df):
    t = clean.build_tables(current_df.astype("string"))
    ins = t.inspections
    assert not ins.duplicated(["est_key", "inspection_date"]).any()
    # follow-ups are exactly the visits within 30 days after a Conditional Pass/Closed
    fu = ins[ins["is_followup"]]
    assert len(fu) > 0 and (fu["days_since_previous"] <= 30).all()
    # infraction counts agree between the two tables
    per = t.infractions.groupby(["est_key", "inspection_date"]).size()
    merged = ins.set_index(["est_key", "inspection_date"])["n_infractions"]
    assert (merged[merged > 0] == per.reindex(merged[merged > 0].index)).all()
    assert (t.infractions["category"] != "Other").all()
    # the pest infraction is filed under premises maintenance by the City but shown as pest control
    pest = t.infractions[t.infractions["infraction"].str.contains("pests")]
    assert (pest["category"] == "Pest control").all()
