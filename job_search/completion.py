"""CLI entry point for deterministic pre-filter job data completion.

The implementation remains in ``repair_reviews`` for backward compatibility
with existing automation and repair audit history.
"""

from job_search.repair_reviews import main


if __name__ == "__main__":
    raise SystemExit(main())
