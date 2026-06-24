#!/bin/bash
# Taclaco Dashboard Launcher
# Double-click this file in Finder to launch the dashboard.

# Move into the folder where this script lives (handles iCloud paths with spaces)
cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "  Launching Taclaco Dashboard..."
echo "  Folder: $(pwd)"
echo "================================================"
echo ""

# Activate virtual environment
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    echo "[OK] Virtual environment activated."
else
    echo "[WARN] venv/bin/activate not found - using system Python."
fi

# Verify streamlit is available
if ! command -v streamlit >/dev/null 2>&1; then
    echo ""
    echo "[ERROR] streamlit is not installed in this environment."
    echo "Install it with:  pip install streamlit"
    echo ""
    echo "Press any key to close this window..."
    read -n 1 -s
    exit 1
fi

# Launch the dashboard (this opens it in your default browser automatically)
echo ""
echo "Starting Streamlit... (your browser should open automatically)"
echo "When you're done, close the browser tab and press Ctrl+C in this"
echo "window to stop the dashboard."
echo ""

streamlit run dashboard.py

# If streamlit exits, give the user a moment before the Terminal window closes
echo ""
echo "Dashboard stopped. Press any key to close this window..."
read -n 1 -s
