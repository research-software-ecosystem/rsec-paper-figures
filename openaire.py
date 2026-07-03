import pandas as pd
import time
import traceback
import logging
import os
from datetime import datetime, timedelta
from requests_cache import CachedSession
from calendar import monthrange

try:
    import matplotlib.pyplot as plt
    import matplotlib

    matplotlib.use("Agg")  # Use non-interactive backend
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
PAGE_SIZE = 100


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
    Returns (period_count, hit_limit, new_cache_hits, new_cache_misses).
    """
    logger.info(f"  Processing {from_date} to {to_date}")
    period_count = 0
    cursor = "*"
    hit_limit = False

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

                if response.from_cache:
                    cache_hits += 1
                else:
                    cache_misses += 1

                response.raise_for_status()
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
                    wait_time = 2**retry_count
                    logger.info(
                        f"Retrying in {wait_time} seconds... ({retry_count}/{max_retries})"
                    )
                    time.sleep(wait_time)
                else:
                    success = True
                    has_results = False
                    break

        if not success or not has_results:
            break

        try:
            if not response.from_cache:
                time.sleep(0.5)
        except NameError:
            # response might not be defined if we broke from the loop early
            pass

    return period_count, hit_limit, cache_hits, cache_misses


def fetch_with_fallback(
    from_date,
    to_date,
    year,
    month,
    all_software,
    cache_hits,
    cache_misses,
    max_depth=5,
    current_depth=0,
):
    """
    Recursively fetch research software for a date period, splitting if needed.
    """
    if current_depth >= max_depth:
        logger.error(
            f"    Maximum depth reached for {from_date} to {to_date}. Some data may be missing."
        )
        return 0, cache_hits, cache_misses

    period_count, hit_limit, cache_hits, cache_misses = (
        fetch_research_software_for_period(
            from_date, to_date, year, month, all_software, cache_hits, cache_misses
        )
    )

    if hit_limit:
        # Split the period in two and retry
        logger.info(
            f"    Splitting period {from_date} to {to_date} in half (depth {current_depth + 1})"
        )

        # Parse dates
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")

        # Calculate mid point
        delta = to_dt - from_dt
        mid_dt = from_dt + delta / 2

        # First half (from_date to mid_date)
        mid_date = mid_dt.strftime("%Y-%m-%d")
        logger.info(f"    First half: {from_date} to {mid_date}")
        count1, cache_hits, cache_misses = fetch_with_fallback(
            from_date,
            mid_date,
            year,
            month,
            all_software,
            cache_hits,
            cache_misses,
            max_depth,
            current_depth + 1,
        )

        # Second half (mid_date to to_date) - note: API uses inclusive ranges
        # To avoid duplicates, we might need to adjust. But the API filters by date accepted
        # and splitting at midnight should naturally separate the data.
        logger.info(f"    Second half: {mid_date} to {to_date}")
        count2, cache_hits, cache_misses = fetch_with_fallback(
            mid_date,
            to_date,
            year,
            month,
            all_software,
            cache_hits,
            cache_misses,
            max_depth,
            current_depth + 1,
        )

        period_count = count1 + count2

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

            # Use the fetch_with_fallback function that automatically splits periods if 10k limit is hit
            month_count, cache_hits, cache_misses = fetch_with_fallback(
                from_date, to_date, year, month, all_software, cache_hits, cache_misses
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


def fetch_research_software_count_for_year(year, cache_hits, cache_misses):
    """Fetch the OpenAIRE count of research software records for one year."""
    params = {
        "page": 1,
        "pageSize": 1,
        "type": RESEARCH_SOFTWARE_TYPE,
        "fromPublicationDate": str(year),
        "toPublicationDate": str(year),
    }

    response = session.get(OPENAIRE_RESEARCH_PRODUCTS_URL, params=params, timeout=30)
    if response.from_cache:
        cache_hits += 1
    else:
        cache_misses += 1

    response.raise_for_status()
    data = response.json()
    count = data.get("header", {}).get("numFound", 0)

    if not response.from_cache:
        time.sleep(0.5)

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


def plot_research_software_counts(
    year_counts, plot_file, start_year, end_year, y_scale="linear"
):
    """Generate the OpenAIRE research software publication-year count plot."""
    fig, ax = plt.subplots(figsize=(12, 10))
    ax.bar(
        year_counts.index,
        year_counts.values,
        color="steelblue",
        edgecolor="black",
    )
    ax.set_xlabel("Year", fontsize=12)
    ax.set_ylabel("Number of research software records", fontsize=12)
    if y_scale == "log":
        ax.set_yscale("symlog", linthresh=1)

    title_text = (
        f"Research Software Records by publication year {start_year}-{end_year}"
    )
    if y_scale == "log":
        title_text = f"{title_text} (log-scale y axis)"
    subtitle_text = (
        f"source: OpenAIRE Graph API, retrieved on {datetime.now().strftime('%Y-%m-%d')}"
    )
    ax.set_title(f"{title_text}\n{subtitle_text}", fontsize=12, fontweight="bold")

    ax.grid(axis="y", alpha=0.3, linestyle="--")

    decade_years = year_counts[(year_counts.index % 10 == 0) & (year_counts > 0)]
    for year, count in decade_years.items():
        ax.text(
            year,
            count,
            str(count),
            ha="center",
            va="bottom",
            fontsize=8,
        )

    plt.tight_layout()
    plt.savefig(plot_file, dpi=300, bbox_inches="tight")
    plt.close()


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
                logger.warning("\n⚠️  Cannot generate plot: matplotlib is not installed")
                logger.info("Install it with: pip install matplotlib")
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
