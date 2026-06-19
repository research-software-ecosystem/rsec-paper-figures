# rsec-paper-figures

This repository contains code to reproduce all figures for the paper "The ELIXIR Research Software Ecosystem: An Open Metadata Commons for Software".

## OpenAIRE Software Metadata Scraper

The `openaire.py` script fetches research software metadata from the OpenAIRE API and generates temporal analyses.

### What it does

- **Fetches software metadata** from the OpenAIRE API by month-year periods to bypass the 10k results limit
- **Collects** software title, publication year/month, URL, and persistent identifier (PID)
- **Implements caching** via `requests-cache` to store API responses in an SQLite database, reducing redundant API calls on re-runs
- **Handles errors gracefully** with automatic retries, exponential backoff, and recursive period splitting when limits are hit
- **Saves checkpoints** after processing each year, allowing resumption if interrupted
- **Generates a bar plot** showing the number of new software products per year (when `--plot` is specified)

### Usage

```bash
# Basic usage - fetch from 1960 to current year
python openaire.py

# Custom year range
python openaire.py --start-year 2000 --end-year 2024

# With custom output file and plot
python openaire.py -s 2010 -e 2024 -o results.csv -p software_per_year.png

# Clear the API cache
python openaire.py --clear-cache
```

### Output

- `openaire_software_complete.csv` (or custom `-o` filename): CSV with columns `title`, `year`, `month`, `year_month`, `url`, `pid`
- `openaire_cache.sqlite`: Cached API responses
- Optional PNG plot showing software publications per year
- Log files with timestamped execution details

---

*This README was created with the help of the opencode AI agent (model: litellm/medium).*