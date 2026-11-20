"""
The broker's credential store: the key it holds for each service you connected.

Each key is a file of its own, `<service>.json`, in a folder on a volume only the
broker's container mounts (`maji-credentials` under compose). The folder is the
broker's alone (mode 700) and each file is too (mode 600). A file is written whole to a
temporary name beside it and renamed into place, so a key is never half written.

A key reaches the store from the Connect decision you signed (`connections.py`). It
leaves the store only in the requests the broker makes to the service's own host, and
never appears in a result, an error, the run log or a report to the control plane:
those name a key by its fingerprint.
"""

# system imports
import contextlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SERVICE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass(frozen=True)
class Credential:
    """
    The key held for one service, and what connected it.

    Attributes:
        key: the key, sent only to the service's host
        settings: what was given with the key, none of it secret, such as an
            organization slug
        fingerprint: the key's SHA-256, as the decision you signed names it
        connected_at: when the key was stored
        decision: the Connect decision that brought the key
        item: the Connect item that decision was made on
    """

    key: str = field(repr=False)
    settings: dict[str, str]
    fingerprint: str
    connected_at: str
    decision: str
    item: str

    def described(self) -> dict[str, object]:
        """The connection as the control plane is told of it: everything but the key."""
        return {
            "status": "connected",
            "fingerprint": self.fingerprint,
            "settings": self.settings,
            "connected_at": self.connected_at,
            "decision": self.decision,
            "item": self.item,
        }


class CredentialStore:
    """
    The keys the broker holds, one file for each service.

    Args:
        folder: where the files are kept; made, readable only by the broker, if it
            isn't there
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.stat().st_mode & 0o077:
            folder.chmod(0o700)

    def __repr__(self) -> str:
        return f"CredentialStore({self.folder})"

    def _file(self, service: str) -> Path:
        if not SERVICE_NAME.fullmatch(service):
            raise ValueError(f"{service!r} isn't a service's name")
        return self.folder / f"{service}.json"

    def get(self, service: str) -> Credential | None:
        """The key held for a service, or None."""
        path = self._file(service)
        if not path.is_file():
            return None
        return Credential(**json.loads(path.read_text()))

    def put(self, service: str, credential: Credential) -> None:
        """Hold a key for a service, in place of any it held."""
        path = self._file(service)
        fd, name = tempfile.mkstemp(dir=self.folder, prefix=f".{service}.")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(asdict(credential), out)
            os.chmod(name, 0o600)
            os.replace(name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)
            raise

    def delete(self, service: str) -> bool:
        """Forget a service's key. Returns whether one was held."""
        try:
            self._file(service).unlink()
        except FileNotFoundError:
            return False
        return True
