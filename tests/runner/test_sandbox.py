"""Tests for the executor sandbox's fixed flags and the check that enforces them."""

# system imports
from typing import Any

# 3rd party imports
import pytest
from pytest_check import check

# project imports
from maji_mcp.runner.sandbox import (
    COVER_SOURCE,
    COVER_TMPFS,
    MAX_COVERS,
    Cover,
    Mount,
    SandboxSettings,
    container_config,
    validate,
)

SETTINGS = SandboxSettings(
    images={"python": "maji-python-runtime:latest"}, broker_volume="maji_broker-call"
)


@pytest.fixture
def config() -> dict[str, Any]:
    """The settings container_config builds for a run with nothing mounted."""
    return container_config(SETTINGS, "python", "run-1", "token-1")


def test_the_built_config_passes_its_own_check(config: dict[str, Any]) -> None:
    """
    GIVEN: A run request for the Python runtime
     WHEN: container_config builds the container's settings
     THEN: validate accepts them unchanged
      AND: they have no network, a read-only root, a non-root user, no capabilities,
           and only the broker's socket volume, read-only
    """
    validate(config, SETTINGS)

    # Each is one of the sandbox's fixed settings, so each is reported on its own.
    #
    with check:
        assert config["network_mode"] == "none"
    with check:
        assert config["read_only"] is True
    with check:
        assert config["user"] == "10001:10001"
    with check:
        assert config["cap_drop"] == ["ALL"]
    with check:
        assert config["security_opt"] == ["no-new-privileges:true"]
    with check:
        assert config["mounts"] == [
            {
                "Target": "/run/maji",
                "Source": "maji_broker-call",
                "Type": "volume",
                "ReadOnly": True,
            }
        ]


def test_an_unknown_runtime_is_refused() -> None:
    """
    GIVEN: A run request naming a runtime outside the allowlist
     WHEN: container_config builds the container's settings
     THEN: it refuses, so no image is ever chosen by the request
    """
    with pytest.raises(ValueError, match="unknown runtime"):
        container_config(SETTINGS, "ruby", "run-1", "token-1")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.update(privileged=True), "outside the sandbox"),
        (lambda c: c.update(cap_add=["SYS_ADMIN"]), "outside the sandbox"),
        (lambda c: c.update(volumes={"/Users": {"bind": "/h"}}), "outside the sandbox"),
        (lambda c: c.update(devices=["/dev/kmsg"]), "outside the sandbox"),
        (lambda c: c.update(pid_mode="host"), "outside the sandbox"),
        (lambda c: c.update(network_mode="bridge"), "network_mode"),
        (lambda c: c.update(read_only=False), "read_only"),
        (lambda c: c.update(user="0:0"), "user"),
        (lambda c: c.update(cap_drop=[]), "cap_drop"),
        (lambda c: c.update(security_opt=[]), "security_opt"),
        (lambda c: c.update(image="docker:cli"), "allowlist"),
        (lambda c: c["tmpfs"].update({"/etc": "rw"}), "tmpfs"),
        (lambda c: c.update(mem_limit="8g"), "mem_limit"),
        (lambda c: c.update(mem_limit="99999m"), "over"),
        (lambda c: c.update(memswap_limit="-1"), "memswap_limit"),
        (lambda c: c.update(pids_limit=-1), "pids_limit"),
        (lambda c: c.update(nano_cpus=64_000_000_000), "nano_cpus"),
        (
            lambda c: c["mounts"].append(
                {"Target": "/h", "Source": "/Users", "Type": "bind", "ReadOnly": True}
            ),
            "unexpected bind mount settings",
        ),
        (
            lambda c: c["mounts"].append(
                {"Target": "/h", "Source": "/var/run/docker.sock", "Type": "tmpfs"}
            ),
            "unexpected cover settings",
        ),
        (
            lambda c: c["mounts"].append(
                {"Target": "/h", "Source": "/var/run/docker.sock", "Type": "npipe"}
            ),
            "only granted paths",
        ),
        (
            lambda c: c["mounts"].append(
                {"Target": "/x", "Source": "other", "Type": "volume", "ReadOnly": True}
            ),
            "socket volume",
        ),
        (lambda c: c["mounts"][0].update(ReadOnly=False), "read-only"),
        (lambda c: c["environment"].update(SENTRY_AUTH_TOKEN="x"), "environment"),
    ],
)
def test_every_way_out_of_the_sandbox_is_refused(
    config: dict[str, Any], change: Any, message: str
) -> None:
    """
    GIVEN: A container configuration that differs from the fixed sandbox in one way,
           such as privileges, a host mount, a network or a missing limit
     WHEN: validate checks it before the container is created
     THEN: it refuses, naming the setting
    """
    change(config)
    with pytest.raises(ValueError, match=message):
        validate(config, SETTINGS)


