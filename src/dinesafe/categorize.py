"""Establishment categories.

The current DineSafe feed no longer says what *kind* of establishment something is. The
historical archive does ("Restaurant", "Food Take Out", "Child Care - Catered", ... 58
types), so for establishments that existed before 2024 we take the type from there and
group it into a handful of categories that are useful for filtering a map.

Establishments that opened since then have no archive record. For those, a text
classifier trained on archive (name -> category) pairs predicts the category from the
name alone; predictions below a confidence threshold are left as "Unknown". Inferred
categories are always flagged as such in the outputs.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

UNKNOWN = "Unknown"

CATEGORY_OF_TYPE = {
    # Restaurants and take-out
    "Restaurant": "Restaurant & take-out",
    "Food Take Out": "Restaurant & take-out",
    "Food Court Vendor": "Restaurant & take-out",
    "Cafeteria - Public Access": "Restaurant & take-out",
    "Hot Dog Cart": "Restaurant & take-out",
    "Food Cart": "Restaurant & take-out",
    "Mobile Food Preparation Premises": "Restaurant & take-out",
    "Refreshment Stand (Stationary)": "Restaurant & take-out",
    "Ice Cream / Yogurt Vendors": "Restaurant & take-out",
    # Bars and clubs
    "Cocktail Bar / Beverage Room": "Bar & club",
    "Private Club": "Bar & club",
    "Chartered Cruise Boats": "Bar & club",
    "Brew Your Own Beer / Wine": "Bar & club",
    # Bakeries
    "Bakery": "Bakery",
    "Bake Shop": "Bakery",
    # Grocery and retail food
    "Food Store (Convenience/Variety)": "Grocery & retail",
    "Supermarket": "Grocery & retail",
    "Butcher Shop": "Grocery & retail",
    "Fish Shop": "Grocery & retail",
    "Food Depot": "Grocery & retail",
    "Flea Market": "Grocery & retail",
    "Farmers` Market Vendor": "Grocery & retail",
    "Food Vending Facility": "Grocery & retail",
    "Locker Plant": "Grocery & retail",
    # Catering and banquet
    "Food Caterer": "Catering & banquet",
    "Banquet Facility": "Catering & banquet",
    "Church Banquet Facility": "Catering & banquet",
    "Commissary": "Catering & banquet",
    "Catering Vehicle": "Catering & banquet",
    "Centralized Kitchen": "Catering & banquet",
    "Serving Kitchen": "Catering & banquet",
    "Fairs / Festivals / Special Occasions": "Catering & banquet",
    # Institutions (schools, child care, health care, community)
    "Child Care - Catered": "Institutional",
    "Child Care - Food Preparation": "Institutional",
    "Student Nutrition Site": "Institutional",
    "Secondary School Food Services": "Institutional",
    "Elementary School Food Services": "Institutional",
    "College / University Food Services": "Institutional",
    "Other Educational Facility Food Services": "Institutional",
    "Nursing Home / Home for the Aged": "Institutional",
    "Retirement Homes(Licensed)": "Institutional",
    "Retirement Homes(Un-licensed)": "Institutional",
    "Rest Home": "Institutional",
    "Hospitals & Health Facilities": "Institutional",
    "Institutional Food Services": "Institutional",
    "Boarding / Lodging Home - Kitchen": "Institutional",
    "Cafeteria - Private Access": "Institutional",
    "Community Kitchen (Meal Program)": "Institutional",
    "Food Bank": "Institutional",
    "Bed & Breakfast": "Institutional",
    # Manufacturing
    "Food Processing Plant": "Food processing",
    "Meat Processing Plant": "Food processing",
    "Milk Pasteurization Plant": "Food processing",
    "Milk Products Plant": "Food processing",
    "Cheese Plant": "Food processing",
    "Ice Cream Plant": "Food processing",
    "Bottling Plant": "Food processing",
    "Ice Manufacturing Plant": "Food processing",
}

CATEGORIES = [
    "Restaurant & take-out", "Bar & club", "Bakery", "Grocery & retail",
    "Catering & banquet", "Institutional", "Food processing",
]


def category_of_type(types: pd.Series) -> pd.Series:
    """Map archive establishment types to categories (case-insensitive; unknown -> NA)."""
    lookup = {k.casefold(): v for k, v in CATEGORY_OF_TYPE.items()}
    return types.astype("string").str.strip().str.casefold().map(lookup).astype("string")


def normalize_name(names: pd.Series) -> pd.Series:
    """Upper-case, strip punctuation and accents, collapse spaces - used for matching names."""
    s = names.astype("string").fillna("")
    s = s.map(lambda x: x.encode("ascii", "ignore").decode() if isinstance(x, str) else "")
    s = s.str.upper().str.replace(r"&", " AND ", regex=False)
    s = s.str.replace(r"[^A-Z0-9 ]+", " ", regex=True).str.replace(r"\s+", " ", regex=True).str.strip()
    return s.astype("string")


class NameCategoryClassifier:
    """TF-IDF (word + character n-grams) + multinomial logistic regression."""

    def __init__(self, min_confidence: float = 0.6, random_state: int = 0) -> None:
        self.min_confidence = min_confidence
        self.random_state = random_state
        self.pipeline = None
        self.metrics: dict = {}

    def _make(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline, make_union

        features = make_union(
            TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=2, sublinear_tf=True),
            TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=3, sublinear_tf=True),
        )
        clf = LogisticRegression(C=4.0, max_iter=2000, class_weight=None)
        return make_pipeline(features, clf)

    def evaluate(self, names: pd.Series, labels: pd.Series, n_splits: int = 3) -> dict:
        """Grouped cross-validation: all establishments sharing a name stay in one fold, so
        chains (e.g. every "TIM HORTONS") can't leak between training and test."""
        from sklearn.model_selection import GroupKFold

        x = normalize_name(names).to_numpy(dtype=object)
        y = labels.to_numpy(dtype=object)
        proba_max = np.zeros(len(y))
        pred = np.empty(len(y), dtype=object)
        for train, test in GroupKFold(n_splits=n_splits).split(x, y, groups=x):
            model = self._make().fit(x[train], y[train])
            p = model.predict_proba(x[test])
            proba_max[test] = p.max(axis=1)
            pred[test] = model.classes_[p.argmax(axis=1)]
        correct = pred == y
        covered = proba_max >= self.min_confidence
        self.metrics = {
            "n_training_names": int(len(y)),
            "accuracy_all": round(float(correct.mean()), 4),
            "coverage_at_threshold": round(float(covered.mean()), 4),
            "accuracy_at_threshold": round(float(correct[covered].mean()), 4) if covered.any() else None,
            "min_confidence": self.min_confidence,
        }
        return self.metrics

    def fit(self, names: pd.Series, labels: pd.Series) -> "NameCategoryClassifier":
        x = normalize_name(names).to_numpy(dtype=object)
        self.pipeline = self._make().fit(x, labels.to_numpy(dtype=object))
        return self

    def predict(self, names: pd.Series) -> pd.DataFrame:
        x = normalize_name(names).to_numpy(dtype=object)
        p = self.pipeline.predict_proba(x)
        conf = p.max(axis=1)
        label = self.pipeline.classes_[p.argmax(axis=1)]
        label = np.where(conf >= self.min_confidence, label, UNKNOWN)
        return pd.DataFrame({"category": label, "category_confidence": conf.round(3)}, index=names.index)


