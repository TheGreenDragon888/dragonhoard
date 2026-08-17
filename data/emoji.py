"""
data/emoji.py

A custom Discord emoji belongs to whichever application uploaded it, not to
the bot's code. "Dragonhoard" (live) and "Dragonhoard Beta" are two
completely separate Discord applications (docs/testing.md) - nothing is
shared between them - so every icon has to be uploaded to each one
separately and ends up with a different numeric ID on each side. A live ID
means nothing to the beta bot, and vice versa.

custom_emoji() is how every material's "emoji" field (data/materials.py) and
any other custom emoji in the game resolves to the right ID for whichever
application this process logged in as (config.IS_BETA). It's called once,
at import time - which application this process is doesn't change while
running - so everything downstream still just reads a plain string out of
the material dict exactly as before; nothing else needs to change.

An icon that hasn't been uploaded to Dragonhoard Beta yet passes
beta_id=None and resolves to MISSING_EMOJI there, so a missing beta upload
is obvious while visually testing instead of looking like a real bug. Every
icon in data/materials.py currently has both ids, so nothing takes that path
today - it exists for the gap between designing an icon and uploading it to
the second application.
"""
import config

# Shown in place of a custom emoji that this process's application doesn't
# have an ID for yet. Matches the "unknown id" glyph get_material_info's
# callers already fall back to (see the ALL_MATERIALS comment in
# data/materials.py) - reusing it here keeps one visual meaning for "this
# icon isn't resolvable" instead of introducing a second one.
MISSING_EMOJI = "❓"


def custom_emoji(name: str, live_id: int, beta_id: int | None) -> str:
    """Resolves one custom emoji's Discord markup for whichever application
    this process is running as.

    `name` is cosmetic - Discord identifies the emoji by id alone - but
    keeping it matching the icon's real uploaded name makes raw embed
    content readable and easy to cross-check against the Developer Portal's
    emoji list. `beta_id` is None until that icon has been uploaded to
    Dragonhoard Beta too; fill it in then, same as any other id here.
    """
    emoji_id = beta_id if config.IS_BETA else live_id
    if emoji_id is None:
        return MISSING_EMOJI
    return f"<:{name}:{emoji_id}>"


# Not tied to any material - the /mine status field label in cogs/mining.py.
MINING_POOL_EMOJI = custom_emoji("MiningBlock", 1523436645729173514, 1533714755150282932)
