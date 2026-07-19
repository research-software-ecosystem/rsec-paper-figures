# rsec-paper-figures

This repository contains code to reproduce all figures for the paper "The ELIXIR Research Software Ecosystem: An Open Metadata Commons for Software".

## Setup

From the repository root, create and activate a virtual environment and install the Python dependencies:

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt

## OpenAIRE Software Metadata Scraper

The `openaire.py` script fetches research software metadata from the OpenAIRE Graph API and generates temporal analyses.

### What it does

- **Fetches research software metadata** from the OpenAIRE Graph API by month-year periods using `researchProducts?type=software`
- **Collects** software title, publication year/month, URL, and persistent identifier (PID)
- **Implements caching** via `requests-cache` to store API responses in an SQLite database, reducing redundant API calls on re-runs
- **Handles errors gracefully** with automatic retries, exponential backoff, and cursor-based paging
- **Respects OpenAIRE request limits** by using the documented maximum `pageSize=100`, cursor paging for result sets above the 10,000-record offset-paging limit, and conservative throttling for uncached API calls
- **Saves checkpoints** after processing each year, allowing resumption if interrupted
- **Generates a Plotnine bar plot** showing research software records by publication year (when `--plot` is specified)

### Usage

```bash
cd fig1a

# Basic usage - fetch from 1960 to current year
python openaire.py

# Custom year range
python openaire.py --start-year 2000 --end-year 2024

# With custom output file and plot
python openaire.py -s 2010 -e 2024 -o results.csv -p software_per_year.png

# Generate the publication-year figure from annual counts without downloading all metadata
python openaire.py --counts-only -o openaire_research_software_counts.csv -p openaire_research_software_per_year.png

# Generate a log-scale variant of the publication-year figure
python openaire.py --counts-only -o openaire_research_software_counts.csv -p openaire_research_software_per_year_log.png --y-scale log

# Generate a log-scale variant with an exponential fit overlay
python openaire.py --counts-only -o openaire_research_software_counts.csv -p openaire_research_software_exponential_fit_2014_2025.png --y-scale log --exponential-fit-start-year 2014 --exponential-fit-end-year 2025

# Use an OpenAIRE personal access token for the higher authenticated request limit
OPENAIRE_API_TOKEN=your_token python openaire.py --counts-only

# Clear the API cache
python openaire.py --clear-cache
```

By default, uncached unauthenticated OpenAIRE requests are throttled to one request every 61 seconds, matching the public limit of 60 requests/hour. If `OPENAIRE_API_TOKEN` is set, requests use `Authorization: Bearer ...` and default to a 0.5 second interval, matching the authenticated limit of 7,200 requests/hour. The interval can be overridden with `OPENAIRE_REQUEST_INTERVAL_SECONDS`.

### Output

- `openaire_software_complete.csv` (or custom `-o` filename): CSV with columns `title`, `year`, `month`, `year_month`, `url`, `pid`
- `openaire_research_software_counts.csv` when `--counts-only` is used: CSV with columns `year`, `count`
- `openaire_cache.sqlite`: Cached API responses
- Optional PNG or SVG Plotnine chart showing research software records by publication year
- Log files with timestamped execution details

---

## bio.tools Scatter Plot Generator

The `biotools_scatter.py` script analyzes bio.tools entries to compare publication dates with entry creation dates.

### What it does

- **Processes bio.tools JSON files** from the data directory structure
- **Extracts publication dates** by fetching DOI metadata from Crossref API
- **Compares dates** between first publication and bio.tools entry creation
- **Implements two levels of caching**: Crossref API responses in `doi_cache.sqlite`, and processed bio.tools records in `biotools_processed_cache.sqlite`
- **Resumes after interruption** by checkpointing processed records after every batch; unchanged source files are reused on later runs
- **Generates a combined visualization** with:
  - Scatter plot of entry creation vs publication date
  - Marginal density distributions
  - Color-coded time difference between dates
- **Outputs summary statistics** including average lag, median lag, and percentage of entries created after publication

### Usage

```bash
# Download the bio.tools JSON corpus into a sibling directory
git clone --depth 1 https://github.com/research-software-ecosystem/metadata-commons.git ../metadata-commons

cd fig1b

# Process bio.tools data and generate plot
python biotools_scatter.py --data-dir ../../metadata-commons/data

# Custom output filename
python biotools_scatter.py -d ../../metadata-commons/data -o my_plot.png

# Checkpoint more frequently while processing new records
python biotools_scatter.py -d ../../metadata-commons/data --batch-size 50

# Clear both the DOI and processed-record caches
python biotools_scatter.py --clear-cache
```

### Output

- `biotools-entries-publication-combined.png` (or custom `-o` filename): Combined scatter plot with marginal distributions
- `doi_cache.sqlite`: Cached Crossref API responses
- `biotools_processed_cache.sqlite`: Cached extracted dates for unchanged bio.tools source files
- Log files with timestamped execution details and summary statistics

---

*This README was created with the help of the opencode AI agent (model: litellm/medium).*