def assign_categories(establishments: pd.DataFrame, archive: pd.DataFrame,
                      *, evaluate: bool = True) -> tuple[pd.DataFrame, dict]:
    """Add ``category``, ``category_source`` ('archive' | 'inferred' | 'unknown') and
    ``category_confidence`` columns to the establishments table."""
    est = establishments.copy()
    est["category"] = category_of_type(est["type"])
    est["category_source"] = np.where(est["category"].notna(), "archive", "unknown")
    est["category_confidence"] = np.where(est["category"].notna(), 1.0, np.nan)

    train = archive.dropna(subset=["name", "type"]).copy()
    train["label"] = category_of_type(train["type"])
    # Fixed row order so results don't depend on how upstream steps happened to order rows.
    train = train.dropna(subset=["label"]).sort_values("est_key", kind="stable")
    report: dict = {"archive_training_rows": int(len(train))}
    todo = est["category"].isna() & est["name"].notna()
    if len(train) >= 200 and train["label"].nunique() >= 2 and todo.any():
        clf = NameCategoryClassifier()
        if evaluate:
            report["cross_validation"] = clf.evaluate(train["name"], train["label"])
            log.info("Name->category classifier (grouped CV): %s", report["cross_validation"])
        clf.fit(train["name"], train["label"])
        pred = clf.predict(est.loc[todo, "name"])
        est.loc[todo, "category"] = pred["category"].astype("string")
        est.loc[todo, "category_confidence"] = pred["category_confidence"]
        est.loc[todo, "category_source"] = np.where(pred["category"] == UNKNOWN, "unknown", "inferred")
    est["category"] = est["category"].fillna(UNKNOWN).astype("string")
    report["category_source_counts"] = est["category_source"].value_counts().to_dict()
    report["category_counts"] = est["category"].value_counts().to_dict()
    return est, report


def chain_sizes(names: pd.Series) -> pd.Series:
    """Number of establishments sharing the same normalized name (1 = independent)."""
    key = normalize_name(names)
    key = key.str.replace(r"\b(INC|LTD|LIMITED|CORP|CO)\b", "", regex=True).str.strip()
    counts = key.map(key.value_counts())
    return counts.where(key != "", 1).astype(int)

