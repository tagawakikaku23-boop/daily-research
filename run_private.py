"""Run report jobs without publishing portfolio values to public Actions logs."""
import subprocess
import sys


if __name__ == "__main__":
    allowed = {"daily_report.py", "monthly_review.py", "refresh_watchlist.py"}
    if len(sys.argv) < 2 or sys.argv[1] not in allowed:
        sys.exit("Unsupported report script")
    result = subprocess.run([sys.executable, *sys.argv[1:]], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    if result.returncode:
        print("Report failed. Output suppressed because it may contain private financial data.")
        print("Check connection validation and any pending Notion report before retrying.")
    else:
        print("Report finished successfully. Private report details are available in Notion.")
    sys.exit(result.returncode)

