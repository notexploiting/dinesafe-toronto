# DineSafe Toronto
Explore restaurant inspection data from the City of Toronto.

![City of Toronto Logo](https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSyy76VN3b0xT5ReM-Av4gu3flYxW3sArThhA&s)

DineSafe Toronto is a data science projected focused on analyzing food safety inspections conducted by Toronto Public Health. We explore temporal trends, establishment characteristics, neighborhood distributions, as well as patterns in infractions, severity levels, location, and operations.

| DineSafe Pass Notice | DineSafe Conditional Notice | DineSafe Closed Notice |
|----------------------|-----------------------------|------------------------|
| ![Pass](https://www.toronto.ca/wp-content/uploads/2019/10/981a-pass_lrg.png) | ![Conditional](https://www.toronto.ca/wp-content/uploads/2019/10/906e-conditional_lrg.png) | ![Closed](https://www.toronto.ca/wp-content/uploads/2019/10/9098-closed_lrg.png) |



## Example Visual
<p align="center"> <img src="notebooks/assets/inspections_by_month.png" width="600"/> </p>



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

### Run the EDA notebook
Open `notebooks/01_eda.ipynb` in Jupyter or VSCode

## Project Goals

### Data Cleaning
- Convert inspection dates to datetime
- Handle nulls and inconsistent types
- Drop irrelevant or unusable columns

### Exploratory Data Analysis (EDA)
- What are the most common infractions?
- Which establishment types have the most serious violations?
- How do inspection trends vary over time and geography?

### Feature Engineering
- Risk classification: High/Medium/Low risk establishments
- Inspection count and infraction rate per restaurant
- Repeat offender tagging

### Visualizations
- Top 10 infractions by severity
- Infractions over time 
- Heatmap of neighborhoods with high violation density

## Data Source

- Dataset: [City of Toronto DineSafe Open Data](https://open.toronto.ca/dataset/dinesafe/)
- License: [Open Government License - Toronto](https://open.toronto.ca/open-data-licence/)
- Updated: Automatically using the CKAN API

## Notebooks Rendered on GitHub
You can view `01_eda.ipynb` directly in your browser [here](https://github.com/notexploiting/dinesafe-toronto/blob/main/notebooks/01_eda.ipynb), no need to download or run anything. Currently doesn't work on mobile devices.
