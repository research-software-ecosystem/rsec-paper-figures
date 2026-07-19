import json
import glob
import sqlite3
from pathlib import Path
import pandas as pd
import requests
import requests_cache
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
import argparse
import logging
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(f"biotools_scrape_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Keep caches beside this script, regardless of the directory it is run from.
SCRIPT_DIR = Path(__file__).resolve().parent
DOI_CACHE_PATH = SCRIPT_DIR / 'doi_cache'
PROCESSED_CACHE_PATH = SCRIPT_DIR / 'biotools_processed_cache.sqlite'

# Cache Crossref responses so repeated DOI lookups do not hit the API.
doi_session = requests_cache.CachedSession(str(DOI_CACHE_PATH))
logger.info(f"DOI cache enabled: {DOI_CACHE_PATH}.sqlite")


def get_doi_metadata(doi):
    """
    Fetches metadata for a given DOI using the Crossref API with caching.
    
    Parameters:
        doi (str): The DOI string
        
    Returns:
        dict or None: A dictionary containing the metadata or None if not found.
    """
    if doi is None:
        return None
    
    url = f"https://api.crossref.org/works/{doi}"
    
    try:
        response = doi_session.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        message = data.get("message", {})
        
        # Extract and format date
        date_parts = message.get("published", {}).get("date-parts", [[""]])
        if date_parts and date_parts[0] and date_parts[0][0] is not None:
            date_values = date_parts[0]
            if len(date_values) >= 3:
                year, month, day = date_values[0], date_values[1], date_values[2]
                date_str = f"{year}-{month:02d}-{day:02d}T00:00:00Z"
            elif len(date_values) >= 2:
                year, month = date_values[0], date_values[1]
                date_str = f"{year}-{month:02d}-01T00:00:00Z"
            else:
                year = date_values[0]
                date_str = f"{year}-01-01T00:00:00Z"
        else:
            date_str = None
        
        metadata = {
            "title": message.get("title", [""])[0] if message.get("title") else "",
            "authors": [{"name": author.get("family", "") + ", " + author.get("given", "")}
                         for author in message.get("author", [])],
            "abstract": message.get("abstract", ""),
            "journal": message.get("container-title", [""])[0] if message.get("container-title") else "",
            "date": date_str,
            "citationCount": message.get("is-referenced-by-count", 0),
        }
        
        return metadata
    
    except requests.exceptions.RequestException as e:
        logger.debug(f"Error fetching metadata for DOI {doi}: {e}")
        return None


def extract_info_from_json(json_file):
    """Extract biotoolsID, primary_date, and addition_date from a bio.tools JSON file."""
    with open(json_file, 'r') as file:
        data = json.load(file)
    
    biotoolsID = data.get('biotoolsID')
    
    # Get all publication dates from DOIs
    all_publication_date_strings = []
    for publi in data.get('publication', []):
        if publi is not None and publi.get('doi') is not None:
            doi_metadata = get_doi_metadata(publi.get('doi'))
            if doi_metadata is not None:
                all_publication_date_strings.append(doi_metadata['date'])
    
    # Parse dates and find the earliest publication date
    all_publication_dates = []
    for date_string in all_publication_date_strings:
        if date_string is not None:
            try:
                all_publication_dates.append(datetime.strptime(date_string, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                logger.debug(f"Could not parse date: {date_string}")
    
    earliest_publication_date = None
    if all_publication_dates:
        earliest_publication_date = min(all_publication_dates)
    
    addition_date = pd.to_datetime(data.get('additionDate'), utc=True)
    
    return biotoolsID, earliest_publication_date, addition_date


def process_single_file(filepath):
    """Wrapper function to process a single file and return results."""
    try:
        biotoolsID, primary_date, addition_date = extract_info_from_json(filepath)
        return {
            'file': filepath, 
            'biotoolsID': biotoolsID, 
            'primary_date': primary_date, 
            'addition_date': addition_date
        }
    except Exception as e:
        logger.error(f"Error processing {filepath}: {e}")
        return None


def open_processed_cache(cache_path=PROCESSED_CACHE_PATH):
    """Open the persistent cache of results extracted from bio.tools files."""
    connection = sqlite3.connect(cache_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS file_results (
            filepath TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            biotools_id TEXT,
            primary_date TEXT,
            addition_date TEXT
        )
        """
    )
    return connection


def get_cached_result(connection, filepath):
    """Return a cached result when the source file has not changed."""
    file_stat = Path(filepath).stat()
    row = connection.execute(
        """
        SELECT biotools_id, primary_date, addition_date
        FROM file_results
        WHERE filepath = ? AND mtime_ns = ? AND size = ?
        """,
        (str(Path(filepath).resolve()), file_stat.st_mtime_ns, file_stat.st_size),
    ).fetchone()
    if row is None:
        return None

    biotools_id, primary_date, addition_date = row
    return {
        'file': filepath,
        'biotoolsID': biotools_id,
        'primary_date': primary_date,
        'addition_date': addition_date,
    }


def cache_result(connection, result):
    """Store one processed result together with its source-file signature."""
    filepath = Path(result['file'])
    file_stat = filepath.stat()

    def serialize_date(value):
        if value is None or pd.isna(value):
            return None
        return pd.Timestamp(value).isoformat()

    connection.execute(
        """
        INSERT INTO file_results (
            filepath, mtime_ns, size, biotools_id, primary_date, addition_date
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(filepath) DO UPDATE SET
            mtime_ns = excluded.mtime_ns,
            size = excluded.size,
            biotools_id = excluded.biotools_id,
            primary_date = excluded.primary_date,
            addition_date = excluded.addition_date
        """,
        (
            str(filepath.resolve()),
            file_stat.st_mtime_ns,
            file_stat.st_size,
            result['biotoolsID'],
            serialize_date(result['primary_date']),
            serialize_date(result['addition_date']),
        ),
    )


def clean_crossref_placeholders(df, date_col='primary_date'):
    """Clean publisher deposit errors & placeholders."""
    df[date_col] = pd.to_datetime(df[date_col])
    
    # Crossref-specific false dates (pre-Crossref + common errors)
    false_dates = [
        pd.Timestamp('1900-01-01 00:00:00+00:00'),
        pd.Timestamp('1990-10-01 00:00:00+00:00'),
    ]
    mask = df[date_col].isin(false_dates)
    df[f'{date_col}_clean'] = df[date_col].where(~mask, pd.NaT)
    return df


def plot_combined(df, output_file='biotools-entries-publication-combined.png'):
    """
    Create a combined figure with:
    - Main scatter plot (entry creation vs publication date)
    - Marginal density plots for both dates
    - Color-coded differences between publication and entry creation
    """

    # Calculate the time difference between addition and primary dates
    df['days_difference'] = (df['addition_date'] - df['primary_date']).dt.days

    # Remove rows with NaT values in dates
    df = df.dropna(subset=['primary_date', 'addition_date'])

    # Create JointGrid
    g = sns.JointGrid(data=df, x='primary_date', y='addition_date', 
                    height=10, ratio=4, space=0.2)

    # Main scatter plot
    scatter = g.ax_joint.scatter(df['primary_date'], df['addition_date'], 
                                c=df['days_difference'], cmap='viridis', 
                                s=120, alpha=0.7, edgecolors='black', linewidth=0.5)

    # Marginal density curves
    g.plot_marginals(sns.kdeplot, color='darkblue', fill=True, alpha=0.5, linewidth=1.5)

    # Reference line and formatting (same as before)...
    g.ax_joint.plot([df['primary_date'].min(), df['primary_date'].max()], 
                    [df['primary_date'].min(), df['primary_date'].max()], 
                    'r--', alpha=0.5, label='Same date line', linewidth=2)
    # Calculate both limits
    earliest_addition_date = df['addition_date'].min()
    latest_addition_date = df['addition_date'].max()  # Get the max date

    # Apply to the joint plot axis
    g.ax_joint.set_ylim(bottom=earliest_addition_date, top=latest_addition_date)

    # Add 50% padding so points don't sit exactly on the top edge
    g.ax_joint.margins(y=0.5)

    g.ax_joint.set_xlabel('Primary Date (First Publication)', fontsize=12, fontweight='bold')
    g.ax_joint.set_ylabel('Addition Date (Entry Creation)', fontsize=12, fontweight='bold')
    g.figure.suptitle('Timeline: Entry Creation vs Publication Date',
                fontsize=14, fontweight='bold', y=1.02)

    # DATE FIXING: Move colorbar to the right to avoid marginal plots
    # 1. Reduce the right margin of the subplots to make room for colorbar
    g.figure.subplots_adjust(right=0.85)

    # 2. Create a new axes specifically for the colorbar [left, bottom, width, height]
    cax = g.figure.add_axes([0.87, 0.2, 0.02, 0.6])  # Positioned at 87% from left, spanning 60% height

    # 3. Add colorbar to this new axes
    cbar = g.figure.colorbar(scatter, cax=cax)
    cbar.set_label('Days Difference', fontsize=11, fontweight='bold')

    # Date formatting
    g.ax_joint.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    g.ax_joint.yaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    plt.setp(g.ax_joint.get_xticklabels(), rotation=45)
    plt.setp(g.ax_joint.get_yticklabels(), rotation=45)
    g.ax_joint.legend(fontsize=10, loc='upper left')
    plt.savefig(output_file, format='png', dpi=300, bbox_inches='tight')
    logger.info(f"Combined plot saved to {output_file}")
    plt.close()


def process_biotools_files(data_dir, output_plot='biotools-entries-publication-scatterplot.png',
                          batch_size=100):
    """
    Process bio.tools JSON files and generate scatter plot.
    
    Parameters:
        data_dir (str): Path to the bio.tools data directory
        output_plot (str): Output filename for the scatter plot
        batch_size (int): Number of files to process in each batch
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    glob_pattern = f'{data_dir}/*/*.biotools.json'
    results = []
    cache_hits = 0
    cache_misses = 0
    
    # Get list of files
    file_list = sorted(glob.glob(glob_pattern))
    
    if not file_list:
        logger.error(f"No .biotools.json files found in {data_dir}")
        sys.exit(1)
    
    num_batches = (len(file_list) + batch_size - 1) // batch_size
    logger.info(f"Processing {len(file_list)} files in {num_batches} batches...")
    
    with open_processed_cache() as processed_cache:
        for i in range(0, len(file_list), batch_size):
            batch = file_list[i:i+batch_size]
            batch_num = i // batch_size + 1

            logger.info(f"Processing batch {batch_num}/{num_batches}")

            for filepath in batch:
                result = get_cached_result(processed_cache, filepath)
                if result is not None:
                    cache_hits += 1
                else:
                    cache_misses += 1
                    result = process_single_file(filepath)
                    if result is not None:
                        cache_result(processed_cache, result)

                if result is not None:
                    results.append(result)

            # Checkpoint each batch so an interrupted run can resume here.
            processed_cache.commit()
            logger.info(
                f"Batch {batch_num}/{num_batches} complete "
                f"(cache hits: {cache_hits}, processed: {cache_misses})"
            )
    
    # Load the results into a Pandas DataFrame
    df = pd.DataFrame(results)
    
    df['primary_date'] = pd.to_datetime(df['primary_date'], utc=True)
    df['addition_date'] = pd.to_datetime(df['addition_date'], utc=True)
    
    logger.info(f"Processed {len(df)} entries")
    logger.info(f"Processed-file cache hits: {cache_hits}")
    logger.info(f"Files processed this run: {cache_misses}")
    
    # Clean the data
    df = clean_crossref_placeholders(df)
    df_clean = df.dropna(subset=['primary_date_clean'])
    
    logger.info(f"Entries with valid publications: {len(df_clean)}")
    logger.info(f"Entries without valid publications: {len(df) - len(df_clean)}")
    
    # Generate the combined plot
    logger.info("Generating combined plot...")
    plot_combined(df_clean.copy(), output_plot)
    
    # Print summary statistics
    df_clean = df_clean.copy()
    df_clean['days_difference'] = (df_clean['addition_date'] - df_clean['primary_date']).dt.days
    
    logger.info("\n" + "="*50)
    logger.info("SUMMARY STATISTICS")
    logger.info("="*50)
    logger.info(f"Total entries analyzed: {len(df_clean)}")
    logger.info(f"Average lag (days): {df_clean['days_difference'].mean():.2f}")
    logger.info(f"Median lag (days): {df_clean['days_difference'].median():.2f}")
    logger.info(f"Standard deviation (days): {df_clean['days_difference'].std():.2f}")
    logger.info(f"Min lag (days): {df_clean['days_difference'].min():.2f}")
    logger.info(f"Max lag (days): {df_clean['days_difference'].max():.2f}")
    logger.info(f"\nEntries created before publication: {(df_clean['days_difference'] < 0).sum()}")
    logger.info(f"Entries created after publication: {(df_clean['days_difference'] > 0).sum()}")
    logger.info(f"Entries created same day: {(df_clean['days_difference'] == 0).sum()}")
    logger.info(f"Percentage created after publication: {(df_clean['days_difference'] > 0).sum()/len(df_clean)*100:.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process bio.tools JSON files and plot entry creation dates against publication dates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python biotools_scatter.py --data-dir /path/to/content/data
  python biotools_scatter.py -d /path/to/content/data -o my_plot.png
  python biotools_scatter.py --clear-cache   # Clear all caches and exit
        """,
    )
    parser.add_argument(
        "--data-dir", "-d",
        type=str,
        help="Path to the bio.tools data directory (containing subdirectories with .biotools.json files)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="biotools-entries-publication-combined.png",
        help="Output plot file name (default: biotools-entries-publication-combined.png)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Files per checkpointed batch (default: 100)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the DOI and processed-file caches, then exit",
    )
    
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    
    if args.clear_cache:
        doi_session.cache.clear()
        if PROCESSED_CACHE_PATH.exists():
            PROCESSED_CACHE_PATH.unlink()
        logger.info("DOI and processed-file caches cleared. Exiting.")
        sys.exit(0)

    if not args.data_dir:
        parser.error("--data-dir is required unless --clear-cache is used")
    
    process_biotools_files(args.data_dir, args.output, args.batch_size)
