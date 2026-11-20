"""Fixtures for the tests of what runs on the host: the bridge, the relay and setup."""

# system imports
from collections.abc import Callable

# 3rd party imports
import pytest
from faker import Faker


@pytest.fixture
def new_token(faker: Faker) -> Callable[[], str]:
    """
    A function that returns a made-up token, a new one each time a test calls it: 43
    letters and digits, as long as the tokens `secrets.token_urlsafe(32)` makes.
    """

    def token() -> str:
        return faker.password(length=43, special_chars=False)

    return token