def test_a_run_without_a_broker_volume_mounts_nothing() -> None:
    """
    GIVEN: A runner configured with no broker socket volume
     WHEN: container_config builds a run's settings
     THEN: the container has no mounts at all, and still passes the check
    """
    settings = SandboxSettings(broker_volume=None)
    config = container_config(settings, "python", "run-1", "token-1")
    assert config["mounts"] == []
    validate(config, settings)


GRANTED = "/Users/someone/src/api"
ALSO_GRANTED = "/Users/someone/src/web"


def _bind(**changes: Any) -> dict[str, Any]:
    """The bind mount of the granted path, with any settings changed."""
    mount = {
        "Target": "/mnt/api",
        "Source": GRANTED,
        "Type": "bind",
        "ReadOnly": True,
        "BindOptions": {"NonRecursive": True},
    }
    return {**mount, **changes}


def _file_cover(**changes: Any) -> dict[str, Any]:
    """The cover of a file inside the granted path, with any settings changed."""
    cover = {
        "Target": "/mnt/api/.env",
        "Source": COVER_SOURCE,
        "Type": "bind",
        "ReadOnly": True,
    }
    return {**cover, **changes}


def _folder_cover(**changes: Any) -> dict[str, Any]:
    """The cover of a folder inside the granted path, with any settings changed."""
    cover = {
        "Target": "/mnt/api/private",
        "Type": "tmpfs",
        "ReadOnly": True,
        "TmpfsOptions": dict(COVER_TMPFS),
    }
    return {**cover, **changes}


def test_a_granted_path_is_bound_read_only_under_mnt() -> None:
    """
    GIVEN: A run the broker granted two paths
     WHEN: container_config builds the container's settings with them
     THEN: each path is bound read-only at /mnt/<name>, leaving out anything mounted
           beneath it
      AND: validate accepts the settings, given the same granted paths
    """
    mounts = [Mount(GRANTED, "api"), Mount(ALSO_GRANTED, "web")]
    config = container_config(SETTINGS, "python", "run-1", "token-1", mounts=mounts)
    validate(config, SETTINGS, granted={GRANTED, ALSO_GRANTED})
    assert config["mounts"][1:] == [
        _bind(),
        _bind(Target="/mnt/web", Source=ALSO_GRANTED),
    ]


