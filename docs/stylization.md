# Stylization

The single source of truth for the bot's visual identity. Every embed is
built through `utils/embeds.py: make_embed()`, which applies a palette color
and the standard footer automatically.

## Footer

All embeds end with the footer:

> Dragonhoard by Isaac Day · Version X.X

The version number comes from `config.VERSION` — bump it there when
releasing a new version.

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
| Factory menus (`/factory`)          | red-orange  | `#FF4000` | `FACTORY_COLOR`  |
| Recipe book (`/recipe`)             | cyan        | `#00FFEA` | `RECIPE_COLOR`   |
| Hydraulic press menus (`/press`)    | blue        | `#0066FF` | `PRESS_COLOR`    |
| Extras manual page (`/help extras`) | green (default) | `#00FF3C` | `DEFAULT_COLOR` |

Future games/features should claim their own fully saturated color here (and
in their design doc) before implementation.

`/honk` is the one command that sends no embed at all - its response is the
audio clip on its own, and a title card above the player would only get in the
way of it.

The manual is the one deliberate exception to "one command, one color": each of
its pages is tinted with the color of the feature it describes (the mining page
is purple, the furnace page purple-red, and so on), so the reader recognises a
game by its color before reading a word. Its own front page uses the default
green. See `SECTIONS` in `data/manual.py`.
