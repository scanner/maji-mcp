"""
The sandbox every executor container runs in: the fixed flags, and the check on them.

The runner is the only component that holds the Docker API, and agent code never reaches
it (`docs/design/deployment.md`). A run request names a runtime and carries a script.
Everything else about the container is decided here, the same way every time:

- no network (`--network none`)
- a read-only root filesystem, with a tmpfs for the workspace and `/tmp`
- a non-root user, all capabilities dropped, `no-new-privileges`, never privileged
- memory, CPU, process-count and time limits
- one volume, the broker's socket, read-only
- the paths the run was granted, each bound read-only at `/mnt/<name>`, and no other
  host path. Docker on a Mac shares your home directory into its VM, so a bind mount
  the broker didn't grant could reach any of your files.
- covers over the paths inside a granted folder that a script isn't to read, such as
  a `.env` file: each reads as empty (`Cover`)

`container_config` builds the arguments for the Docker SDK's `containers.create`, and
`validate` checks that exact dictionary before it is sent. They are separate on
purpose: a mistake in the builder, or a future option added carelessly, is refused by
the check rather than run.
"""

# system imports
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

WORKSPACE = "/work"
SOCKET_DIR = "/run/maji"
MOUNT_DIR = "/mnt"
RUN_USER = "10001:10001"

# What a cover is made of. A file is covered by the kernel's empty device, bound over
# it, and a folder by an empty file system of its own that nothing can be written to.
# Neither holds anything of yours, so a cover can only take away what a script reads.
#
COVER_SOURCE = "/dev/null"
COVER_TMPFS = {"SizeBytes": 4096, "Mode": 0o555}

# How many covers one run may have. Each is a mount of its own, and a folder with more
# files named like secrets than this is asked for by a folder inside it.
#
MAX_COVERS = 200

# A mount's name is the last part of its path in the executor, so it is one plain
# segment: no separators, and nothing that reads as `.` or `..`.
#
MOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# The keys `container_config` may produce. Anything else, such as `privileged`,
# `volumes`, `devices`, `cap_add`, `pid_mode` or `network`, is refused.
#
ALLOWED_KEYS = frozenset(
    {
        "image",
        "stdin_open",
        "user",
        "network_mode",
        "read_only",
        "tmpfs",
        "cap_drop",
        "security_opt",
        "mem_limit",
        "memswap_limit",
        "nano_cpus",
        "pids_limit",
        "environment",
        "mounts",
        "labels",
        "log_config",
        "working_dir",
    }
)


###############################################################################
#
@dataclass(frozen=True)
class Limits:
    """
    The resources one run may use.

    The defaults suit a script that fetches and filters data. `ceiling` holds the most
    a request may ask for; the validator refuses anything above it.
    """

    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    timeout_s: float = 60.0
    workspace_mb: int = 64
    output_bytes: int = 64 * 1024


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Mount:
    """
    A granted path, bound read-only into the executor at `/mnt/<name>`.

    Args:
        source: the resolved absolute path on the host (`runner/mounts.py`)
        name: the folder or file name the script sees under `/mnt`
    """

    source: str
    name: str

    @property
    def target(self) -> str:
        return f"{MOUNT_DIR}/{self.name}"


@dataclass(frozen=True)
class Cover:
    """
    A path inside a mount that a script reads as empty.

    A granted folder can hold files a script isn't to read: ones named the way secrets
    are kept, and places on your never-mount list. The folder is mounted, and each of
    those is covered inside the executor: a file reads as an empty file, and a folder
    lists as an empty folder. The script sees that the name is there, and nothing in
    it.

    Args:
        mount: the name of the mount the path is in
        path: the path inside the mount, with `/` between its parts
        folder: whether the path is a folder
    """

    mount: str
    path: str
    folder: bool = False

    @property
    def target(self) -> str:
        return f"{MOUNT_DIR}/{self.mount}/{self.path}"


CEILING = Limits(
    memory_mb=4096,
    cpus=4.0,
    pids=512,
    timeout_s=600.0,
    workspace_mb=1024,
    output_bytes=1024 * 1024,
)


@dataclass(frozen=True)
class SandboxSettings:
    """
    What the runner's configuration decides, never the request.

    Args:
        images: the runtime allowlist, each runtime's image pinned by reference
        broker_volume: the named volume holding the broker's call socket, or None for a
            run that may not call stubs at all
    """

    images: dict[str, str] = field(
        default_factory=lambda: {"python": "maji-python-runtime:latest"}
    )
    broker_volume: str | None = None


