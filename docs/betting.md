# Prediction Bets

This document explains the rules behind `/bet` (1.4) and the reasoning that
picked them. As with `docs/market.md`, treat it as the intended behaviour to
evaluate the code against — the *why* lives here, not in code comments.

All bet embeds are violet, `#C800FF` (see `docs/stylization.md`).

---

## 1. The constraint everything follows from

**A bet creates no currency and destroys none.** It is a transfer between
players and nothing else — the same economic status a `/donate player` transfer
has (`cogs/donate.py`), and the reason neither `currency_minted_total` nor
`currency_burned_total` is touched anywhere in the feature.

That was the requirement the feature was specified under, and almost every
decision below is downstream of it rather than of anything about gambling.

### There is no house cut

The obvious design would take a percentage of each pot and burn it. By
`docs/market.md` section 1's argument that would be a *good* sink: players
would be paying for something they actively wanted rather than a toll, which is
exactly the test that section sets.

It was ruled out anyway, because a rake means the pot paid out is smaller than
the pot staked, and "no currency is destroyed" was the harder constraint. The
consequence worth stating plainly is that **`/bet` is the first mechanic in the
game that is neither a faucet nor a sink.** It moves money around a server
without changing how much of it there is. A server that bets heavily is not
inflating and not deflating; it is redistributing, and the economy statistics in
`docs/market.md` section 4 correctly ignore it.

---

## 2. Parimutuel, not fixed odds

Everything staked goes into one pot. Whoever is on the winning side splits the
whole pot in proportion to what they put in.

A winner who staked `s` out of a winning pool `w`, where the pot is `P`, is paid

```
s × P / w
```

which is their own stake back plus their share of what was bet against them.

This is the only structure that pays out exactly the pot without an outside
bankroll. Fixed odds — "I'll give you 3:1" — require somebody to cover the
difference when the book is unbalanced, and there is nobody here to cover it:
the bot holds no treasury it could lose from, and making the server's market
stock the counterparty would mean a bet that mints currency whenever the odds
were wrong.

### Odds are quoted as a total return

The embeds show **one** number per side: `11.00x returned`. That is the whole
amount that comes back, stake included — so 10 staked into a for-pool of 10
against an against-pool of 100 returns 110.

Quoted this way on purpose. The alternative convention states the *profit*
("10x", or "1:10"), and then every figure on screen has to say which of the two
it is. One number that always means the same thing is worth more than matching
any particular bookmaker's house style.

The odds are live and they move. A wager arriving on your side shares the pot
more ways; one arriving against you makes the pot bigger. **What is locked in
when you bet is your stake and your side, never the odds you saw** — and the
status embed says so.

---

## 3. Why stakes are integer cents

A proportional split does not generally land on whole cents, and floats do not
reliably add back up to themselves. Since the one thing this feature promises is
that the pot pays out exactly what went into it, the accounting is integers:
`prediction_wagers.stake_cents`.

The split uses **floor plus largest remainder** (`utils/betting.py: apportion`):
each winner takes the floor of their exact share, and the leftover cents go one
each to the largest discarded fractions, ties broken by larger stake and then by
earlier wager.

The two obvious alternatives are both wrong, and wrong in opposite directions —
rounding each share independently can pay out more or less than the pot, and
truncating every share destroys the remainder. Largest remainder is exact by
construction: the leftover is the sum of fractions each below one cent, so there
are strictly fewer leftover cents than winners and nobody is owed two of them.

The tie-break matters because rows come back in whatever order SQLite hands them
over, and a payout that moved with that order would be one nobody could
reproduce. `tests/test_betting_math.py` pins both the exactness and the
order-independence.

Cents rather than `PLAYER_PRICE_SCALE` ten-thousandths (`data/materials.py`)
because a stake is a sum of money somebody typed, not a unit price that has to
fit inside a band one cent wide. The two scales exist for different jobs, which
is why `circulating_currency` takes each escrow in its own unit rather than one
pre-summed total.

---

## 4. Stakes are escrowed

A wager deducts the stake from `server_currency_balances` immediately and holds
it on the wager row until the bet settles. Without that, somebody could stake
500, spend it at `/market buy`, and leave a bet that cannot pay out — the same
failure an unfunded `/market order` bid would be, and the same fix
(`database/schema.sql`, `market_orders`).

**That escrow is not a burn.** The currency has left its owner's balance but not
the economy, so `circulating_currency` in `utils/db_helpers.py` adds it back.
Without that term a server's money supply would appear to shrink every time
somebody bet and recover when the bet resolved. `/economy status` and the Ops
dashboard both read that one function for the same reason `slot_progress` is
one function.

---

## 5. The rules that follow from conservation

Three cases have no discretion in them at all — the arithmetic decides, and the
only design work was noticing that.

