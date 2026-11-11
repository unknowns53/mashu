"""`python -m mashu`, the same command as the `mashu` script.

On Windows, uv rewrites .venv\\Scripts\\mashu.exe at every version change, and a running
mashu.exe can be neither deleted nor renamed. Agents and the desktop app there start Mashu
through python.exe instead, which uv leaves alone unless the Python version changes.
"""

import sys

from mashu.cli import main

if __name__ == "__main__":
    sys.exit(main())
