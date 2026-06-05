import sys
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
try:
    from app import app
    print(f"App loaded OK from {PROJECT_DIR}", flush=True)
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
except Exception as e:
    print(f"ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()
    input("Press Enter to exit...")
