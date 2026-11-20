"""
Names for agent sessions that people can tell apart: "laughing-sparky-daisy".

A session's key is a SHA-256 in hex, which nobody can read at a glance. Its name is a
present participle, an adjective and a noun, picked by a hash of the key, so the
gateway's log, the control plane and every client give the same session the same name
without keeping a table of them.

A name is a label, never an identity. Grants, decisions and revocations stay bound to
the key, and the start of the key is shown beside the name. An agent can't choose its
key, but it could open session after session until one is named like another, so no
two words in a list are within one letter of each other.

The words come from gentle things, nature, weather, colour and texture, chosen to read
well in any combination.
"""

# system imports
import hashlib

# The lists are frozen: a name comes from positions in them and from their lengths,
# so changing, reordering or adding a word renames every session, including the ones
# grants are held for and those in logs already written. `tests/common/test_names.py` pins
# them.
#
VERBS = tuple(
    """
    beaming bobbing bouncing browsing bubbling chirping chuckling climbing coasting
    counting crooning cycling dabbling dancing darting dashing diving doodling
    dozing drawing dreaming drifting drumming echoing exploring flickering floating
    fluttering folding foraging gardening gathering giggling gleaming gliding
    glimmering glinting glowing hiking hopping hovering humming idling jingling
    juggling kayaking knitting laughing leaping lilting lingering looping lounging
    marching meandering mending murmuring musing napping nodding orbiting paddling
    painting pausing perching planting pondering pottering prancing purring puzzling
    questing rafting rambling reading resting rhyming rippling roaming rowing
    sauntering scribbling seeking shimmering shining sifting singing skating
    sketching skipping sledding slumbering smiling snoozing soaring sorting spinning
    spiraling splashing sprouting stacking stargazing steering stitching strolling
    strumming studying surfing swaying swimming swooping tidying tinkering tiptoeing
    trotting trundling tumbling tuning twinkling twirling voyaging waltzing
    wandering watching waving whistling winking wishing wobbling writing yodeling
    zigzagging zooming
    """.split()
)

ADJECTIVES = tuple(
    """
    airy amber ample arctic ashen auburn autumnal azure balmy beige blithe bluish
    blustery bold breezy bright brisk bronze bubbly burnished calm candid chalky
    cheery chestnut chilly clear cloudy cobalt cobbled copper coral cosmic cozy
    crimson crisp curly dainty dappled dewy distant dotted downy dreamy earthy
    eastern elfin emerald fabled faded fair feathery festive flaxen fleecy flinty
    floral fluffy foamy foggy fresh frosty frothy fuzzy gauzy gentle giant gilded
    glacial gleeful glittery glossy golden gossamer graceful grand granite grassy
    green hardy hazy hearty hidden hushed icy indigo inky inland ivory jade jaunty
    jolly jovial joyful jumbo keen khaki lacy lavender leafy light lilac limber
    linen little lively lofty lucid lucky lunar magenta marbled maroon mauve merry
    mighty misty molten moonlit mossy mottled muted mystic narrow nautical navy neat
    nifty nimble noble nordic northern oaken oceanic ochre olive opal orange ornate
    pale pastel peaceful pearly pebbly peppy piney placid plush polar polished prime
    pristine purple quaint quick quiet quilted radiant rainy rapid regal rippled
    rocky roomy rosy round royal ruby rugged rural russet rustic rusty sable saffron
    sage sandy satin scarlet scenic seaside serene shiny sienna silent silky silver
    sleek sleepy small smoky smooth snowy snug soft solid sonic southern sparky
    speckled spotted sprightly spry starlit starry stately steady stony stormy
    striped sturdy sublime sunlit sunny sweeping swift tartan tawny teal thorny
    tidal tinted tiny topaz tranquil tropical tufted turquoise twilit upbeat valiant
    vast velvet verdant vernal vintage violet vivid warm watery waxen western
    wheaten whimsical wild willowy windy winged wintry wispy wooded woolly yellow
    zany zippy
    """.split()
)

NOUNS = tuple(
    """
    acorn albatross alder alpaca anchor aspen aster atoll aurora badger banjo basin
    bayou beacon beetle bell birch bison blossom bluebell bluff bobcat bonsai
    boulder bracken bramble breeze brook bubble buttercup butterfly cabin cactus
    cairn candle canoe canyon cardinal caribou cascade cavern cedar chalet chipmunk
    cinder cliff clover cobble comet compass condor coral cosmos cottage coyote crag
    crane crater creek cricket crocus crystal cypress dahlia daisy delta dingo
    dolphin dove dragonfly drum dune eagle eclipse egret elm ember emu equinox
    estuary falcon feather fern ferret fiddle finch firefly fjord flamingo flint
    flute foal forest fox frost galaxy garden gazelle gecko geode geyser gibbon
    glacier glade glen gopher gorge granite grove gull guppy hamlet harbor harp hawk
    hazel heath hedgehog heron hill hollow hourglass iceberg inlet iris island ivy
    jasmine jay juniper kayak kelp kestrel kettle kite koala ladybug lagoon lake
    lantern lark laurel lemur lichen lighthouse lily llama locket lotus lupine lynx
    magnolia magpie mallard maple marble marigold marmot marsh meadow mesa meteor
    minnow mist moon moose moss moth mountain narwhal nebula newt oak oasis ocean
    ocelot orchard orchid origami oriole osprey otter owl pagoda panda parrot
    pasture peak pebble pelican penguin peony petal pheasant piano pigeon pine
    pinecone pinwheel planet plateau plume pond poppy porpoise prairie prism puffin
    quail quartz quill quokka rabbit raccoon rain rapids raven reed ridge ripple
    river rivulet robin rose saddle sandbar sapling seahorse seal seashell sequoia
    sextant shore skylark sloth snail snowflake solstice sonnet sparrow spruce
    squirrel star starfish starling stone stork stream summit sundial sunflower swan
    sycamore tadpole tapir teapot telescope thicket thistle thrush tide timber
    tortoise toucan trail tuba tulip tundra turtle umbrella valley vole walrus
    warbler waterfall wave whale wildflower willow wombat woodland wren yarrow yew
    zebra zenith zephyr zinnia
    """.split()
)


def session_name(session: str) -> str:
    """
    Name a session for people, from its key.

    Args:
        session: the key the session's grants and items are held under; any text is
            named, such as a demo's short key

    Returns:
        Three words joined by hyphens: a present participle, an adjective and a noun
    """
    number = int.from_bytes(hashlib.sha256(session.encode()).digest(), "big")
    number, verb = divmod(number, len(VERBS))
    number, adjective = divmod(number, len(ADJECTIVES))
    return f"{VERBS[verb]}-{ADJECTIVES[adjective]}-{NOUNS[number % len(NOUNS)]}"
