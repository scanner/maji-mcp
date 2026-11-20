"""
The control plane's service: the REST API and web client for you, and an internal
socket each for the broker and the gateway, over one database.

Both run on one event loop, which owns the database connection (`store.py`). Every 30
seconds, items past their expiry go back to their requesters.

    maji-control --db /var/lib/maji-control/control.db \\
        --instance-file /run/secrets/instance --clients /run/secrets/control_clients \\
        --origin http://localhost:8767 --host 0.0.0.0 --port 8767

`maji-control setup-code` prints a one-time code for registering the first passkey,
and refuses once a passkey exists. `make up` runs it in the container and opens the
setup page with the code in the link's fragment.

`maji-control backup` writes a copy of the database to stdout, taken with SQLite's
online backup while the service runs; `maji-control restore` reads one from stdin and
puts it in place, with the service stopped (`make backup`, `make restore`).
"""

# system imports
import argparse
import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

# 3rd party imports
import uvicorn

# project imports
from maji_mcp.control import demo as demo_queue
from maji_mcp.control import internal
from maji_mcp.control.api import Settings, build_app
from maji_mcp.control.demo import DEMO, seed
from maji_mcp.control.ids import LABEL
from maji_mcp.control.passkeys import CODE_ALPHABET
from maji_mcp.control.store import ControlStore

SWEEP_SECONDS = 30
SHUTDOWN_SECONDS = 5


def read_instance(path: Path) -> str:
    """The instance id `maji-setup` recorded from the host's name."""
    instance = path.read_text().strip()
    if not LABEL.match(instance):
        raise SystemExit(f"{path}: {instance!r} isn't an instance id")
    return instance


def read_clients(path: Path) -> dict[str, str]:
    """Each client's name and its token's hash, as `sha256:<hex>`."""
    if not path.exists():
        return {}
    clients = json.loads(path.read_text()).get("clients", {})
    return {str(name): str(digest) for name, digest in clients.items()}


def setup_code(store: ControlStore) -> str | None:
    """A new one-time setup code, or None once a passkey exists."""
    if store.passkey_count():
        return None
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))
    store.add_setup_code(code)
    return code


