"""The words Remote's passphrase is made of: 512 of them, so four distinct ones are ~36 bits.

The passphrase was four distinct words of 32, 863 040 phrases or about 19.7 bits, and
the unlock limiter in front of it keyed on a header the client writes. Four words of
512 is 512 x 511 x 510 x 509, about 6.8 x 10^10 phrases or 35.98 bits, for the same
four words of typing; against the global failed-unlock budget (20 wrong guesses per 30
minutes, at most 960 a day) the expected time to guess one is about 3.5 x 10^7 days.

Curated by hand, for a phrase read off a terminal and typed on a phone:

* common, concrete words a phone keyboard already knows, 3 to 7 lower-case letters;
* no word with a common homophone or near-homophone in a widespread accent (no
  ``bear``/``bare``, ``cedar``/``seeder``, ``atom``/``Adam``, ``lava``/``larva``), and no
  word with a second accepted spelling (``harbor``/``harbour``, ``yogurt``/``yoghurt``),
  so a phrase read aloud is typed the one way it was written;
* nothing offensive, and no slang sense a person would not want to read out.

``normalize_passphrase`` in :mod:`aisquare.services.remote_server` lowercases what the
phone sent and joins its letter runs with ``-``, which is why every word is letters only.
A remote module (``services/remote_*.py``): imported by ``new_password`` only, inside it.
"""

from __future__ import annotations

