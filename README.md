# DineSafe Toronto
Explore restaurant inspection data from the City of Toronto.

## Project Summary

DineSafe Toronto is a data analysis project focused on food safety inspections in Toronto. We explore trends in infractions, identify high-risk establishments, and visualize inspection results across time, neighborhoods, and restaurant types.

## Data Source

The dataset is published by the City of Toronto's Open Data portal under the [DineSafe program](https://www.toronto.ca/community-people/health-wellness-care/health-inspections/dinesafe/).
* Free to use (Open Government License – Toronto)
* Accessible on the [Open Data Catalogue](https://open.toronto.ca/dataset/dinesafe/)
* "Gold" grade (>=80%)
  * Freshness (100%): up-to-date
  * Metadata (100%): well-described
  * Accessibility (75%): this dataset hasn't been associated with any additional, searchable keywords
  * Completeness (100%): no missing data
  * Usability (100%): easy to work with data

## Motivation
Toronto’s DineSafe program monitors food safety and public health by inspecting all restaurants and food establishments. This project explores:
* Which businesses are more prone to serious health violations?
* How has food safety changed over time in the city?

## Getting Started

### Clone the repository
```bash
git clone https://github.com/notexploiting/dinesafe-toronto.git
cd dinesafe-toronto
```

### Set up a virtual environment
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### Install dependencies
```bash
pip install -r requirements.txt
```

### Download the dataset
```bash
python src/download_data.py
```

## Project Goals

### Data Cleaning
* Convert inspection dates to datetime
* Handle nulls and inconsistent types

### Exploratory Data Analysis (EDA)
* Most common infractions
* Serious vs. minor violations by restaurant type
* Monthly trends in inspection outcomes
* Chain restaurants vs. independents

### Feature Engineering
* Risk classification: High/Medium/Low
* Inspection count and infraction rate per restaurant

### Visualizations
* Top 10 infractions bar plot
* Infractions over time line chart
* Heatmap of neighborhoods with high violation density

## License

[Open Government License - Toronto](https://open.toronto.ca/open-data-licence/)
