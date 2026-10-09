"""Execute every SQL file in sql/ against PostgreSQL and save the results.

Can be used two ways:

* as a library -- ``from run_queries import run_all_queries`` (run_all.py does
  this, then renders reports/sql_report.md from the returned DataFrames);
* standalone  -- ``python run_queries.py`` starts a server, runs the queries
  against whatever is already loaded, and writes the CSVs.

Output: ``reports/query_results/NN_name.csv``, one file per query.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import PROJECT_ROOT, Database, query_files, read_query  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "reports" / "query_results"
SQL_DIR = PROJECT_ROOT / "sql"


@dataclass
class QueryResult:
    """One executed query: its metadata, its rows, and where it was written."""

    name: str                     # e.g. "01_monthly_revenue"
    path: Path                    # the .sql file
    question: str                 # business-question header from the file
    sql: str                      # executable body
    dataframe: pd.DataFrame
    elapsed_s: float = 0.0
    csv_path: Path | None = None
    error: str | None = None

    @property
    def row_count(self) -> int:
        return int(len(self.dataframe))

    @property
    def columns(self) -> list[str]:
        return list(self.dataframe.columns)

    @property
    def ok(self) -> bool:
        return self.error is None

    def head_markdown(self, n: int = 12) -> str:
        """Markdown table of the first ``n`` rows (real query output)."""
        return to_markdown(self.dataframe.head(n))

    def total_markdown(self) -> str:
        return to_markdown(self.dataframe)


def to_markdown(frame: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavoured markdown table.

    Implemented locally rather than via ``DataFrame.to_markdown`` so the report
    has no optional ``tabulate`` dependency.
    """
    if frame.empty:
        return "_(no rows returned)_"

    def fmt(value) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, (bool,)):
            return "true" if value else "false"
        # numpy scalars: check the integer case before the float case so that a
        # count column renders as "434" and not "434.00".
        if isinstance(value, float):
            if value != value:  # NaN
                return ""
            if value == 0:
                return "0.00"
            if abs(value) < 0.01:
                return f"{value:,.4f}"
            return f"{value:,.2f}"
        if isinstance(value, int):
            return f"{value:,}"
        if hasattr(value, "item") and not isinstance(value, (str, bytes)):
            # numpy scalar -> python scalar, then re-dispatch
            return fmt(value.item())
        return str(value).replace("|", "\\|")

    columns = [str(c) for c in frame.columns]
    lines = ["| " + " | ".join(columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    # itertuples keeps each cell's own dtype; iterrows() would upcast a row of
    # mixed numeric columns to float64 and turn every integer into "1.00".
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)


def run_query(db, path: Path, save: bool = True,
              out_dir: Path | None = None) -> QueryResult:
    """Run a single .sql file and optionally persist its result to CSV."""
    question, sql = read_query(path)
    result = QueryResult(name=path.stem, path=path, question=question, sql=sql,
                         dataframe=pd.DataFrame())
    started = time.time()
    try:
        result.dataframe = db.fetch_dataframe(sql)
    except Exception as exc:  # noqa: BLE001 - surfaced in the report
        result.error = f"{exc.__class__.__name__}: {exc}".strip()
        raise
    finally:
        result.elapsed_s = time.time() - started

    if save:
        directory = Path(out_dir) if out_dir else RESULTS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        csv_path = directory / f"{path.stem}.csv"
        result.dataframe.to_csv(csv_path, index=False)
        result.csv_path = csv_path
    return result


def run_all_queries(db, save: bool = True,
                    out_dir: Path | None = None,
                    verbose: bool = True) -> list[QueryResult]:
    """Run every numbered .sql file in sql/, in order."""
    files = query_files(SQL_DIR)
    if not files:
        raise FileNotFoundError(f"No .sql files found in {SQL_DIR}")

    results: list[QueryResult] = []
    if verbose:
        print(f"\n-- running {len(files)} queries --")
    for path in files:
        try:
            result = run_query(db, path, save=save, out_dir=out_dir)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  FAIL {path.name}: {exc}")
            raise
        results.append(result)
        if verbose:
            print(f"  ok   {path.stem:<28} {result.row_count:>6,} rows "
                  f"x {len(result.columns):>2} cols  {result.elapsed_s:5.2f}s"
                  + (f"  -> {result.csv_path.name}" if result.csv_path else ""))
    return results


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = RESULTS_DIR

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    with Database() as db:
        version = db.server_version()
        print(f"PostgreSQL {version}  ({db.backend})")
        print(f"target: {db.describe_target()}")
        results = run_all_queries(db, save=True, out_dir=out_dir)

    print(f"\n{len(results)} queries executed; CSVs in "
          f"{out_dir.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
