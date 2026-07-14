import pandas as pd
import numpy as np
import time
import traceback
import logging
import os
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from requests_cache import CachedSession
from calendar import monthrange

try:
    from plotnine import (
        aes,
        element_blank,
        element_line,
        element_rect,
        element_text,
        geom_col,
        geom_line,
        geom_point,
        geom_text,
        ggplot,
        labs,
        scale_color_manual,
        scale_fill_manual,
        scale_x_continuous,
        scale_y_continuous,
        scale_y_symlog,
        theme,
        theme_minimal,
    )

    PLOTTING_AVAILABLE = True
except ImportError:
    PLOTTING_AVAILABLE = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(f"openaire_scrape_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Create cached session
session = CachedSession(
    "openaire_cache",
    backend="sqlite",
    expire_after=timedelta(days=7),
    stale_if_error=True,
)

logger.info(f"Requests cache enabled: openaire_cache.sqlite")

# OpenAIRE documents research software as research products with type=software.
OPENAIRE_RESEARCH_PRODUCTS_URL = "https://api.openaire.eu/graph/v2/researchProducts"
RESEARCH_SOFTWARE_TYPE = "software"
PLOT_HIGHLIGHT_START_YEAR = 2014
# OpenAIRE currently rejects larger page sizes with "Page size must be at most 100".
PAGE_SIZE = 100
# OpenAIRE public API limits: 60 requests/hour unauthenticated, 7200/hour authenticated.
UNAUTHENTICATED_REQUEST_INTERVAL_SECONDS = 61.0
AUTHENTICATED_REQUEST_INTERVAL_SECONDS = 0.5
OPENAIRE_API_TOKEN = os.environ.get("OPENAIRE_API_TOKEN")


def get_request_interval_seconds():
    """Return the configured delay between uncached OpenAIRE API requests."""
    authenticated_default = AUTHENTICATED_REQUEST_INTERVAL_SECONDS
    unauthenticated_default = UNAUTHENTICATED_REQUEST_INTERVAL_SECONDS
    default_interval = (
        authenticated_default if OPENAIRE_API_TOKEN else unauthenticated_default
    )
    configured_interval = os.environ.get("OPENAIRE_REQUEST_INTERVAL_SECONDS")
    if configured_interval is None:
        return default_interval

    try:
        interval = float(configured_interval)
    except ValueError:
        logger.warning(
            "Ignoring invalid OPENAIRE_REQUEST_INTERVAL_SECONDS=%r; using %.1fs",
            configured_interval,
            default_interval,
        )
        return default_interval

    if interval < 0:
        logger.warning(
            "Ignoring negative OPENAIRE_REQUEST_INTERVAL_SECONDS=%r; using %.1fs",
            configured_interval,
            default_interval,
        )
        return default_interval

    return interval


if OPENAIRE_API_TOKEN:
    session.headers.update({"Authorization": f"Bearer {OPENAIRE_API_TOKEN}"})
    logger.info(
        "OpenAIRE API token detected; using authenticated requests and %.1fs default throttle",
        AUTHENTICATED_REQUEST_INTERVAL_SECONDS,
    )
else:
    logger.info(
        "No OPENAIRE_API_TOKEN set; uncached API calls will be throttled to %.1fs",
        UNAUTHENTICATED_REQUEST_INTERVAL_SECONDS,
    )

REQUEST_INTERVAL_SECONDS = get_request_interval_seconds()
logger.info(
    "OpenAIRE request interval for uncached responses: %.1fs",
    REQUEST_INTERVAL_SECONDS,
)


def retry_after_seconds(response):
    """Parse a Retry-After header as seconds, if present."""
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None

    try:
        return max(float(retry_after), 0)
    except ValueError:
        try:
            retry_after_dt = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, OverflowError):
            return None

    now = datetime.now(retry_after_dt.tzinfo)
    return max((retry_after_dt - now).total_seconds(), 0)