@pytest.mark.parametrize(
    ("mounts", "message"),
    [
        ([_bind(Source="/Users/someone/.ssh")], "only of a granted path"),
        ([_bind(ReadOnly=False)], "read-only"),
        ([_bind(BindOptions={})], "mounts beneath it"),
        ([_bind(Target="/run/maji")], "/mnt/<name>"),
        ([_bind(Target="/mnt")], "/mnt/<name>"),
        ([_bind(Target="/mnt/..")], "/mnt/<name>"),
        ([_bind(Target="/mnt/a/b")], "/mnt/<name>"),
        ([_bind(Propagation="shared")], "unexpected bind mount settings"),
        ([_bind(), _bind(Source=ALSO_GRANTED)], "two mounts at /mnt/api"),
    ],
)
def test_a_bind_mount_outside_its_grant_is_refused(
    config: dict[str, Any], mounts: list[dict[str, Any]], message: str
) -> None:
    """
    GIVEN: A run granted two paths, and a container configuration whose bind mount
           differs from a grant in one way: another path, writable, carrying what is
           mounted beneath it, placed outside /mnt/<name>, or placed where another
           granted path already is
     WHEN: validate checks it before the container is created
     THEN: it refuses, naming what is wrong, so one mount can't hide another and
           nothing but a granted path is bound
    """
    config["mounts"] += mounts
    with pytest.raises(ValueError, match=message):
        validate(config, SETTINGS, granted={GRANTED, ALSO_GRANTED})


def test_a_cover_hides_a_file_or_a_folder_inside_a_granted_path() -> None:
    """
    GIVEN: A run granted a folder that holds a file and a folder a script isn't to
           read
     WHEN: container_config builds the container's settings with a cover for each
     THEN: the file is covered by the empty device and the folder by an empty file
           system, each read-only, at its path inside the mount
      AND: validate accepts the settings
    """
    covers = [Cover("api", ".env"), Cover("api", "private", folder=True)]
    config = container_config(
        SETTINGS,
        "python",
        "run-1",
        "token-1",
        mounts=[Mount(GRANTED, "api")],
        covers=covers,
    )
    validate(config, SETTINGS, granted={GRANTED})
    assert config["mounts"][1:] == [_bind(), _file_cover(), _folder_cover()]


@pytest.mark.parametrize(
    ("covers", "message"),
    [
        ([_file_cover(Target="/run/maji/call.sock")], "inside a granted path"),
        ([_file_cover(Target="/work/notes")], "inside a granted path"),
        ([_file_cover(Target="/mnt/api")], "two mounts at /mnt/api"),
        ([_file_cover(Target="/mnt/web/.env")], "inside a granted path"),
        ([_file_cover(Target="/mnt/api/../../etc/passwd")], "path is plain"),
        ([_file_cover(Target="/mnt/api//.env")], "path is plain"),
        ([_file_cover(Target="/mnt/api/./.env")], "path is plain"),
        ([_file_cover(ReadOnly=False)], "must be read-only"),
        ([_file_cover(Source="/Users/someone/.ssh/id_rsa")], "unexpected bind mount"),
        ([_file_cover(BindOptions={"Propagation": "shared"})], "unexpected cover"),
        ([_folder_cover(Target="/tmp/x")], "inside a granted path"),
        ([_folder_cover(ReadOnly=False)], "must be read-only"),
        ([_folder_cover(TmpfsOptions={"SizeBytes": 1 << 30})], "a folder's cover is"),
        ([_folder_cover(Source="/Users/someone")], "unexpected cover"),
        (
            [_folder_cover(), _file_cover(Target="/mnt/api/private/key.pem")],
            "inside a covered folder",
        ),
        (
            [
                _file_cover(Target=f"/mnt/api/key-{n}.pem")
                for n in range(MAX_COVERS + 1)
            ],
            "more than",
        ),
    ],
)
def test_a_cover_that_could_do_more_than_hide_is_refused(
    config: dict[str, Any], covers: list[dict[str, Any]], message: str
) -> None:
    """
    GIVEN: A run granted one folder, and a container configuration whose covers
           differ from covers of paths in that folder in one way: placed outside the
           folder's mount, reached through `..`, writable, bound from a path of
           yours, an empty file system with settings of its own, placed inside a
           folder already covered whole, or more than MAX_COVERS of them
     WHEN: validate checks it before the container is created
     THEN: it refuses, naming what is wrong, so a cover only ever hides
    """
    config["mounts"] += [_bind(), *covers]
    with pytest.raises(ValueError, match=message):
        validate(config, SETTINGS, granted={GRANTED})
