# Diwali Sales Data — Exploratory Data Analysis (EDA)

A beginner-friendly data exploration project that analyses Diwali sales records to uncover purchasing patterns across gender, age, state, marital status, occupation, and product category.

---

## Project Overview

This notebook performs end-to-end EDA on a Diwali Sales dataset. It cleans the raw CSV data, then uses visualisations to answer questions such as:

- Which gender and age group spends the most?
- Which states generate the highest orders and revenue?
- How does marital status influence purchasing behaviour?
- Which occupations and product categories drive the most sales?

---

## Dataset

| Property | Details |
|---|---|
| File | `Diwali Sales Data.csv` |
| Format | CSV (comma-separated values, unicode-escaped encoding) |
| Key columns | `Gender`, `Age`, `Age Group`, `State`, `Marital_Status`, `Occupation`, `Product_Category`, `Product_ID`, `Orders`, `Amount` |

> **Note:** Columns `Status` and `unnamed1` are dropped during cleaning as they are unrelated or blank.

---

## Libraries Used

| Library | Purpose |
|---|---|
| `numpy` | Numerical operations |
| `pandas` | Data loading, cleaning, and aggregation |
| `matplotlib` | Base plotting |
| `seaborn` | Statistical bar charts and count plots |

---

## Project Structure

```
project_on_data_explorator1.ipynb   # Main notebook
Diwali Sales Data.csv               # Source dataset (place in /content/)
README.md                           # This file
```

---

## Steps Performed

### 1. Data Loading
- Import libraries
- Read the CSV with `encoding='unicode_escape'`

### 2. Data Cleaning
- Inspect shape, head, and info
- Drop irrelevant columns (`Status`, `unnamed1`)
- Remove null values with `dropna()`
- Cast `Amount` column to integer

### 3. Exploratory Data Analysis

#### Gender
- Count plot of buyers by gender
- Bar plot of total sales amount by gender

#### Age & Age Group
- Count plots for age and age group
- Age group breakdown by gender
- Total sales amount by age group

#### State
- Top 10 states by number of orders
- Top 10 states by total sales amount

#### Marital Status
- Count plot of marital status
- Sales amount by marital status, split by gender

#### Occupation
- Count plot of buyers by occupation
- Total sales amount by occupation

#### Product Category
- Count plot by product category
- Total sales amount by product category

#### Top Products
- Top 10 products by number of orders (by `Product_ID`)

---

## How to Run

1. Upload `Diwali Sales Data.csv` to `/content/` (Google Colab) or update the file path.
2. Open `project_on_data_explorator1.ipynb` in Jupyter Notebook or Google Colab.
3. Run all cells from top to bottom (`Runtime → Run all` in Colab).

### Requirements

```bash
pip install numpy pandas matplotlib seaborn
```

---

## Key Insights (from analysis)

- **Female buyers** tend to purchase more and spend more than male buyers.
- **Age group 26–35** is the most active purchasing segment.
- **Uttar Pradesh, Maharashtra, and Karnataka** lead in both orders and revenue.
- **Married women** show higher spending compared to other groups.
- **IT, Healthcare, and Aviation** professionals are top spenders by occupation.
- **Food, Clothing, and Electronics** are the highest-selling product categories.

---

## Author

Student project on Data Exploration using Python and Seaborn.