def backup(db: Path, out: BinaryIO) -> None:
    """Copy the database, consistently while it is in use, and write the copy to `out`."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "backup.db"
        source = sqlite3.connect(str(db))
        target = sqlite3.connect(str(copy))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        out.write(copy.read_bytes())


def restore(db: Path, instance: str, data: bytes) -> None:
    """
    Put a backup in place of the database, with the service stopped.

    The backup is written beside the database and checked there, then renamed over
    it in one step, so a refused or failed restore leaves the database as it was.

    Raises:
        SystemExit: the backup isn't a sound database, or belongs to another instance
    """
    fd, name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=db.parent)
    incoming = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        check = sqlite3.connect(str(incoming))
        try:
            sound = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            owner = check.execute(
                "SELECT value FROM meta WHERE key = 'instance'"
            ).fetchone()
        except sqlite3.DatabaseError:
            sound, owner = False, None
        finally:
            check.close()
        if not sound:
            raise SystemExit("control: that isn't a sound control plane database")
        if owner is None or owner[0] != instance:
            found = owner[0] if owner else "no instance"
            raise SystemExit(f"control: that backup belongs to {found}, not {instance}")

        # The old database's write-ahead log would be replayed onto the restored file,
        # so it goes with the file it belonged to.
        #
        for stale in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            stale.unlink(missing_ok=True)
        os.replace(incoming, db)
    finally:
        incoming.unlink(missing_ok=True)


async def serve(
    store: ControlStore,
    settings: Settings,
    host: str,
    port: int,
    broker_dir: Path | None,
    gateway_dir: Path | None,
) -> None:
    """
    Serve the REST API, and the internal sockets when given their directories, until
    cancelled. The demo has no broker or gateway behind it, so it serves no sockets.
    """
    if broker_dir is not None and gateway_dir is not None:
        await internal.serve(store, broker_dir, gateway_dir)
    stopping = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(store, settings, stopping),
            host=host,
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=SHUTDOWN_SECONDS,
        )
    )

    async def sweep() -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            store.expire_due()

    # uvicorn's stop waits for every open connection to close, and an open queue tab
    # holds the event stream open until told to end. Its signal handlers set
    # `should_exit`, so seeing that set tells the streams to end; the graceful
    # shutdown timeout bounds the wait for anything else.
    #
    async def end_streams() -> None:
        while not server.should_exit:
            await asyncio.sleep(0.1)
        stopping.set()

    tasks = [asyncio.create_task(sweep()), asyncio.create_task(end_streams())]
    try:
        print(f"control: {store.instance}, serving {settings.origin}", flush=True)
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()


def demo(
    directory: Path, port: int, web_dir: Path, *, reset: str | None = None
) -> None:
    """
    Run the demo queue: a control plane of its own, seeded with sample requests.

    Its database belongs to the `demo` instance, so a reset can only ever remove the
    demo's own data, and opening anything else as the demo is refused.

    Args:
        reset: `queue` to start the queue afresh, keeping its passkeys; `all` to
            remove those too
    """
    if directory is None:
        raise SystemExit("control: the demo needs --dir")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = directory / "control.db"
    if reset is not None:
        demo_queue.reset(db, passkeys=reset != "all")
    store = ControlStore(db, DEMO)
    made = seed(store)
    origin = f"http://localhost:{port}"
    code = setup_code(store)
    link = f"{origin}/setup#code={code}" if code else f"{origin}/queue"
    if made:
        print(f"control: the demo queue has {len(made)} sample requests", flush=True)
    opener = shutil.which("open") or shutil.which("xdg-open")
    if sys.stdout.isatty() and opener:
        subprocess.run([opener, link], check=False)
        print(
            f"control: opened {origin}; nothing in the demo is ever applied", flush=True
        )
    else:
        print(f"control: the demo is at {link}", flush=True)
    settings = Settings(origin=origin, rp_id="localhost", web_dir=web_dir)
    asyncio.run(serve(store, settings, "127.0.0.1", port, None, None))


def main() -> None:
    # Once uvicorn has shut down on Ctrl-C, it raises SIGINT again, which asyncio
    # turns into KeyboardInterrupt. Stopping is what was asked for, so it ends without
    # a traceback, with the exit status a shell expects after Ctrl-C.
    #
    try:
        _run()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _run() -> None:
    parser = argparse.ArgumentParser(description="the maji-mcp control plane")
    parser.add_argument(
        "--db", type=Path, default=Path("/var/lib/maji-control/control.db")
    )
    parser.add_argument(
        "--instance-file", type=Path, default=Path("/run/secrets/instance")
    )
    parser.add_argument(
        "--clients", type=Path, default=Path("/run/secrets/control_clients")
    )
    parser.add_argument("--origin", default="http://localhost:8767")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--web-dir", type=Path, default=Path("/app/web"))
    parser.add_argument(
        "--broker-dir", type=Path, default=Path("/run/maji-plane-broker")
    )
    parser.add_argument(
        "--gateway-dir", type=Path, default=Path("/run/maji-plane-gateway")
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "setup-code", "backup", "restore", "demo"],
        default="serve",
        help=(
            "serve (the default); print a setup code for the first passkey; write a "
            "backup to stdout, or restore one from stdin with the service stopped; or "
            "run the demo queue"
        ),
    )
    parser.add_argument("--dir", type=Path, help="demo: where the demo keeps its data")
    parser.add_argument(
        "--reset",
        nargs="?",
        const="queue",
        choices=["queue", "all"],
        help="demo: start its queue afresh, keeping its passkeys; `all` removes those too",
    )
    args = parser.parse_args()
    if args.command == "demo":
        demo(args.dir, args.port, args.web_dir, reset=args.reset)
        return
    instance = read_instance(args.instance_file)
    if args.command == "backup":
        backup(args.db, sys.stdout.buffer)
        return
    if args.command == "restore":
        restore(args.db, instance, sys.stdin.buffer.read())
        print("control: restored", file=sys.stderr)
        return
    store = ControlStore(args.db, instance)

    if args.command == "setup-code":
        code = setup_code(store)
        if code is None:
            print(
                "control: a passkey is registered already; no setup code",
                file=sys.stderr,
            )
            raise SystemExit(3)
        print(code)
        return

    store.set_client_tokens(read_clients(args.clients))
    settings = Settings(
        origin=args.origin,
        rp_id=urlsplit(args.origin).hostname or "localhost",
        web_dir=args.web_dir,
    )
    asyncio.run(
        serve(
            store,
            settings,
            args.host,
            args.port,
            args.broker_dir,
            args.gateway_dir,
        )
    )


if __name__ == "__main__":
    main()