**Nobody took the other side.** The pot is the winners' own money, so the
formula pays each of them exactly what they staked. Nothing special happens; the
embed just says so rather than printing the technically-true `1.00x`.

**Nobody took the side that won.** There is nothing to pay, the losing stakes
cannot go to a side with no backers, and keeping them would destroy currency. So
the bet is **voided and refunded in full**. It reads as a let-down, which is why
the reply says plainly what happened and why.

**A cancelled bet** refunds every stake at face value. Trivially conserving —
each player gets their own money back — which is why a void needs no
apportionment at all.

---

## 6. One side, locked in

A player picks a side with their first wager and can only add to it.
`UNIQUE (bet_id, user_id)` in the schema is the whole mechanism.

Holding both sides would be harmless to the pot arithmetic — it is the player's
own money either way, and the totals still balance. It is refused because it
makes "did you win?" unanswerable for that player, and because hedging a
prediction you proposed yourself is not what the feature is for.

---

## 7. Lifecycle, and why there is no sixth loop

A bet is opened with a `closes_in` in hours, resolved to an absolute `closes_at`
at that moment. Frozen rather than stored as a duration for the reason
`daily_jobs` freezes its quantity and reward: a deadline somebody has already
bet against must not move.

Nothing sweeps for expiry. Every path that touches a bet checks `closes_at` and
flips `open → closed` there (`utils/betting.py: refresh_status`), which is the
job board's approach to posting the day's task — a bet nobody is looking at has
nothing to accrue, so a background loop would only be one more thing to keep
running. The status embed carries a Discord relative timestamp, so it counts
itself down in the reader's client without anything being rewritten.

`closes_in` is capped at two weeks. Past that an unresolved bet is holding
somebody's currency over a prediction they have forgotten making, and `/bet
cancel` is the escape hatch rather than the routine path.

### Resolution is an admin call

`/bet resolve` and `/bet cancel` need Manage Server, the same permission every
`/setup` subcommand needs. An admin who has money on the bet may still resolve
it: the trust model here is "your admins run your server", the same one that
lets them set every fee in the game. A server that does not trust its admins has
a larger problem than this command.

---

## 8. Delivery: buttons without a channel post

Opening a bet does **not** post into a channel. It raises an ordinary server
notice (`utils/notifications.py`) carrying an `action_key`, and
`utils/responses.py` hangs the two buttons on whatever reply that notice next
rides along with.

Two things drove that. A bot posting unbidden into a channel overrides a
server's `/setup messages private` setting, which is a setting about exactly
this. And the notice rows are fetched on every successful command anyway, so
attaching buttons to one costs no extra query — whereas asking "does this server
have an open bet?" on the reply path would charge every command in every server
for a feature most of them never use. That costing discipline is the same one
behind `guilds_with_queued_work` and the furnace's `_auto_smelt_pass`.

What it inherits is the notice feed's shape: **only the newest notice of a feed
is shown, and only once.** So the buttons are a nudge, never the only way in.
`/bet status` carries the same two on a reply anybody can ask for at any time,
and `/bet place` needs no buttons at all.

The buttons are `discord.ui.DynamicItem`s rather than views held in memory. A
bet outlives the process; a view stored per message would not, and
re-registering one per open bet at startup would mean holding every live bet in
memory to do it. A dynamic item carries its bet id in its own `custom_id` and is
matched by pattern on arrival, which keeps working after a restart and after the
view it arrived on has expired — discord.py matches it before it looks up the
message's view at all (`discord/ui/view.py`: `dispatch_view` calls
`dispatch_dynamic_items` first).

One consequence to know about: a reply can be offered the same buttons twice —
`/bet status #12` puts bet 12's buttons on the reply, and the notice announcing
bet 12 wants to put the same two there. Discord rejects a message whose
components share a `custom_id`, so `utils/responses.py` drops the duplicate.

### Player text in front of other players

A prediction is free text, written by one player and shown to the rest. It is
the first thing in the bot that is, so two guards had no precedent to follow and
are stated here: the prediction is escaped with `discord.utils.escape_markdown`
so it cannot restyle the embed quoting it, and every send carrying one passes
`allowed_mentions=discord.AllowedMentions.none()` so it cannot ping anybody.

---

## 9. What is deliberately not here

- **No rake, and so no sink** — section 1.
- **No bot-as-counterparty**, no fixed odds, no "the house will take the other
  side if nobody else does" — section 2.
- **No cross-server bets.** A server's currency is its own, exactly as with
  `/donate` and the market.
- **No partial cash-out.** A stake is in until the bet settles; the escrow and
  the odds both assume it.
- **No automatic resolution.** Nothing the bot can observe tells it whether an
  arbitrary prediction came true, and a feature that guessed would be worse than
  one that asks.