def retry_wait_seconds(error, retry_count):
    """Choose a retry delay, respecting OpenAIRE rate-limit responses."""
    response = getattr(error, "response", None)
    if response is not None and response.status_code == 429:
        wait_time = retry_after_seconds(response)
        if wait_time is not None:
            return wait_time
        return max(REQUEST_INTERVAL_SECONDS, UNAUTHENTICATED_REQUEST_INTERVAL_SECONDS)

    return 2**retry_count


def sleep_after_live_response(response):
    """Throttle only real API calls; cached responses do not count against the API limit."""
    if getattr(response, "from_cache", False):
        return
    if REQUEST_INTERVAL_SECONDS > 0:
        time.sleep(REQUEST_INTERVAL_SECONDS)


def first_value(value):
    """Return a useful scalar from an OpenAIRE value that may be a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def get_research_software_url(item):
    """Extract the most useful URL exposed by the Graph API response."""
    code_repository_url = first_value(item.get("codeRepositoryUrl"))
    if code_repository_url:
        return code_repository_url

    documentation_url = first_value(item.get("documentationUrls"))
    if documentation_url:
        return documentation_url

    for instance in item.get("instances") or []:
        url = first_value(instance.get("urls"))
        if url:
            return url

    return None


def fetch_research_software_for_period(
    from_date,
    to_date,
    year,
    month,
    all_software,
    cache_hits,
    cache_misses,
    retry_on_error=True,
    max_retries=3,
):
    """
    Fetch research software for a specific publication-date period.
    Returns (period_count, new_cache_hits, new_cache_misses).
    """
    logger.info(f"  Processing {from_date} to {to_date}")
    period_count = 0
    cursor = "*"

    while True:
        params = {
            "pageSize": PAGE_SIZE,
            "cursor": cursor,
            "type": RESEARCH_SOFTWARE_TYPE,
            "fromPublicationDate": from_date,
            "toPublicationDate": to_date,
        }

        retry_count = 0
        success = False
        has_results = False

        while not success and retry_count < max_retries:
            try:
                logger.debug(
                    f"Requesting {from_date} to {to_date}, cursor {cursor}, attempt {retry_count + 1}"
                )

                response = session.get(
                    OPENAIRE_RESEARCH_PRODUCTS_URL, params=params, timeout=30
                )

                if getattr(response, "from_cache", False):
                    cache_hits += 1
                else:
                    cache_misses += 1

                response.raise_for_status()
                sleep_after_live_response(response)
                data = response.json()

                results = data.get("results", [])

                if not results or results is None:
                    success = True
                    has_results = False
                    break

                has_results = True

                for item in results:
                    try:
                        result_type = item.get("type")
                        if result_type != RESEARCH_SOFTWARE_TYPE:
                            logger.warning(
                                f"Skipping non-software OpenAIRE result {item.get('id')}: type={result_type}"
                            )
                            continue

                        title = item.get("mainTitle") or "N/A"
                        url = get_research_software_url(item)

                        all_software.append(
                            {
                                "title": title,
                                "year": year,
                                "month": month,
                                "year_month": f"{year}-{month:02d}",
                                "url": url,
                                "pid": item.get("id", "N/A"),
                            }
                        )
                        period_count += 1
                    except Exception as item_error:
                        logger.error(
                            f"Error processing individual item in {from_date}-{to_date}, cursor {cursor}"
                        )
                        logger.error(f"Item error: {item_error}")
                        traceback.print_exc()
                        continue

                logger.info(
                    f"    Cursor page: +{len(results)} research software records (period total: {period_count})"
                )

                next_cursor = data.get("header", {}).get("nextCursor")
                if not next_cursor or next_cursor == cursor:
                    success = True
                    has_results = False
                    break

                cursor = next_cursor

                success = True

            except Exception as e:
                logger.error(f"\n❌ Error on {from_date}-{to_date}, cursor {cursor}:")
                logger.error(f"Error type: {type(e).__name__}")
                logger.error(f"Error message: {e}")
                traceback.print_exc()

                retry_count += 1
                if retry_count < max_retries and retry_on_error:
                    wait_time = retry_wait_seconds(e, retry_count)
                    logger.info(
                        f"Retrying in {wait_time:.1f} seconds... ({retry_count}/{max_retries})"
                    )
                    time.sleep(wait_time)
                else:
                    success = True
                    has_results = False
                    break

        if not success or not has_results:
            break

    return period_count, cache_hits, cache_misses


def get_openaire_software_by_month(
    start_year=2000, end_year=2024, retry_on_error=True, max_retries=3
):
    """Get OpenAIRE research software month by month."""
    all_software = []
    cache_hits = 0
    cache_misses = 0

    for year in range(start_year, end_year + 1):
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Processing year {year}")
        logger.info(f"{'=' * 60}")

        for month in range(1, 13):
            # Get the last day of the month
            last_day = monthrange(year, month)[1]

            # Format dates
            from_date = f"{year}-{month:02d}-01"
            to_date = f"{year}-{month:02d}-{last_day}"

            logger.info(
                f"\n=== Processing {year}-{month:02d} ({from_date} to {to_date}) ==="
            )

            month_count, cache_hits, cache_misses = fetch_research_software_for_period(
                from_date,
                to_date,
                year,
                month,
                all_software,
                cache_hits,
                cache_misses,
                retry_on_error=retry_on_error,
                max_retries=max_retries,
            )

            logger.info(
                f"Total for {year}-{month:02d}: {month_count} research software records"
            )

        # Save checkpoint after each year
        if all_software:
            df_temp = pd.DataFrame(all_software)
            df_temp.to_csv(f"openaire_software_checkpoint_{year}.csv", index=False)
            logger.info(
                f"✓ Checkpoint saved at year {year} (total so far: {len(all_software)})"
            )

    logger.info(f"\n{'=' * 60}")
    logger.info(f"=== Cache Statistics ===")
    logger.info(f"{'=' * 60}")
    logger.info(f"Cache hits: {cache_hits}")
    logger.info(f"Cache misses (API calls): {cache_misses}")
    if (cache_hits + cache_misses) > 0:
        logger.info(
            f"Cache efficiency: {cache_hits / (cache_hits + cache_misses) * 100:.1f}%"
        )

    return pd.DataFrame(all_software)


def fetch_research_software_count_for_year(
    year, cache_hits, cache_misses, retry_on_error=True, max_retries=3
):
    """Fetch the OpenAIRE count of research software records for one year."""
    params = {
        "page": 1,
        "pageSize": 1,
        "type": RESEARCH_SOFTWARE_TYPE,
        "fromPublicationDate": f"{year}-01-01",
        "toPublicationDate": f"{year}-12-31",
    }

    retry_count = 0
    while True:
        try:
            response = session.get(
                OPENAIRE_RESEARCH_PRODUCTS_URL, params=params, timeout=30
            )
            if getattr(response, "from_cache", False):
                cache_hits += 1
            else:
                cache_misses += 1

            response.raise_for_status()
            sleep_after_live_response(response)
            break
        except Exception as e:
            retry_count += 1
            if retry_count < max_retries and retry_on_error:
                wait_time = retry_wait_seconds(e, retry_count)
                logger.info(
                    f"Retrying count request for {year} in {wait_time:.1f} seconds... ({retry_count}/{max_retries})"
                )
                time.sleep(wait_time)
            else:
                raise

    data = response.json()
    count = data.get("header", {}).get("numFound", 0)

    return int(count), cache_hits, cache_misses


def get_openaire_research_software_counts_by_year(start_year=2000, end_year=2024):
    """Get annual OpenAIRE research software counts without downloading metadata."""
    rows = []
    cache_hits = 0
    cache_misses = 0

    for year in range(start_year, end_year + 1):
        logger.info(f"Fetching research software count for {year}")
        count, cache_hits, cache_misses = fetch_research_software_count_for_year(
            year, cache_hits, cache_misses
        )
        rows.append({"year": year, "count": count})
        logger.info(f"  {year}: {count}")

    logger.info(f"\n{'=' * 60}")
    logger.info("=== Count Cache Statistics ===")
    logger.info(f"{'=' * 60}")
    logger.info(f"Cache hits: {cache_hits}")
    logger.info(f"Cache misses (API calls): {cache_misses}")

    return pd.DataFrame(rows)


def format_count_labels(values):
    """Format y-axis count labels with thousands separators."""
    labels = []
    for value in values:
        if pd.isna(value):
            labels.append("")
            continue

        rounded_value = round(float(value))
        if abs(rounded_value) >= 1000:
            labels.append(f"{rounded_value:,.0f}")
        else:
            labels.append(f"{rounded_value:.0f}")
    return labels


def make_plot_dataframe(year_counts, y_scale):
    """Return plotting data with visual grouping and sparse direct labels."""
    plot_df = year_counts.rename_axis("year").reset_index(name="count")
    plot_df["year"] = plot_df["year"].astype(int)
    plot_df["count"] = plot_df["count"].astype(int)

    current_year = datetime.now().year
    current_year_label = f"{current_year} to date"
    before_highlight_label = f"Before {PLOT_HIGHLIGHT_START_YEAR}"
    highlight_label = f"{PLOT_HIGHLIGHT_START_YEAR} onward"
    categories = [before_highlight_label, highlight_label]

    def period_for_year(year):
        if year == current_year:
            return current_year_label
        if year < PLOT_HIGHLIGHT_START_YEAR:
            return before_highlight_label
        return highlight_label

    plot_df["period"] = plot_df["year"].apply(period_for_year)
    if current_year_label in plot_df["period"].values:
        categories.append(current_year_label)
    plot_df["period"] = pd.Categorical(
        plot_df["period"], categories=categories, ordered=True
    )

    max_count = max(plot_df["count"].max(), 1)
    label_df = plot_df[(plot_df["year"] % 5 == 0) & (plot_df["count"] > 0)].copy()
    label_df["label"] = label_df["count"].map(lambda count: f"{count:,}")
    if y_scale == "log":
        label_df["label_y"] = label_df["count"] * 1.2
    else:
        label_df["label_y"] = label_df["count"] + max_count * 0.018

    return plot_df, label_df


def fit_exponential_counts(year_counts, fit_start_year, fit_end_year):
    """Fit count = a * exp(b * (year - fit_start_year)) on positive counts."""
    fit_counts = year_counts.loc[fit_start_year:fit_end_year]
    fit_counts = fit_counts[fit_counts > 0]
    if len(fit_counts) < 2:
        raise ValueError(
            "At least two positive annual counts are required for an exponential fit"
        )

    years_since_start = fit_counts.index.to_numpy(dtype=float) - fit_start_year
    log_counts = np.log(fit_counts.to_numpy(dtype=float))
    slope, intercept = np.polyfit(years_since_start, log_counts, 1)
    fitted_logs = intercept + slope * years_since_start

    residual_sum_squares = np.sum((log_counts - fitted_logs) ** 2)
    total_sum_squares = np.sum((log_counts - log_counts.mean()) ** 2)
    r_squared = 1 - residual_sum_squares / total_sum_squares

    fit_years = np.arange(fit_start_year, fit_end_year + 1)
    fit_df = pd.DataFrame({"year": fit_years})
    fit_df["fit_count"] = np.exp(intercept + slope * (fit_years - fit_start_year))
    fit_df["series"] = f"Exponential fit {fit_start_year}-{fit_end_year}"

    stats = {
        "fit_start_year": fit_start_year,
        "fit_end_year": fit_end_year,
        "intercept_count": float(np.exp(intercept)),
        "slope": float(slope),
        "annual_factor": float(np.exp(slope)),
        "annual_growth_percent": float((np.exp(slope) - 1) * 100),
        "doubling_time_years": float(np.log(2) / slope),
        "r_squared": float(r_squared),
        "series": fit_df["series"].iloc[0],
    }

    return fit_df, stats


def default_fit_end_year(end_year):
    """Avoid fitting the current year, because it is usually incomplete."""
    current_year = datetime.now().year
    if end_year >= current_year:
        return end_year - 1
    return end_year


def plot_research_software_counts(
    year_counts,
    plot_file,
    start_year,
    end_year,
    y_scale="linear",
    fit_start_year=None,
    fit_end_year=None,
):
    """Generate the OpenAIRE research software publication-year count plot."""
    if year_counts.empty:
        raise ValueError("Cannot generate a plot from an empty year count series")

    plot_df, label_df = make_plot_dataframe(year_counts, y_scale)
    max_count = max(plot_df["count"].max(), 1)
    fit_df = None
    fit_stats = None
    visible_max = max_count
    if fit_start_year is not None:
        if fit_end_year is None:
            fit_end_year = default_fit_end_year(end_year)
        fit_df, fit_stats = fit_exponential_counts(
            year_counts, fit_start_year, fit_end_year
        )
        visible_max = max(visible_max, fit_df["fit_count"].max())

    label_upper_limit = (
        label_df["label_y"].max() * 1.08 if not label_df.empty else max_count
    )

    y_upper_limit = max(visible_max * 1.18, label_upper_limit)
    x_break_start = (start_year // 5) * 5
    x_breaks = [
        year
        for year in range(x_break_start, end_year + 1, 5)
        if start_year <= year <= end_year
    ]
    if x_breaks and end_year not in x_breaks and end_year - x_breaks[-1] < 3:
        x_breaks = x_breaks[:-1]
    x_breaks = sorted(set([start_year, end_year, *x_breaks]))

    title_text = "OpenAIRE research software records by publication year"
    subtitle_text = (
        f"Annual records from the OpenAIRE Graph API, {start_year}-{end_year}"
    )
    if y_scale == "log":
        subtitle_text = f"{subtitle_text}; symmetric log y-axis"
    if fit_stats is not None:
        subtitle_text = (
            f"{subtitle_text}; exponential fit "
            f"{fit_stats['fit_start_year']}-{fit_stats['fit_end_year']}"
        )

    y_scale_layer = (
        scale_y_symlog(
            breaks=[1, 10, 100, 1000, 10000, 100000],
            labels=format_count_labels,
            limits=(0, y_upper_limit),
            expand=(0.02, 0),
        )
        if y_scale == "log"
        else scale_y_continuous(
            labels=format_count_labels,
            limits=(0, y_upper_limit),
            expand=(0, 0),
        )
    )

    period_colors = {
        f"Before {PLOT_HIGHLIGHT_START_YEAR}": "#9CA3AF",
        f"{PLOT_HIGHLIGHT_START_YEAR} onward": "#2A9D8F",
        f"{datetime.now().year} to date": "#E76F51",
    }
    active_periods = [
        period
        for period in plot_df["period"].cat.categories
        if period in plot_df["period"].values
    ]

    plot = (
        ggplot(plot_df, aes(x="year", y="count", fill="period"))
        + geom_col(width=0.82, color="#FFFFFF", size=0.3)
        + geom_text(
            data=label_df,
            mapping=aes(y="label_y", label="label"),
            size=7,
            color="#22313F",
            va="bottom",
            show_legend=False,
        )
        + scale_fill_manual(
            values=period_colors,
            breaks=active_periods,
            name="",
        )
        + scale_x_continuous(
            breaks=x_breaks,
            limits=(start_year - 0.6, end_year + 1.2),
            expand=(0, 0),
        )
        + y_scale_layer
        + labs(
            title=title_text,
            subtitle=subtitle_text,
            x="Publication year",
            y="Number of records",
            caption=f"Source: OpenAIRE Graph API. Retrieved {datetime.now():%Y-%m-%d}.",
        )
        + theme_minimal(base_size=12, base_family="DejaVu Sans")
        + theme(
            figure_size=(12, 8),
            dpi=300,
            svg_usefonts=False,
            plot_background=element_rect(fill="#FFFFFF", color="#FFFFFF"),
            panel_background=element_rect(fill="#FFFFFF", color="#FFFFFF"),
            panel_grid_major_x=element_blank(),
            panel_grid_minor=element_blank(),
            panel_grid_major_y=element_line(color="#D8DEE4", size=0.45),
            axis_text_x=element_text(rotation=35, ha="right"),
            axis_title_x=element_text(margin={"t": 10}),
            axis_title_y=element_text(margin={"r": 10}),
            plot_title=element_text(
                size=17, weight="bold", color="#1F2933", ha="center"
            ),
            plot_subtitle=element_text(size=11, color="#52616B", ha="center"),
            plot_caption=element_text(size=9, color="#6B7280", ha="center"),
            legend_position="top",
            legend_title=element_blank(),
            legend_background=element_blank(),
            legend_key=element_blank(),
        )
    )
    if fit_df is not None:
        plot = (
            plot
            + geom_line(
                data=fit_df,
                mapping=aes(x="year", y="fit_count", color="series"),
                inherit_aes=False,
                size=1.25,
            )
            + geom_point(
                data=fit_df,
                mapping=aes(x="year", y="fit_count", color="series"),
                inherit_aes=False,
                size=2.5,
            )
            + scale_color_manual(
                values={fit_stats["series"]: "#D1495B"},
                breaks=[fit_stats["series"]],
                name="",
            )
        )

    plot.save(plot_file, width=12, height=8, dpi=300, verbose=False)


if __name__ == "__main__":
    import sys
    import argparse

    # Setup argument parser
    parser = argparse.ArgumentParser(
        description="Scrape research software metadata from the OpenAIRE Graph API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python openaire.py                    # Use defaults (1960 to current year)
  python openaire.py --start-year 2000  # Start from year 2000
  python openaire.py --end-year 2020    # End at year 2020
  python openaire.py -s 2010 -e 2020    # Custom range
  python openaire.py -o results.csv     # Custom output CSV file
  python openaire.py -p plot.png        # Generate a plot of research software per year
  python openaire.py --counts-only -p fit.png --y-scale log --exponential-fit-start-year 2014 --exponential-fit-end-year 2025
  python openaire.py --clear-cache      # Clear the API cache and exit
        """,
    )
    parser.add_argument(
        "--start-year",
        "-s",
        type=int,
        default=1960,
        help="Start year for scraping (default: 1960)",
    )
    parser.add_argument(
        "--end-year",
        "-e",
        type=int,
        default=datetime.now().year,
        help=f"End year for scraping (default: current year {datetime.now().year})",
    )
    parser.add_argument(
        "--clear-cache", action="store_true", help="Clear the API cache and exit"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="openaire_software_complete.csv",
        help="Output CSV file name (default: openaire_software_complete.csv)",
    )
    parser.add_argument(
        "--plot",
        "-p",
        type=str,
        default=None,
        help="Output plot file name (e.g., software_per_year.png). If not specified, no plot is generated",
    )
    parser.add_argument(
        "--counts-only",
        action="store_true",
        help="Fetch annual OpenAIRE counts only, useful for generating the yearly figure without downloading all metadata",
    )
    parser.add_argument(
        "--y-scale",
        choices=["linear", "log"],
        default="linear",
        help="Y-axis scale for generated plots (default: linear). The log option uses a symmetric log scale so zero-count years remain visible.",
    )
    parser.add_argument(
        "--exponential-fit-start-year",
        type=int,
        default=None,
        help="Overlay an exponential fit from this year. By default the fit ends at the last complete year.",
    )
    parser.add_argument(
        "--exponential-fit-end-year",
        type=int,
        default=None,
        help="End year for the exponential fit overlay (default: last complete year).",
    )

    args = parser.parse_args()

    if args.clear_cache:
        session.cache.clear()
        logger.info("Cache cleared. Exiting.")
        sys.exit(0)

    # Validate year range
    current_year = datetime.now().year
    if args.start_year < 1900 or args.start_year > current_year:
        logger.error(
            f"Invalid start year: {args.start_year}. Must be between 1900 and {current_year}"
        )
        sys.exit(1)
    if args.end_year < 1900 or args.end_year > current_year + 1:
        logger.error(
            f"Invalid end year: {args.end_year}. Must be between 1900 and {current_year + 1}"
        )
        sys.exit(1)
    if args.start_year > args.end_year:
        logger.error(
            f"Start year ({args.start_year}) must be less than or equal to end year ({args.end_year})"
        )
        sys.exit(1)

    try:
        logger.info(f"Year range: {args.start_year} to {args.end_year}")

        output_file = args.output

        if args.counts_only:
            logger.info("Starting OpenAIRE research software count retrieval")
            logger.info("Using Graph API researchProducts with type=software")
            df = get_openaire_research_software_counts_by_year(
                args.start_year, args.end_year
            )
            df.to_csv(output_file, index=False)
            logger.info(f"\n✓ Saved annual counts to {output_file}")
        else:
            logger.info("Starting OpenAIRE research software scraping (MONTH BY MONTH)")
            logger.info(
                "Using Graph API researchProducts with type=software and cursor paging"
            )
            df = get_openaire_software_by_month(args.start_year, args.end_year)
            df.to_csv(output_file, index=False)
            logger.info(f"\n✓ Saved results to {output_file}")

        # Generate plot if requested
        if args.plot:
            if not PLOTTING_AVAILABLE:
                logger.warning("\n⚠️  Cannot generate plot: plotnine is not installed")
                logger.info("Install it with: pip install plotnine")
            else:
                try:
                    logger.info(f"\nGenerating plot: {args.plot}")
                    if args.counts_only:
                        year_counts = df.set_index("year")["count"].sort_index()
                    else:
                        year_counts = df.groupby("year").size().sort_index()

                    plot_research_software_counts(
                        year_counts,
                        args.plot,
                        args.start_year,
                        args.end_year,
                        args.y_scale,
                        fit_start_year=args.exponential_fit_start_year,
                        fit_end_year=args.exponential_fit_end_year,
                    )
                    logger.info(f"✓ Plot saved to {args.plot}")
                except Exception as plot_error:
                    logger.error(f"\n❌ Error generating plot: {plot_error}")
                    traceback.print_exc()

        logger.info(f"\n{'=' * 60}")
        logger.info("=== Summary ===")
        logger.info(f"{'=' * 60}")
        if args.counts_only:
            logger.info(f"Total research software records: {df['count'].sum()}")
        else:
            logger.info(f"Total research software records: {len(df)}")

        # By year
        logger.info("\nBy year:")
        if args.counts_only:
            year_counts = df.set_index("year")["count"].sort_index()
        else:
            year_counts = df.groupby("year").size().sort_index()
        for year, count in year_counts.items():
            logger.info(f"  {year}: {count}")

        # By month (for recent years)
        if not args.counts_only:
            logger.info("\nBy year-month (last 3 years):")
            recent_df = df[df["year"] >= 2021]
            if len(recent_df) > 0:
                month_counts = recent_df.groupby("year_month").size().sort_index()
                for ym, count in month_counts.items():
                    logger.info(f"  {ym}: {count}")

    except KeyboardInterrupt:
        logger.warning("\n⚠️  Script interrupted by user")
        logger.info("Cached responses are saved. Re-run to continue.")

        # Save whatever we have
        if "all_software" in locals() and all_software:
            df_partial = pd.DataFrame(all_software)
            emergency_file = (
                f"openaire_software_interrupted_{datetime.now():%Y%m%d_%H%M%S}.csv"
            )
            df_partial.to_csv(emergency_file, index=False)
            logger.info(f"Partial results saved to {emergency_file}")
        sys.exit(130)

    except Exception as e:
        logger.error("\n❌ Fatal error in main execution:")
        traceback.print_exc()

        # Save whatever we have
        if "all_software" in locals() and all_software:
            df_partial = pd.DataFrame(all_software)
            emergency_file = (
                f"openaire_software_error_{datetime.now():%Y%m%d_%H%M%S}.csv"
            )
            df_partial.to_csv(emergency_file, index=False)
            logger.info(f"Partial results saved to {emergency_file}")
        sys.exit(1)
