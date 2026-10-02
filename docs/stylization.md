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
it. A just-unlocked `/focus` or `/efficiency` appends what the unlock cost, as
"· unlocked for 1 Ruby" rather than as the gem's icon; an open `/bet` appends
"· odds move as people bet", and drops it once the bet is settled and they
cannot. A receipt whose next step is another command names it there -
"· /market cancel takes it back", "· /help Economy explains GDP" - rather than
spending a sentence of the embed on it.

Every one of those goes through `utils/embeds.py: footer_with()`, which is the
only place the footer is extended. Build the string anywhere else and a release
that changes the footer has to find each copy. (It lived in `cogs/mining.py:
unlock_footer` until 1.4, which was fine while the two unlock commands were its
only callers.)

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

The author line identifies the machine and its level, the title is what the
machine actually runs at, and the description is the one number a server is
working towards. The title is not always that level's speed - fees part way to
the next level speed the machine up in proportion (`data/materials.py:
effective_level`) - so it goes through `utils/formatting.py: format_rate`: one
decimal place below ten, whole numbers from ten, truncated so it never shows a
speed the machine hasn't reached. Fields are left for the two settings a manager can actually change,
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

## Receipts

Anything that moves goods or currency answers with a receipt, laid out the same
way whichever command sent it:

```
title:       🏷️ Listed
description: Listed <:Wiring:…> **30x Wiring**. It comes off the market in 7 days if it hasn't sold.

Listed   <:Wiring:…> **30 Wiring** (10 remaining)
Asking   💰 0.72 each (21.60 for all)
footer:  … · /market cancel takes it back
```

The title says what happened, not which command or office did it (`Machine
Funded`, not `Mayor`). The description is one sentence. Then one field per thing
that moved: emoji, the bolded amount, and what the player has now in
parentheses. That last word is **`remaining` after something was taken and
`total` after something was gained** - one word per direction, everywhere
(`utils/receipts.py: SPENT, GAINED`). 1.4 had grown four for the two meanings.
When the figure in brackets is not the player's own - the treasury, on a Mayor's
receipt - it says whose (`(362.55 left in the treasury)`).

Machine jobs use `build_receipt_embed`, trades `build_market_receipt_embed`,
and everything else `build_action_receipt`; the three share `material_line`
and `currency_line`. Until 1.4.1 every 1.4 command that was not a machine job or
a trade answered with a paragraph of prose instead.

## Figures, not explanations

A status page or receipt carries figures. What they mean - how GDP is counted,
where government revenue goes, how bonds are repaid - is written once, on the feature's page
of `/help` (`data/manual.py`), and a footer note points there when a reader
might need it. Repeating the explanation on the embed made every reader pay
for it on every command, and 1.4's embeds had grown sentences for each.

## One separator inside a line

Parts of one line are separated with ` · `. ` • ` is kept for author lines and
queue headings (`🔥 Furnace • Level 3`, `Queue • 12 items`), and ` - ` and
` — ` are prose, not separators.

## Machine names

A machine is named by `utils/embeds.py: MACHINE_DISPLAY` wherever a player
reads it - its own status page's author line, `/government status`, the
`/economy status` queue, the Mayor's and Treasurer's receipts - so it is
`♨️ Blast Furnace` and `⚙️ Hydraulic Press` everywhere rather than
"Blast furnace" in one place and "Press" in another.

## Market book lines

`/market status` and `/market entries` render a book row the same way, and call
the two books by the same names - **Listings** and **Orders** - so a player
reads one page having learned the other:

```
`#3` <:Wiring:…> `0.7200` · 100 · `72.000` held · expires in 3 days   (entries: id first)
      <:Wiring:…> `0.7200` · 100 · 1 seller                            (status: no id)
      <:IronOre:…> `0.01` `0.02` · 9,000                               (status: the server's row)
```

The server's own rows follow the same grammar - emoji, its two prices, then its
stock after the ` · ` - rather than a bracketed "(9,000 in stock)".

An id column where there is one, then the item's **emoji**, then a
fixed-width price from `format_compact_price`, then the counts.

The item's NAME is deliberately absent from both. It is proportional-width
text, so including it shifts every column after it by a different amount on
each line and defeats the alignment the compact price exists to provide;
custom emoji all render at one fixed size, which is why they can lead a
column and a name cannot. A listed drill is the exception and carries its
level and container instead of a count, because those are most of what one is
worth and a bare "1" would not say which drill was on offer.

## Currency emoji in a repeated column

A field whose every line carries a price names the currency **once in the
field's own name**, not on each line - `Server · Sell · Buy · Stock · {emoji} each`
rather than the emoji beside each of the six prices. It applies to every
embed, not just the market: `/government status`'s machine list, `/bonds
holdings` and `/economy gdp`'s breakdown all follow it as of 1.4.1.

It started as a budget rule. A server's currency emoji can be
a custom or animated one, which is 38 to 53 characters of markup rather than a
single glyph, and `/market status` has four price columns whose length grows
with the number of materials and the size of the player books. Repeating it
per line took the busiest book past Discord's 6,000-character embed ceiling -
which fails the whole message rather than truncating it.
`tests/test_player_market.py: EmbedBudgetTests` pins the worst case.

## Embed colors

All colors are **fully saturated**. The default is green `#00FF3C`; each
feature area gets its own color, dictated below and repeated in that
feature's own design doc where one exists.

| Feature area                        | Color       | Hex       | Constant (`utils/embeds.py`) |
| ----------------------------------- | ----------- | --------- | ---------------------------- |
| Bot settings (`/setup`) and manual (`/help`, `/manual`, `/man`) | green (default) | `#00FF3C` | `DEFAULT_COLOR` |
| Mining menus (`/mine`, `/collect`)  | purple      | `#8C00FF` | `MINING_COLOR`   |
| Inventory and balance (`/inventory`, `/balance`) | orange | `#FF8C00` | `INVENTORY_COLOR` |
| Market menus and reporting (`/market`, `/economy`) | yellow | `#FFE600` | `MARKET_COLOR`   |
| Furnace menus (`/furnace`)          | purple-red  | `#FF0059` | `FURNACE_COLOR`  |
| Blast furnace menus (`/blast`)      | pure red    | `#FF0000` | `BLAST_FURNACE_COLOR` |
| Factory menus (`/factory`)          | red-orange  | `#FF4000` | `FACTORY_COLOR`  |
| Recipe book (`/recipe`)             | cyan        | `#00FFEA` | `RECIPE_COLOR`   |
| Hydraulic press menus (`/press`)    | blue        | `#0066FF` | `PRESS_COLOR`    |
| Scrapper menus (`/scrapper`)        | chartreuse  | `#9EFF00` | `SCRAPPER_COLOR` |
| Job board (`/jobboard`)             | magenta     | `#FF00AA` | `JOBBOARD_COLOR` |
| Prediction bets (`/bet`)            | violet      | `#C800FF` | `BET_COLOR`      |
| Government (`/government`, `/vote`, `/treasurer`, `/mayor`, `/bonds`) | sky blue | `#00AAFF` | `GOVERNMENT_COLOR` |
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

`/economy` is the one command that shares a color with another rather than
claiming its own, and it is a sharing rather than an exception: it is not a new
feature area. It lives in `cogs/economy.py` beside `/market`, every figure it
reports is the market's own faucet-and-sink ledger from docs/market.md read
from a different angle, and a player running it is reading the market rather
than something new. That is the same relationship `/balance` has to
`/inventory` on the inventory orange - one feature, two views - and the yellow
row above now covers the market's reporting as well as its menus.

What the shared color costs is worth stating, because it is a real cost:
`/economy status`, `/economy gdp` and `/market status` are now three visually
identical embeds, and they are commands a player will run back to back. **The
author line has to carry the whole distinction on its own**, so each names its
command outright - `📊 Economy • <server name>` and `📊 Economy • GDP •
<server name>` - rather than only the server, and they have to keep doing so.
Anything that makes those headings similar makes the commands
indistinguishable.

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
