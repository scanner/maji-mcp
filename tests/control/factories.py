"""
Factories for what the control plane's tests put in its queue.

Each item factory builds the arguments of `ControlStore.create_item`. They are also
the parameters of the broker socket's `create_item`, so one description of a
changeset serves the tests of the store, of the REST API and of the sockets:

    store.create_item(**ChangesetFactory.build(high_risk=True))
    await broker.call("create_item", **MountFactory.build(requester=requester))

They are called through `build`, which makes the dictionary and nothing else: an item
exists only once a store is given it.

Titles, claims, facts and the ids of sessions and runs are made up, since no test
reads them back except from the item itself. What a test names in a choice or
compares against stays fixed: the principal, the repositories, and the ids of
patterns and parts.
"""

# 3rd party imports
import factory

# project imports
from tests.support.control import INSTANCE

PRINCIPAL = "claude-code"
REPOSITORY = "~/src/api"


class RequesterFactory(factory.DictFactory):
    """
    Who asked for an item: a principal alone, unless a trait says where it asked from.

    A grant can be held to a session or a run only if the requester named one, so
    `in_session` and `in_run` are what make an approval rememberable that long.
    """

    principal = PRINCIPAL

    class Params:
        in_session = factory.Trait(session=factory.Faker("sha256"))
        in_run = factory.Trait(run=factory.Faker("uuid4"))
        on_machine = factory.Trait(machine=INSTANCE)


class FactFactory(factory.DictFactory):
    """Something the broker computed about a request."""

    label = factory.Faker("word")
    value = factory.Faker("sentence")


class PartFactory(factory.DictFactory):
    """One part of a request that can be approved or refused on its own."""

    id = "api"
    label = factory.LazyAttribute(lambda part: f"~/src/{part.id}")


class PatternFactory(factory.DictFactory):
    """
    A pattern an approval can be remembered with, and so what a grant holds: by
    default `p1`, mounts of one repository.
    """

    id = "p1"
    access = "mount"
    resource = REPOSITORY
    summary = factory.LazyAttribute(lambda p: f"{p.access}s of {p.resource}")
    match = factory.LazyFunction(dict)


class ReasonFactory(factory.DictFactory):
    """
    A reason the broker gives for high risk: by default a suspicion, resting on the
    first line of the item's diff.
    """

    code = "addressed_to_ai"
    basis = "suspected"
    text = "this added line is worded as an instruction"
    evidence = factory.LazyFunction(lambda: [{"line": 0}])


class DiffFactory(factory.DictFactory):
    """A diff of one added line, which the broker flagged."""

    target = factory.Faker("file_path", depth=1, extension="org")
    lines = factory.List(
        [factory.Dict({"op": "add", "text": factory.Faker("sentence"), "flag": True})]
    )


class ItemFactory(factory.DictFactory):
    """
    What every item is created with: a title, who asked, and one fact. Each kind of
    item adds its own.

    The `high_risk` trait makes it an item the broker judged high risk: one reason,
    pointing at the one line of its diff.
    """

    class Meta:
        abstract = True

    title = factory.Faker("sentence")
    requester = factory.SubFactory(RequesterFactory)
    facts = factory.List([factory.SubFactory(FactFactory)])

    class Params:
        high_risk = factory.Trait(
            risk="high",
            risk_reasons=factory.List([factory.SubFactory(ReasonFactory)]),
            diff=factory.SubFactory(DiffFactory),
        )


class ChangesetFactory(ItemFactory):
    """A change an agent proposes, with its claim of what the change does."""

    kind = "changeset"
    requester = factory.SubFactory(RequesterFactory, in_session=True, on_machine=True)
    subject = factory.Faker("sentence", nb_words=3)
    claim = factory.Faker("sentence")


class MountFactory(ItemFactory):
    """
    A request to mount one repository, offering one pattern, `p1`, to remember later
    mounts of it with.
    """

    kind = "grant"
    patterns = factory.List([factory.SubFactory(PatternFactory)])


class MountsFactory(ItemFactory):
    """
    A request to mount several repositories in one run.

    Each repository is a part of the item, named as in `repositories`, with a
    pattern `mount-<name>` over that part alone to remember its mount with.
    """

    kind = "grant"
    requester = factory.SubFactory(RequesterFactory, in_session=True, in_run=True)
    parts = factory.LazyAttribute(
        lambda item: [PartFactory.build(id=name) for name in item.repositories]
    )
    patterns = factory.LazyAttribute(
        lambda item: [
            PatternFactory.build(
                id=f"mount-{name}",
                resource=f"~/src/{name}",
                match={"path": f"~/src/{name}"},
                parts=[name],
            )
            for name in item.repositories
        ]
    )

    class Params:
        repositories = ("api", "web", "notes")


class SkillFactory(factory.DictFactory):
    """A version of a skill: each one made is a new version of the same skill."""

    id = "me/local/count"
    version = factory.Sequence(lambda n: f"{n:016x}")


class PromotionFactory(ItemFactory):
    """A request to promote a version of a skill, as the broker raises it."""

    kind = "promote"
    requester = factory.SubFactory(RequesterFactory, in_session=True)
    skill = factory.SubFactory(SkillFactory)


class QuestionFactory(ItemFactory):
    """A question an agent asks you, which has no facts of its own."""

    kind = "question"
    facts = factory.List([])


class SettingFactory(factory.DictFactory):
    """A setting a service asks for with its key: by default an organization slug."""

    name = "org"
    label = "Organization slug"
    description = "As it appears in the service's addresses."
    pattern = "[a-z0-9][a-z0-9-]{0,49}"


class ServiceFactory(factory.DictFactory):
    """
    A service the broker can call with a key, as the broker reports it: a tracker at a
    made-up host, read with a bearer key, asking for one setting.
    """

    service = "tracker"
    label = "Tracker"
    method = "api_key"
    host = "tracker.example"
    sent_as = "Authorization: Bearer"
    permissions = factory.LazyFunction(lambda: ["issues:read", "projects:read"])
    key_page = "https://tracker.example/settings/keys"
    settings = factory.List([factory.SubFactory(SettingFactory)])
