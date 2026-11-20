"""Where things are in the repository, for tests that read its files."""

# system imports
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC_FILE = REPO / "api" / "openapi.yaml"
MAJI_MODULE = REPO / "src" / "maji_mcp" / "executor" / "maji.py"
FAKE_SERVER = REPO / "tests" / "support" / "fake_stdio_server.py"
HEADERS_HELPER = REPO / "bin" / "maji-headers"
RUNTIME_REQUIREMENTS = REPO / "images" / "python-runtime" / "requirements.in"
RUNTIME_LOCK = REPO / "images" / "python-runtime" / "requirements.txt"
RUN_MODULE = REPO / "src" / "maji_mcp" / "executor" / "run.py"