###############################################################################
#
def container_config(
    settings: SandboxSettings,
    runtime: str,
    run_id: str,
    token: str,
    limits: Limits = DEFAULT_LIMITS,
    mounts: Sequence[Mount] = (),
    covers: Sequence[Cover] = (),
) -> dict[str, Any]:
    """
    Build the `containers.create` arguments for one run.

    Args:
        settings: the runner's own configuration
        runtime: the runtime the request names, such as `python`
        run_id: recorded as a label, so a leftover container can be traced to its run
        token: the run's broker token, the only secret the executor gets. It is valid
            for this run alone, and only for what the run's principal may call.
        limits: the run's resource limits
        mounts: the granted paths to bind, read-only
        covers: the paths inside them that read as empty

    Raises:
        ValueError: the runtime isn't in the allowlist
    """
    if runtime not in settings.images:
        raise ValueError(f"unknown runtime {runtime!r}")
    config: dict[str, Any] = {
        "image": settings.images[runtime],
        "stdin_open": True,
        "user": RUN_USER,
        "network_mode": "none",
        "read_only": True,
        "tmpfs": {
            WORKSPACE: f"rw,nosuid,nodev,size={limits.workspace_mb}m,mode=1777",
            "/tmp": f"rw,nosuid,nodev,size={limits.workspace_mb}m,mode=1777",
        },
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": f"{limits.memory_mb}m",
        "memswap_limit": f"{limits.memory_mb}m",
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "pids_limit": limits.pids,
        "environment": {
            "MAJI_RUN_TOKEN": token,
            "MAJI_BROKER_SOCKET": f"{SOCKET_DIR}/call.sock",
        },
        "mounts": [],
        "labels": {"maji-mcp/run_id": run_id, "maji-mcp/runtime": runtime},
        "log_config": {"type": "json-file", "config": {"max-size": "2m"}},
        "working_dir": WORKSPACE,
    }
    if settings.broker_volume:
        config["mounts"].append(
            {
                "Target": SOCKET_DIR,
                "Source": settings.broker_volume,
                "Type": "volume",
                "ReadOnly": True,
            }
        )

    # A read-only bind is read-only only at its top: a mount beneath the source would
    # come along writable. `NonRecursive` leaves anything mounted beneath it out.
    #
    for mount in mounts:
        config["mounts"].append(
            {
                "Target": mount.target,
                "Source": mount.source,
                "Type": "bind",
                "ReadOnly": True,
                "BindOptions": {"NonRecursive": True},
            }
        )

    # Docker mounts the shallower target first, so each cover lands on the path it
    # names inside its mount, which is there to be mounted on: the mount is read-only,
    # and a cover whose path has gone fails the container's start.
    #
    for cover in covers:
        if cover.folder:
            config["mounts"].append(
                {
                    "Target": cover.target,
                    "Type": "tmpfs",
                    "ReadOnly": True,
                    "TmpfsOptions": dict(COVER_TMPFS),
                }
            )
        else:
            config["mounts"].append(
                {
                    "Target": cover.target,
                    "Source": COVER_SOURCE,
                    "Type": "bind",
                    "ReadOnly": True,
                }
            )
    return config


###############################################################################
#
def validate(
    config: dict[str, Any],
    settings: SandboxSettings,
    granted: Collection[str] = (),
) -> None:
    """
    Refuse any container configuration outside the fixed sandbox.

    Args:
        config: the `containers.create` arguments, as built
        settings: the runner's own configuration
        granted: the host paths the broker granted this run. A bind mount of any
            other path is refused.

    Raises:
        ValueError: naming the first setting that breaks the sandbox
    """
    extra = set(config) - ALLOWED_KEYS
    if extra:
        raise ValueError(f"settings outside the sandbox: {sorted(extra)}")
    if config.get("image") not in settings.images.values():
        raise ValueError(f"image not in the runtime allowlist: {config.get('image')}")

    fixed = {
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "user": RUN_USER,
    }
    for key, value in fixed.items():
        if config.get(key) != value:
            raise ValueError(f"{key} must be {value!r}, not {config.get(key)!r}")

    tmpfs = config.get("tmpfs") or {}
    if set(tmpfs) != {WORKSPACE, "/tmp"}:
        raise ValueError(f"tmpfs only on {WORKSPACE} and /tmp, not {sorted(tmpfs)}")

    _check_limits(config)

    targets: set[str] = set()
    bound: set[str] = set()
    covers: list[dict[str, Any]] = []
    for mount in config.get("mounts") or []:
        if mount.get("Type") == "tmpfs" or (
            mount.get("Type") == "bind" and mount.get("Source") == COVER_SOURCE
        ):
            covers.append(mount)
        elif mount.get("Type") == "bind":
            _check_bind(mount, granted)
            bound.add(mount["Target"])
        elif mount.get("Type") == "volume":
            if mount.get("Source") != settings.broker_volume:
                raise ValueError(
                    f"only the broker's socket volume, not {mount.get('Source')}"
                )
            if mount.get("Target") != SOCKET_DIR or mount.get("ReadOnly") is not True:
                raise ValueError(f"the socket volume goes read-only at {SOCKET_DIR}")
        else:
            raise ValueError(f"only granted paths and the socket volume, not {mount}")
        if mount.get("Target") in targets:
            raise ValueError(f"two mounts at {mount.get('Target')}")
        targets.add(mount.get("Target"))
    _check_covers(covers, bound)

    environment = config.get("environment") or {}
    if set(environment) - {"MAJI_RUN_TOKEN", "MAJI_BROKER_SOCKET"}:
        raise ValueError(f"unexpected environment: {sorted(environment)}")


