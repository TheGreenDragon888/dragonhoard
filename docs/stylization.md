# Stylization

The single source of truth for the bot's visual identity. Every embed is
built through `utils/embeds.py: make_embed()`, which applies a palette color
and the standard footer automatically.

## Footer

All embeds end with the footer:

> Dragonhoard by Isaac Day · Version X.X

The version number comes from `config.VERSION` — bump it there when
releasing a new version.

**Footer text renders no emoji** — not custom `<:Name:ID>` ones and not
unicode ones either. A footer that needs to refer to a material has to NAME
it. Two commands extend the footer, and they are the only ones that do: a
just-unlocked `/focus` or `/efficiency` appends what the unlock cost, as
"· unlocked for 1 Ruby" rather than as the gem's icon. Both go through
`cogs/mining.py: unlock_footer`, so the rule lives in one place; a third
caller should use it rather than build its own string.

## Infrastructure status embeds

`/furnace status`, `/blast status`, `/factory status`, `/press status` and
`/scrapper status` share one shape, built by `utils/embeds.py:
make_infrastructure_embed()`. A sixth machine should use it rather than
inventing its own layout - the blast furnace needed nothing from it but a
`unit` argument on two field helpers, because it counts in batches rather than
items.

```
author:      🏭 Factory • Level 3
title:       3 items/hour
description: 💰 4.50 / 💰 500.00 to level 4

Fee  💰 0.25 per item        Queue Limit  15 items per user
                                          (5 × level 3)

Queue • 12 items / 3 jobs (2h 24m wait)
  5x 🪛 Steel Drill Bit • @alice
  1x 🔧 ⛏ Iron Drill Lv.1 → 2 • @bob
```

The author line identifies the machine and its level, the title is what that
level buys you, and the description is the one number a server is working
towards. Fields are left for the two settings a manager can actually change,
and for the queue — whose heading carries the counts and the total wait rather
than spending two more fields on them.

Queue Limit shows the cap that is actually enforced, with the arithmetic on a
second line (`utils/embeds.py: queue_limit_field_value`). The multiplier is
invisible otherwise, and a manager who set 5 and is watching someone queue 15
would reasonably read that as a bug. The second line is omitted at level 1,
where there is no multiplication to explain.

Two constraints the layout depends on:

- The author line's emoji must be **unicode** (🏭 🔥 ⚙️). Discord renders
  custom `<:Name:ID>` emoji in descriptions, field names and field values, but
  not in author lines or titles — there they come out as the literal text
  `<:IronOre:1533714268560691281>`. Field **names** are fine and several embeds
  rely on it: `/mine status`'s pool heading and every heading in `/focus`.
  Footers are stricter still and take no emoji at all — see Footer above.
- The queue heading's wait uses `format_duration` ("2h 24m"), not a Discord
  relative timestamp. Timestamp markup doesn't render in a field name.
  `format_relative_timestamp` is for descriptions and field values — which is
  where the queue receipts use it.

## Embed colors

All colors are **fully saturated**. The default is green `#00FF3C`; each
feature area gets its own color, dictated below and repeated in that
feature's own design doc where one exists.

| Feature area                        | Color       | Hex       | Constant (`utils/embeds.py`) |
| ----------------------------------- | ----------- | --------- | ---------------------------- |
| Bot settings (`/setup`) and manual (`/help`, `/manual`, `/man`) | green (default) | `#00FF3C` | `DEFAULT_COLOR` |
| Mining menus (`/mine`, `/collect`)  | purple      | `#8C00FF` | `MINING_COLOR`   |
| Inventory and balance (`/inventory`, `/balance`) | orange | `#FF8C00` | `INVENTORY_COLOR` |
| Market menus (`/market`)            | yellow      | `#FFE600` | `MARKET_COLOR`   |
| Furnace menus (`/furnace`)          | purple-red  | `#FF0059` | `FURNACE_COLOR`  |
| Blast furnace menus (`/blast`)      | pure red    | `#FF0000` | `BLAST_FURNACE_COLOR` |
| Factory menus (`/factory`)          | red-orange  | `#FF4000` | `FACTORY_COLOR`  |
| Recipe book (`/recipe`)             | cyan        | `#00FFEA` | `RECIPE_COLOR`   |
| Hydraulic press menus (`/press`)    | blue        | `#0066FF` | `PRESS_COLOR`    |
| Scrapper menus (`/scrapper`)        | chartreuse  | `#9EFF00` | `SCRAPPER_COLOR` |
| Job board (`/jobboard`)             | magenta     | `#FF00AA` | `JOBBOARD_COLOR` |
| Global notifications                | pure green  | `#00FF00` | `GLOBAL_NOTICE_COLOR` |
| Server notifications                | pure yellow | `#FFFF00` | `SERVER_NOTICE_COLOR` |
| Personal notifications              | indigo      | `#2200FF` | `PERSONAL_NOTICE_COLOR` |
| Extras manual page (`/help extras`) | green (default) | `#00FF3C` | `DEFAULT_COLOR` |

The three notification colors are the ones that have to be told apart at a
glance rather than merely recognised, because the embeds are otherwise
identical in shape and arrive unbidden alongside whatever command the player
actually ran (`utils/notifications.py`). Pure green is the bot announcing
something about itself to everyone; pure yellow is one server's own business;
indigo is something that happened to you personally.

Two of the three sit close to a color already in the table — `#00FF00` beside
the default green `#00FF3C`, `#FFFF00` beside the market's `#FFE600`. That is
tolerable because neither pair ever appears in the same embed, but it is the
constraint to check first if a future feature claims a color near either.

A notice colour has a second constraint the feature colours don't, and it is
the one that ruled out the obvious cyan for the personal feed. A notice is
merged into the SAME MESSAGE as the reply it rides along with
(`utils/responses.py`), so unlike the pairs above it can genuinely appear
beside any feature's colour, and all three can appear beside each other.
`#00FFFF` would have sat 21 units of blue from the recipe book's `#00FFEA`
in a message a `/recipe` reply can produce. Indigo is roughly 25° of hue from
its nearest neighbours (`#0066FF` and `#8C00FF`) and further than that from the
other two notice colours.

The blast furnace's `#FF0000` is the third such pair, sitting between the
furnace's `#FF0059` and the factory's `#FF4000`. It is the deliberate choice
of the three: the two smelters are meant to read as relatives, the hot end of
the wheel is where a furnace belongs, and the pages that could be confused
(`/furnace status` and `/blast status`) always name the machine in their author
line. A future feature wanting red should take it up with these three rather
than adding a fourth.

Future games/features should claim their own fully saturated color here (and
in their design doc) before implementation.

`/honk` is the one command that sends no embed at all - its response is the
audio clip on its own, and a title card above the player would only get in the
way of it.

The manual is the one deliberate exception to "one command, one color": each of
its pages is tinted with the color of the feature it describes (the mining page
is purple, the furnace page purple-red, the blast furnace page red, and so on),
so the reader recognises a
game by its color before reading a word. Its own front page uses the default
green. See `SECTIONS` in `data/manual.py`.

## Custom emoji ids

Every material and drill's icon is a custom Discord emoji, defined in
`data/materials.py` as `custom_emoji("Name", live_id, beta_id)` rather than a
plain `<:Name:ID>` string. Dragonhoard and Dragonhoard Beta are separate
Discord applications with separately-uploaded copies of every icon, so a
single id isn't enough - `data/emoji.py` picks whichever one matches the
application this process logged in as. See docs/testing.md part 1c for the
upload workflow when adding or changing an icon.
