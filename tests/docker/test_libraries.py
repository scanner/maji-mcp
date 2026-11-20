"""
Tests that the libraries a script can import work in a real executor, under the
default limits, and that the broker learns which ones a run used.

The broker runs in its own container and the script in an executor, as in the stack.
These tests need both images:

    make test-docker
"""

# system imports
import json
from collections.abc import Iterator

# 3rd party imports
import docker
import pytest
from pytest_check import check

# project imports
from tests.support.containers import PRINCIPAL, Broker, start_broker

pytestmark = pytest.mark.docker

POLICY = {"principals": {PRINCIPAL: {"allow": ["maji.*"], "per_minute": 60}}}

# A script that sifts half a million rows, of about 6 MB, with duckdb and with pandas,
# and asks help() about pandas without printing the page it returns.
#
SIFTING = """
import contextlib, csv, io, json
import duckdb
import pandas as pd

with open("/tmp/events.csv", "w", newline="") as out:
    rows = csv.writer(out)
    rows.writerow(["culprit", "ms"])
    rows.writerows((f"site{i % 40}", i % 997) for i in range(500_000))

top = duckdb.sql(
    "select culprit, count(*) as n from '/tmp/events.csv'"
    " group by culprit order by n desc, culprit limit 1"
).fetchall()
counts = pd.read_csv("/tmp/events.csv").groupby("culprit").size()
with contextlib.redirect_stdout(io.StringIO()):
    help(pd.DataFrame.groupby)
print(json.dumps({"duckdb": top[0], "pandas": int(counts["site0"]), "sites": len(counts)}))
"""

# A script that fails inside a library: YAML that never closes its list.
#
FAILING = """
import yaml
yaml.safe_load("steps: [build, test")
"""

# The run log's rows of the libraries a run used, read in the broker's container.
#
ROWS = """
import json, sqlite3, sys
db = sqlite3.connect("/tmp/runs.db")
print(json.dumps(db.execute(
    "SELECT library, imported, asked, error, raised_here FROM runtime_libraries"
    " WHERE run_id = ? ORDER BY library", (sys.argv[1],)).fetchall()))
"""


@pytest.fixture(scope="module")
def broker(
    docker_api: docker.DockerClient, service_image: str, runtime_image: str
) -> Iterator[Broker]:
    """
    A broker with no network and no servers, whose principal may call the built-in
    `maji` stubs. The module's tests share it, each with runs of its own.
    """
    broker = start_broker(docker_api, service_image, runtime_image, POLICY)
    try:
        yield broker
    finally:
        broker.remove()


def _rows(broker: Broker, run_id: str) -> list[list[object]]:
    code, output = broker.container.exec_run(["python", "-c", ROWS, run_id])
    assert code == 0, output.decode()
    rows: list[list[object]] = json.loads(output)
    return rows


def test_a_script_sifts_rows_with_duckdb_and_pandas_under_the_default_limits(
    broker: Broker,
) -> None:
    """
    GIVEN: An executor with the default memory, a workspace on tmpfs, and no network
     WHEN: a script writes half a million rows, groups them with duckdb and with
           pandas, and asks help() about pandas
     THEN: both give the same counts, and the run ends without error
      AND: the run log records that the run imported duckdb and pandas, and asked
           about pandas once
    """
    run = broker.register()
    try:
        result = broker.runner.run("python", SIFTING, run["run_id"], run["token"])
    finally:
        broker.finish(run)
    assert result.exit_code == 0, result.stderr
    with check:
        assert json.loads(result.stdout) == {
            "duckdb": ["site0", 12500],
            "pandas": 12500,
            "sites": 40,
        }
    with check:
        assert _rows(broker, run["run_id"]) == [
            ["duckdb", 1, 0, None, 0],
            ["pandas", 1, 1, None, 0],
        ]


def test_a_failure_inside_a_library_is_counted_against_it(broker: Broker) -> None:
    """
    GIVEN: A script that hands yaml.safe_load a document that never closes its list
     WHEN: it runs in an executor
     THEN: it fails with yaml's own error, and the run log records that the run
           imported yaml and that the error came from yaml
      AND: `maji-library-use` in the broker's container counts that run under yaml
    """
    run = broker.register()
    try:
        result = broker.runner.run("python", FAILING, run["run_id"], run["token"])
    finally:
        broker.finish(run)
    with check:
        assert result.exit_code == 1
    with check:
        assert "yaml.parser.ParserError" in result.stderr
    with check:
        assert _rows(broker, run["run_id"]) == [["yaml", 1, 0, "ParserError", 1]]

    code, output = broker.container.exec_run(
        ["maji-library-use", "--run-log", "/tmp/runs.db", "--days", "1"]
    )
    assert code == 0, output.decode()
    [yaml_row] = [
        line.split() for line in output.decode().splitlines() if line.startswith("yaml")
    ]
    assert yaml_row[1:4] == ["1", "1", "1"]
