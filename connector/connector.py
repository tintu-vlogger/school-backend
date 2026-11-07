"""Start the attendance connector (also the entry point of the Windows .exe)."""
import sys

from hrms_connector.cli import main

if __name__ == "__main__":
    sys.exit(main())
