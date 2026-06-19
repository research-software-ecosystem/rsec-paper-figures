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


def fetch_software_for_period(
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
    Fetch software for a specific date period.
    Returns (period_count, hit_limit, new_cache_hits, new_cache_misses)
    If hit_limit is True, it means we hit the 10k limit and need to split.
    """
    logger.info(f"  Processing {from_date} to {to_date}")
    period_count = 0
    page = 1
    hit_limit = False

    while True:
        params = {
            "format": "json",
            "size": 100,
            "page": page,
            "fromDateAccepted": from_date,
            "toDateAccepted": to_date,
        }

        retry_count = 0
        success = False
        has_results = False

        while not success and retry_count < max_retries:
            try:
                logger.debug(
                    f"Requesting {from_date} to {to_date}, page {page}, attempt {retry_count + 1}"
                )

                response = session.get(
                    "https://api.openaire.eu/search/software", params=params, timeout=30
                )

                if response.from_cache:
                    cache_hits += 1
                else:
                    cache_misses += 1

                response.raise_for_status()
                data = response.json()

                response_data = data.get("response", {})
                results_data = response_data.get("results")

                if results_data is None or not isinstance(results_data, dict):
                    success = True
                    has_results = False
                    break

                results = results_data.get("result", [])

                if not results or results is None:
                    success = True
                    has_results = False
                    break

                has_results = True

                for item in results:
                    try:
                        metadata = (
                            item.get("metadata", {})
                            .get("oaf:entity", {})
                            .get("oaf:result", {})
                        )

                        title_obj = metadata.get("title", {})
                        if isinstance(title_obj, dict):
                            title = title_obj.get("$", "N/A")
                        elif isinstance(title_obj, list) and len(title_obj) > 0:
                            title = (
                                title_obj[0].get("$", "N/A")
                                if isinstance(title_obj[0], dict)
                                else "N/A"
                            )
                        else:
                            title = "N/A"

                        url_obj = metadata.get("websiteurl", {})
                        if isinstance(url_obj, dict):
                            url = url_obj.get("$")
                        elif isinstance(url_obj, list) and len(url_obj) > 0:
                            url = (
                                url_obj[0].get("$")
                                if isinstance(url_obj[0], dict)
                                else None
                            )
                        else:
                            url = None

                        all_software.append(
                            {
                                "title": title,
                                "year": year,
                                "month": month,
                                "year_month": f"{year}-{month:02d}",
                                "url": url,
                                "pid": item.get("header", {}).get(
                                    "dri:objIdentifier", "N/A"
                                ),
                            }
                        )
                        period_count += 1
                    except Exception as item_error:
                        logger.error(
                            f"Error processing individual item in {from_date}-{to_date}, page {page}"
                        )
                        logger.error(f"Item error: {item_error}")
                        traceback.print_exc()
                        continue

                logger.info(
                    f"    Page {page}: +{len(results)} software (period total: {period_count})"
                )

                # Check if we've hit the 10k limit
                if page * 100 >= 10000:
                    logger.warning(
                        f"    ⚠️  Hit 10k limit for {from_date} to {to_date}!"
                    )
                    hit_limit = True
                    success = True
                    has_results = False
                    break

                success = True

            except Exception as e:
                logger.error(f"\n❌ Error on {from_date}-{to_date}, page {page}:")
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

        page += 1
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
    Recursively fetch software for a date period, splitting if we hit the 10k limit.
    """
    if current_depth >= max_depth:
        logger.error(
            f"    Maximum depth reached for {from_date} to {to_date}. Some data may be missing."
        )
        return 0, cache_hits, cache_misses

    period_count, hit_limit, cache_hits, cache_misses = fetch_software_for_period(
        from_date, to_date, year, month, all_software, cache_hits, cache_misses
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
    """Get software month by month to bypass the 10k limit"""
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

            logger.info(f"Total for {year}-{month:02d}: {month_count} software")

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


if __name__ == "__main__":
    import sys
    import argparse

    # Setup argument parser
    parser = argparse.ArgumentParser(
        description="Scrape software metadata from OpenAIRE API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python openaire.py                    # Use defaults (1960 to current year)
  python openaire.py --start-year 2000  # Start from year 2000
  python openaire.py --end-year 2020    # End at year 2020
  python openaire.py -s 2010 -e 2020    # Custom range
  python openaire.py -o results.csv     # Custom output CSV file
  python openaire.py -p plot.png        # Generate a plot of software per year
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
        logger.info("Starting OpenAIRE software scraping (MONTH BY MONTH)")
        logger.info(
            "This will make more API calls but avoid hitting limits per time period"
        )
        logger.info(f"Year range: {args.start_year} to {args.end_year}")
        print('A')

        df = get_openaire_software_by_month(args.start_year, args.end_year)
        print('B')
        # Save to CSV
        output_file = args.output
        df.to_csv(output_file, index=False)
        logger.info(f"\n✓ Saved results to {output_file}")
        print('C')
        logger.info(args.plot)
        print('D')
        # Generate plot if requested
        if args.plot:
            if not PLOTTING_AVAILABLE:
                logger.warning("\n⚠️  Cannot generate plot: matplotlib is not installed")
                logger.info("Install it with: pip install matplotlib")
            else:
                try:
                    logger.info(f"\nGenerating plot: {args.plot}")
                    year_counts = df.groupby("year").size().sort_index()

                    fig, ax = plt.subplots(figsize=(12, 10))
                    ax.bar(
                        year_counts.index,
                        year_counts.values,
                        color="steelblue",
                        edgecolor="black",
                    )
                    ax.set_xlabel("Year", fontsize=12)
                    ax.set_ylabel("Number of new software products", fontsize=12)

                    # Dynamic title with year range and subtitle with generation date
                    title_text = f"New Research Software Products per year of publication {args.start_year}-{args.end_year}"
                    subtitle_text = f"source: OpenAIRE API, retrieved on {datetime.now().strftime('%Y-%m-%d')}"
                    ax.set_title(
                        f"{title_text}\n{subtitle_text}", fontsize=12, fontweight="bold"
                    )

                    ax.grid(axis="y", alpha=0.3, linestyle="--")

                    # Add value labels at top of bars for recent years
                    recent_years = year_counts[
                        year_counts.index >= year_counts.index.max() - 10
                    ]
                    for year, count in recent_years.items():
                        ax.text(
                            year,
                            count,
                            str(count),
                            ha="center",
                            va="bottom",
                            fontsize=8,
                        )

                    plt.tight_layout()
                    plt.savefig(args.plot, dpi=300, bbox_inches="tight")
                    plt.close()
                    logger.info(f"✓ Plot saved to {args.plot}")
                except Exception as plot_error:
                    logger.error(f"\n❌ Error generating plot: {plot_error}")
                    traceback.print_exc()

        logger.info(f"\n{'=' * 60}")
        logger.info("=== Summary ===")
        logger.info(f"{'=' * 60}")
        logger.info(f"Total software: {len(df)}")

        # By year
        logger.info("\nBy year:")
        year_counts = df.groupby("year").size().sort_index()
        for year, count in year_counts.items():
            logger.info(f"  {year}: {count}")

        # By month (for recent years)
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
