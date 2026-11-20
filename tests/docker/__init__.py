"""
Tests that need a Docker daemon and maji-mcp's two images (`make test-docker`).

`make test-docker` builds the images under the tag `test` and names them here through
the environment, so the tests never rebuild or run the `latest` images a stack on this
machine uses. Run by hand, the tests fall back to `latest`:

    make images
    uv run pytest -m docker
"""

# system imports
import os

SERVICE_IMAGE = os.environ.get("MAJI_SERVICE_IMAGE", "maji-service:latest")
RUNTIME_IMAGE = os.environ.get("MAJI_RUNTIME_IMAGE", "maji-python-runtime:latest")
