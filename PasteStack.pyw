"""Double-click to open the app without a console window."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clipapp.ui import main  # noqa: E402

main()
