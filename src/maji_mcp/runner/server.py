"""
The runner service: runs scripts on the gateway's behalf.

It serves one Unix socket, on a volume only the gateway mounts. A `run` request names
the principal, the runtime and the script, and any paths the script needs mounted.
The runner then:

1. resolves each path, as the Docker daemon will see it, and refuses places never
   mounted (`runner/mounts.py`)
2. registers the run with the broker, which checks the principal and whether grants
   cover the paths. A path about to be put to you comes back to be looked through
   first, for what a mount of it would leave out (`covers_in`), and the run is
   registered again with what was found. If the paths aren't granted, the broker
   raises a queue item and the run ends here, unstarted, naming the item. Otherwise
   it returns the run's id and token.
3. resolves the paths again, works out what each mount leaves out now, and runs the
   script in a fresh executor container (`docker_runner.py`) with the paths bound
   read-only, the files named like secrets and the places on your never-mount list
   covered, and the token as its only secret
4. finishes the run with the broker, which revokes the token, logs the outcome,
   whether the run succeeded, failed or was killed, and says what the run called

The runner is the only component that holds the Docker API.

Run it with:

    maji-runner --socket-dir /run/maji-runner --broker-control /run/maji-control/control.sock \\
        --broker-volume maji-broker-call --mount-root "$HOME" \\
        --secrets-dir ~/.config/maji-mcp/secrets \\
        --never-mount-file ~/.config/maji-mcp/never-mount --home "$HOME"
"""

# system imports
import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

# project imports
from maji_mcp.common.protocol import MajiError, UnknownMethod, call_unix, serve_unix
from maji_mcp.common.stopping import until_stopped
from maji_mcp.runner.docker_runner import (
    CoverLost,
    DockerRunner,
    RunnerStopping,
    RunResult,
)
from maji_mcp.runner.mounts import (
    LeftOut,
    MountRules,
    covers_in,
    named,
    resolve,
    resolve_all,
)
from maji_mcp.runner.sandbox import (
    CEILING,
    DEFAULT_LIMITS,
    MAX_COVERS,
    Limits,
    Mount,
    SandboxSettings,
)

MAX_SCRIPT_BYTES = 512 * 1024

# How long looking through a run's paths may take, for all of them at once: the agent
# hears nothing until it ends, and a large folder on Docker's file share is slow to
# list: about 10,000 entries a second, measured with Rancher Desktop. A folder that
# takes longer isn't mounted.
#
LOOK_SECONDS = 4.0

# How many of the files a mount leaves out are named: on the item that asks you for
# the path, and in the run's result.
#
MAX_NAMED = 20

# How long a stopping runner waits for the runs it has ended to be finished with the
# broker and answered: half of the ten seconds Docker allows a container before
# killing it.
#
DRAIN_SECONDS = 5.0