def _check_bind(mount: dict[str, Any], granted: Collection[str]) -> None:
    """Check a bind mount is a granted path, read-only and on its own, at `/mnt/<name>`."""
    if set(mount) != {"Target", "Source", "Type", "ReadOnly", "BindOptions"}:
        raise ValueError(f"unexpected bind mount settings: {sorted(mount)}")
    if mount["Source"] not in granted:
        raise ValueError(f"a bind mount only of a granted path, not {mount['Source']}")
    if mount["ReadOnly"] is not True:
        raise ValueError(f"{mount['Source']} must be mounted read-only")
    if mount["BindOptions"] != {"NonRecursive": True}:
        raise ValueError(f"{mount['Source']} must leave out mounts beneath it")
    target = mount["Target"]
    name = target.removeprefix(f"{MOUNT_DIR}/") if isinstance(target, str) else ""
    if target != f"{MOUNT_DIR}/{name}" or not MOUNT_NAME.fullmatch(name):
        raise ValueError(f"a granted path goes at {MOUNT_DIR}/<name>, not {target!r}")


def _check_covers(covers: list[dict[str, Any]], bound: set[str]) -> None:
    """
    Check each cover only hides: it is the empty device or an empty file system,
    read-only, on a path inside a granted mount, and not inside another cover.

    A cover adds no path of yours to the executor, so the check is on where it lands:
    anywhere but inside a granted mount, it would hide a part of the sandbox itself.
    """
    if len(covers) > MAX_COVERS:
        raise ValueError(f"{len(covers)} covers, more than {MAX_COVERS}")
    folders: list[str] = []
    for cover in covers:
        if cover.get("Type") == "tmpfs":
            if set(cover) != {"Target", "Type", "ReadOnly", "TmpfsOptions"}:
                raise ValueError(f"unexpected cover settings: {sorted(cover)}")
            if cover["TmpfsOptions"] != COVER_TMPFS:
                raise ValueError(f"a folder's cover is {COVER_TMPFS}")
        elif set(cover) != {"Target", "Source", "Type", "ReadOnly"}:
            raise ValueError(f"unexpected cover settings: {sorted(cover)}")
        if cover["ReadOnly"] is not True:
            raise ValueError(f"the cover at {cover['Target']} must be read-only")
        target = cover["Target"]
        inside = [
            m for m in bound if isinstance(target, str) and target.startswith(f"{m}/")
        ]
        if not inside:
            raise ValueError(f"a cover goes inside a granted path, not at {target!r}")
        parts = target[len(inside[0]) + 1 :].split("/")
        if "\0" in target or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"a cover's path is plain, not {target!r}")
        if cover.get("Type") == "tmpfs":
            folders.append(target)
    for cover in covers:
        if any(cover["Target"].startswith(f"{folder}/") for folder in folders):
            raise ValueError(f"{cover['Target']} is inside a covered folder")


def _check_limits(config: dict[str, Any]) -> None:
    """Check each limit is present and no higher than `CEILING` allows."""
    memory = config.get("mem_limit", "")
    if not (isinstance(memory, str) and memory.endswith("m") and memory[:-1].isdigit()):
        raise ValueError(f"mem_limit must be set in megabytes, not {memory!r}")
    if int(memory[:-1]) > CEILING.memory_mb:
        raise ValueError(f"mem_limit {memory} is over {CEILING.memory_mb}m")
    if config.get("memswap_limit") != memory:
        raise ValueError("memswap_limit must equal mem_limit, so the run can't swap")
    cpus = config.get("nano_cpus")
    if not isinstance(cpus, int) or not 0 < cpus <= CEILING.cpus * 1_000_000_000:
        raise ValueError(f"nano_cpus {cpus!r} is out of range")
    pids = config.get("pids_limit")
    if not isinstance(pids, int) or not 0 < pids <= CEILING.pids:
        raise ValueError(f"pids_limit {pids!r} is out of range")
