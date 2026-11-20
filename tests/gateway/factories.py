"""
Factories for what the gateway's tests build in several places with small differences:
a run's result as the runner returns it, and a tool call's context as far as the
gateway reads it.

A test calls a factory's `build`, with whatever it wants different from the usual.
The root `conftest.py` seeds the values the factories make up before each test.
"""

# system imports
from types import SimpleNamespace
from typing import Any

# 3rd party imports
import factory
from mcp.types import Implementation


class RunResultFactory(factory.DictFactory):
    """
    A run's result, as the runner returns it to the gateway: a script that printed
    nothing and exited 0, with a run id and a duration made up for it.
    """

    stdout = ""
    stderr = ""
    exit_code = 0
    timed_out = False
    truncated = False
    run_id = factory.Faker("hexify", text="^" * 32)
    duration_ms = factory.Faker("pyint", min_value=1, max_value=9999)


class ImplementationFactory(factory.Factory):
    """What an agent's app says it is as it connects: a made-up name and version."""

    class Meta:
        model = Implementation

    name = factory.Faker("slug")
    version = factory.Faker("numerify", text="%.#.###")


def _headers(context: Any) -> dict[str, str]:
    """The session headers of a context's request: each id that was given."""
    given = {
        "mcp-session-id": context.mcp_session,
        "x-maji-session": context.named_session,
    }
    return {name: value for name, value in given.items() if value is not None}


class ContextFactory(factory.Factory):
    """
    A tool call's context, as far as the gateway reads it for a session and its app.

    `app` is what the client said it is, or None for a client that gave no name.
    `mcp_session` and `named_session` are the ids in the request's `mcp-session-id`
    and `X-Maji-Session` headers; a request carries each header only when its id is
    given.
    """

    class Meta:
        model = SimpleNamespace

    class Params:
        app = factory.SubFactory(ImplementationFactory)
        mcp_session = None
        named_session = None

    session = factory.LazyAttribute(
        lambda o: SimpleNamespace(client_params=SimpleNamespace(client_info=o.app))
    )
    request_context = factory.LazyAttribute(
        lambda o: SimpleNamespace(request=SimpleNamespace(headers=_headers(o)))
    )