###############################################################################
#
class RunnerService:
    """
    Serves `run` requests.

    Args:
        runner: starts executor containers
        broker_control: the broker's control socket
        concurrency: how many runs may be in flight at once
        mount_rules: where mounts may come from; without them, nothing is mounted
    """

    def __init__(
        self,
        runner: DockerRunner,
        broker_control: Path,
        concurrency: int = 4,
        mount_rules: MountRules | None = None,
    ) -> None:
        self.runner = runner
        self.broker_control = broker_control
        self._slots = asyncio.Semaphore(concurrency)
        self.mount_rules = mount_rules
        self.in_flight = 0

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        """Answer one request on the runner's socket, counting the runs in flight."""
        if method == "run":
            self.in_flight += 1
            try:
                return await self.run(params)
            finally:
                self.in_flight -= 1
        if method == "runtimes":
            return sorted(self.runner.settings.images)
        raise UnknownMethod(method)

    async def finish_left(self, run_ids: list[str]) -> None:
        """
        Tell the broker that runs an earlier runner left are over, so their tokens
        end.

        A broker that isn't up yet, or that has restarted since, holds no token for
        such a run, so a call that fails changes nothing and is left at that.

        Args:
            run_ids: the runs whose executors `DockerRunner.sweep` removed
        """
        for run_id in run_ids:
            try:
                await call_unix(
                    self.broker_control,
                    "finish_run",
                    {"run_id": run_id, "exit_code": None, "duration_ms": 0},
                )
            except MajiError, OSError:
                continue

    async def drained(self, within: float) -> bool:
        """
        Wait until no run asked for on the socket is still in flight.

        Args:
            within: how many seconds to wait at most

        Returns:
            Whether none is in flight.
        """
        deadline = time.monotonic() + within
        while self.in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return not self.in_flight

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Run one script for a principal, and return what it produced.

        `session`, when the gateway sends one, is the key the broker keeps the
        session's saved values under; the runner passes it on as it is. `paths` are
        what the script needs mounted, and `why` the agent's words on why, for the
        item that asks you for them. `client` is what the agent's app said it is,
        passed on for that item too.

        Returns:
            What the run produced, with `mounts`, the name each path is mounted
            under, and `called`, the stubs and skills it called, from the broker's
            run log. Or, when a path isn't granted yet, only `pending`: the items to
            decide and the paths they ask for; the script hasn't run.

        Raises:
            MajiError: `invalid_argument` for a missing or oversized script, an
                unknown runtime or a path that can't be mounted, `conflict` for a
                path that changed since it was granted, `unavailable` when the
                runner is stopping, and whatever the broker says when registering
        """
        principal = params.get("principal")
        runtime = params.get("runtime", "python")
        code = params.get("code")
        if not isinstance(principal, str) or not principal:
            raise MajiError("invalid_argument", "principal is required")
        if not isinstance(code, str) or not code.strip():
            raise MajiError("invalid_argument", "code is required")
        if len(code.encode()) > MAX_SCRIPT_BYTES:
            raise MajiError(
                "invalid_argument", f"scripts are limited to {MAX_SCRIPT_BYTES} bytes"
            )
        if runtime not in self.runner.settings.images:
            raise MajiError(
                "invalid_argument",
                f"unknown runtime {runtime!r}",
                {"runtimes": sorted(self.runner.settings.images)},
            )
        limits = _limits(params.get("timeout_s"))
        registering: dict[str, Any] = {
            "principal": principal,
            "runtime": runtime,
            "script_sha256": hashlib.sha256(code.encode()).hexdigest(),
        }
        if params.get("session") is not None:
            registering["session"] = params["session"]
        rules = self.mount_rules.current() if self.mount_rules else None
        resolved = self._resolve(params.get("paths"), rules)
        if resolved:
            registering["mounts"] = [str(p) for p in resolved]

            # A file named like a secret is read only on a grant of its own: the grant
            # on a folder holding it covers the folder's mount, which leaves it out.
            #
            assert rules is not None  # `_resolve` used them to resolve
            alone = await asyncio.to_thread(_asked_by_name, resolved, rules)
            if alone:
                registering["exact"] = alone
        if params.get("why") is not None:
            registering["why"] = params["why"]
        if params.get("client") is not None:
            registering["client"] = params["client"]

        registration = await call_unix(self.broker_control, "register_run", registering)
        for _ in range(2):
            if "scan" not in registration:
                break
            assert rules is not None
            found = await asyncio.to_thread(
                _look, resolved, registration["scan"], rules
            )
            registering["findings"] = {**registering.get("findings", {}), **found}
            registration = await call_unix(
                self.broker_control, "register_run", registering
            )
        if "scan" in registration:
            raise MajiError(
                "internal", "the broker kept asking for paths to be looked at"
            )
        if "pending" in registration:
            return {"pending": registration["pending"]}
        run_id, token = registration["run_id"], registration["token"]
        result: RunResult | None = None
        mounts: list[Mount] = []
        left_out: dict[str, LeftOut] = {}
        finished: dict[str, Any] = {}
        try:
            mounts = self._granted(resolved, registration.get("mounts") or [], rules)
            if mounts:
                assert rules is not None
                left_out = await asyncio.to_thread(_left_out, mounts, rules)
            covers = [
                cover for name, out in left_out.items() for cover in out.covers(name)
            ]
            async with self._slots:
                try:
                    result = await asyncio.to_thread(
                        self.runner.run,
                        runtime,
                        code,
                        run_id,
                        token,
                        limits,
                        mounts,
                        {m.source for m in mounts},
                        covers,
                    )
                except CoverLost:
                    raise MajiError(
                        "conflict",
                        "a file in a mounted folder changed while the run was being "
                        "set up, so nothing ran; run it again",
                    ) from None
                except RunnerStopping:
                    raise MajiError(
                        "unavailable",
                        "the runner is stopping, so the script didn't finish; run it "
                        "again once the runner is back",
                    ) from None
        finally:
            finished = await call_unix(
                self.broker_control,
                "finish_run",
                {
                    "run_id": run_id,
                    "exit_code": result.exit_code if result else None,
                    "duration_ms": result.duration_ms if result else 0,
                    "timed_out": result.timed_out if result else False,
                    "output_bytes": len(result.stdout) if result else 0,
                },
            )
        return {
            "run_id": run_id,
            **result.as_dict(),
            "mounts": {m.name: m.source for m in mounts},
            "left_out": {
                name: {"files": list(out.secrets[:MAX_NAMED]), "count": out.count}
                for name, out in left_out.items()
                if out.count
            },
            "called": finished.get("called") or [],
        }

    def _resolve(self, paths: object, rules: MountRules | None) -> list[Path]:
        """Resolve the paths a run asks for, or refuse them all."""
        if paths is None:
            return []
        if rules is None:
            raise MajiError("invalid_argument", "this runner mounts no paths")
        if rules.problem is not None:
            print(f"runner: no mounts until it is fixed: {rules.problem}", flush=True)
        return resolve_all(paths, rules)

    def _granted(
        self, resolved: list[Path], granted: list[str], rules: MountRules | None
    ) -> list[Mount]:
        """
        Name the paths to bind, once the broker has granted them.

        The broker grants exactly the paths the runner resolved, or the run doesn't
        start. Each is resolved again here, just before its container is created, so
        a path swapped for a symlink since it was granted isn't bound.

        Raises:
            MajiError: `internal` when the broker granted other paths than asked,
                and `conflict` when a path now resolves somewhere else
        """
        if set(granted) != {str(p) for p in resolved}:
            raise MajiError("internal", "the broker granted other paths than asked")
        if not resolved:
            return []
        assert rules is not None  # `_resolve` used them to resolve
        for path in resolved:
            if resolve(str(path), rules) != path:
                raise MajiError(
                    "conflict", f"{path} changed since it was granted; run it again"
                )
        return named(resolved)


def _look(
    resolved: list[Path], asked: object, rules: MountRules
) -> dict[str, dict[str, object]]:
    """
    Work out, for the paths the broker is about to put to you, what a mount of each
    would leave out, sharing `LOOK_SECONDS` among them.

    Only paths this run resolved are looked through, whatever the broker names.

    Returns:
        By path: `left_out`, up to `MAX_NAMED` of the paths a mount covers, a folder's
        ending in `/`; `count`, how many there are in all; `itself`, whether the path
        is a file named like a secret; and `looked_at`

    Raises:
        MajiError: `invalid_argument` for a folder that can't be mounted with what it
            holds covered (`covers_in`)
    """
    if not isinstance(asked, list):
        raise MajiError("internal", "the broker's paths to look at aren't a list")
    paths = [p for p in resolved if str(p) in asked]
    start = time.monotonic()
    found = {}
    for n, path in enumerate(paths, 1):
        deadline = start + LOOK_SECONDS * n / len(paths)
        out = covers_in(path, rules, deadline, resolved)
        names = [f"{rel}/" if folder else rel for rel, folder in out.listed]
        names += out.secrets
        found[str(path)] = {
            "left_out": names[:MAX_NAMED],
            "count": out.count,
            "itself": out.itself,
            "looked_at": out.looked_at,
        }
    return found


def _left_out(mounts: list[Mount], rules: MountRules) -> dict[str, LeftOut]:
    """
    Work out what each of a run's mounts leaves out, just before its container is
    made, sharing `LOOK_SECONDS` among them.

    Returns:
        By mount name, for the mounts that are folders

    Raises:
        MajiError: `invalid_argument` for a folder that can't be mounted with what it
            holds covered (`covers_in`), or for more to cover in all than a run can
    """
    asked = [Path(m.source) for m in mounts]
    start = time.monotonic()
    out = {}
    for n, mount in enumerate(mounts, 1):
        deadline = start + LOOK_SECONDS * n / len(mounts)
        out[mount.name] = covers_in(Path(mount.source), rules, deadline, asked)
    total = sum(left.count for left in out.values())
    if total > MAX_COVERS:
        raise MajiError(
            "invalid_argument",
            f"together these paths hold {total} files a mount would leave out, more "
            f"than the {MAX_COVERS} a run can: ask for fewer of them at once",
        )
    return out


def _asked_by_name(resolved: list[Path], rules: MountRules) -> list[str]:
    """
    The paths a run asks for that are files named like secrets, each of which needs a
    grant on that very path.
    """
    later = time.monotonic() + LOOK_SECONDS
    return [
        str(path)
        for path in resolved
        if path.is_file() and covers_in(path, rules, later).itself
    ]


def rules_from(
    roots: list[str],
    secrets_dir: str | None,
    never_file: str | None,
    home: str | None,
) -> MountRules | None:
    """
    Build the runner's mount rules from its arguments, or None to mount nothing.

    Raises:
        ValueError: roots to mount from without maji-mcp's secrets folder to keep
            out of them, or a secrets folder that doesn't exist. Either would leave
            the secrets mountable, so the runner doesn't start.
    """
    if not roots:
        return None
    if not secrets_dir:
        raise ValueError(
            "--secrets-dir is required with --mount-root, so maji-mcp's own secrets "
            "are never mounted"
        )
    return MountRules.of(roots, [secrets_dir], never_file, home)


def _listed(rules: MountRules | None) -> str:
    """Say at start-up how many paths your never-mount list holds, or its problem."""
    if rules is None or rules.never_file is None:
        return ""
    now = rules.current()
    if now.problem is not None:
        return f", no mounts until the never-mount list is fixed: {now.problem}"
    return f", {len(now.never)} paths on the never-mount list {rules.never_file}"


def _limits(timeout_s: object) -> Limits:
    """Return the default limits, with the requested timeout if it is within bounds."""
    if timeout_s is None:
        return DEFAULT_LIMITS
    if not isinstance(timeout_s, int | float) or not 0 < timeout_s <= CEILING.timeout_s:
        raise MajiError(
            "invalid_argument", f"timeout_s must be between 0 and {CEILING.timeout_s}"
        )
    return Limits(timeout_s=float(timeout_s))


###############################################################################
#
def main() -> None:
    parser = argparse.ArgumentParser(description="the maji-mcp runner")
    parser.add_argument("--socket-dir", type=Path, default=Path("/run/maji-runner"))
    parser.add_argument(
        "--broker-control", type=Path, default=Path("/run/maji-control/control.sock")
    )
    parser.add_argument("--broker-volume", default=os.environ.get("MAJI_BROKER_VOLUME"))
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="runtime=image, such as python=maji-python-runtime:latest",
    )
    parser.add_argument(
        "--mount-root",
        action="append",
        default=[],
        help="a folder that paths to mount must be inside, seen at its own path",
    )
    parser.add_argument(
        "--secrets-dir",
        help="maji-mcp's own secrets, never mounted; required with --mount-root",
    )
    parser.add_argument(
        "--never-mount-file",
        help="your list of paths never mounted, one a line, read before each run",
    )
    parser.add_argument("--home", help="your home directory, which ~ is in that list")
    args = parser.parse_args()
    images = dict(item.split("=", 1) for item in args.image) or {
        "python": "maji-python-runtime:latest"
    }
    settings = SandboxSettings(images=images, broker_volume=args.broker_volume)
    try:
        rules = rules_from(
            args.mount_root, args.secrets_dir, args.never_mount_file, args.home
        )
    except ValueError as exc:
        parser.error(str(exc))

    async def run() -> None:
        docker_runner = DockerRunner(settings)
        service = RunnerService(docker_runner, args.broker_control, mount_rules=rules)

        # A runner that crashed or was killed couldn't end its scripts. This one
        # removes what that one left, before it takes a run of its own.
        #
        left = await asyncio.to_thread(docker_runner.sweep)
        await service.finish_left(left)
        if left:
            executors = "executor" if len(left) == 1 else "executors"
            print(
                f"runner: removed {len(left)} {executors} an earlier runner left",
                flush=True,
            )
        server = await serve_unix(
            args.socket_dir / "runner.sock", service.handle, mode=0o660
        )
        print(f"runner: runtimes {sorted(images)}{_listed(rules)}, serving", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            # Told to stop. A script is held to its deadline by the runner waiting
            # on it, so none is left running: the runner takes no more connections,
            # ends the scripts in flight, and gives each run the time to be finished
            # with the broker and answered.
            #
            server.close()
            await asyncio.to_thread(docker_runner.stop)
            await service.drained(DRAIN_SECONDS)

    asyncio.run(until_stopped(run()))
    print("runner: stopped", flush=True)


if __name__ == "__main__":
    main()