REMOTE_PASSPHRASE_WORDS: tuple[str, ...] = (
    "acorn", "almond", "alpine", "amber", "anchor", "anvil", "apple", "apricot", "apron", "arrow",
    "atlas", "attic", "aurora", "autumn", "avocado", "azure", "badge", "badger", "bagel",
    "balcony", "ballet", "bamboo", "banana", "banjo", "banner", "barley", "barn", "barrel",
    "basil", "basket", "beacon", "bench", "birch", "bison", "blanket", "boat", "bobcat", "bottle",
    "brave", "breeze", "bridge", "bright", "bronze", "brook", "bucket", "bugle", "butter",
    "button", "cabbage", "cabin", "cable", "cactus", "calm", "camel", "camera", "canal", "candle",
    "candy", "canoe", "canyon", "carbon", "cargo", "carpet", "cashew", "castle", "cave", "cement",
    "chair", "chalk", "chapel", "cherry", "chess", "chorus", "chrome", "cider", "circus",
    "clever", "cliff", "cloud", "clover", "coast", "cobalt", "cobra", "coconut", "coffee",
    "comet", "compass", "concert", "cookie", "copper", "corn", "cottage", "cotton", "cove",
    "cradle", "crane", "crater", "crayon", "cream", "cricket", "crimson", "crisp", "crown",
    "crystal", "cup", "cushion", "custard", "cyclone", "daisy", "delta", "denim", "desk", "dice",
    "dolphin", "domino", "donkey", "dragon", "drizzle", "drum", "dune", "dusk", "eager", "eagle",
    "easel", "eclipse", "ember", "emerald", "engine", "fabric", "falcon", "fancy", "fence",
    "fennel", "ferret", "fiddle", "field", "fig", "finch", "flag", "flask", "flute", "forest",
    "fork", "frost", "funnel", "gadget", "galaxy", "garage", "garden", "garlic", "garnet",
    "gecko", "gentle", "giant", "ginger", "giraffe", "glacier", "glade", "glider", "globe",
    "glove", "goblet", "golden", "goose", "granite", "grape", "gravity", "guava", "guitar",
    "gulf", "hallway", "hammer", "happy", "harp", "hazel", "helmet", "heron", "hill", "hinge",
    "honey", "hornet", "humble", "husky", "igloo", "indigo", "inlet", "iron", "island", "ivory",
    "jacket", "jade", "jaguar", "jar", "jasper", "jazz", "jelly", "jigsaw", "jolly", "jungle",
    "juniper", "kale", "karate", "kayak", "kestrel", "kettle", "kitchen", "kite", "kitten",
    "kiwi", "koala", "ladder", "ladle", "lagoon", "lake", "lamp", "lantern", "laptop", "laser",
    "lasso", "latch", "leather", "lemon", "lentil", "lever", "library", "lilac", "lily", "lime",
    "linen", "lively", "lizard", "lobby", "lobster", "locket", "lotus", "lucky", "lunar", "lyric",
    "magnet", "magpie", "mammoth", "mango", "mansion", "maple", "marble", "maroon", "marsh",
    "matrix", "meadow", "mellow", "melody", "melon", "mesa", "meteor", "mighty", "mint", "mirror",
    "mitten", "monkey", "moon", "mosaic", "moth", "motor", "mouse", "muffin", "napkin", "navy",
    "nebula", "nectar", "needle", "neon", "nimble", "noble", "nutmeg", "oasis", "oatmeal",
    "olive", "onion", "onyx", "opal", "opera", "orange", "orchard", "orchid", "otter", "oven",
    "owl", "oxygen", "oyster", "paddle", "palace", "panther", "pantry", "papaya", "parade",
    "park", "parrot", "parsley", "pasta", "pasture", "peach", "peanut", "pebble", "pecan",
    "pelican", "pencil", "penguin", "pepper", "pewter", "photon", "piano", "pickle", "picnic",
    "piglet", "pillow", "pilot", "pixel", "pizza", "planet", "plank", "plasma", "plaza", "pocket",
    "poem", "polar", "pond", "pony", "poodle", "popcorn", "poppy", "porch", "potato", "pretzel",
    "prism", "proton", "proud", "pudding", "puffin", "pulse", "puma", "pumpkin", "puppet",
    "puzzle", "python", "quail", "quantum", "quarry", "quick", "quiet", "quilt", "rabbit",
    "radar", "radio", "radish", "raft", "rainbow", "raisin", "rapid", "raven", "ravine", "rhino",
    "ribbon", "rice", "ridge", "river", "robin", "robot", "rocket", "rodeo", "rudder", "rust",
    "rustic", "saddle", "safari", "saffron", "salad", "salmon", "salsa", "sand", "sandal",
    "satin", "scarf", "scarlet", "scooter", "sesame", "shadow", "shark", "sheep", "shiny",
    "shovel", "shrimp", "signal", "silent", "silk", "silver", "simple", "sketch", "sky", "slate",
    "sled", "sleepy", "sleet", "slope", "sloth", "smooth", "snail", "snow", "snowy", "socket",
    "sofa", "solar", "sonar", "sonnet", "sparrow", "spider", "spinach", "spiral", "sponge",
    "spoon", "spring", "sprout", "squash", "squid", "stamp", "statue", "storm", "stream",
    "string", "studio", "sturdy", "subway", "sugar", "summit", "swamp", "swan", "sweater",
    "swift", "table", "tablet", "taco", "tango", "taxi", "teal", "teapot", "temple", "tempo",
    "thimble", "thunder", "ticket", "tidy", "tiger", "tin", "tiny", "toast", "toaster", "tomato",
    "topaz", "tornado", "toucan", "towel", "tower", "tractor", "trail", "trophy", "trout",
    "truck", "trumpet", "tugboat", "tulip", "tundra", "tunnel", "turkey", "turnip", "turtle",
    "valley", "vanilla", "vase", "vector", "velvet", "villa", "village", "violet", "violin",
    "viper", "vivid", "volcano", "vortex", "waffle", "wallet", "walnut", "walrus", "waltz",
    "warm", "wasp", "wheat", "whistle", "wild", "willow", "window", "witty", "wombat", "woods",
    "wool", "wren", "wrench", "yak", "yarn", "young", "zebra", "zenith", "zinc", "zipper",
)  # fmt: skip
"""Sorted, distinct, each ``[a-z]{3,7}`` (``tests/test_remote_security.py`` pins all of it)."""

__all__ = ["REMOTE_PASSPHRASE_WORDS"]
