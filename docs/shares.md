# Server Shares (1.6)

A server's government sells **shares of the server for DragonCoin**, and every
share is owed `SHARE_REVENUE_PERCENT` (1%) of the server's government revenue,
forever, paid hourly in DragonCoin. Members invest in their server - with
DragonCoin they may have earned in another one - the server raises capital, and
investors earn more the busier the server gets.

Code: `utils/shares.py` (every rule and every currency movement),
`cogs/shares.py` (`/shares`, the hourly loop), and the Treasurer's and Mayor's
commands in `cogs/government.py`. Tests: `tests/test_shares.py`.

## 1. Where the return comes from

This is the question the feature turns on, and the first design answered it
wrongly: the owner's idea was that the DragonCoin would "grow alongside the
server's GDP".

**GDP is a measurement, not money.** It records what was produced, at market
prices (`utils/production_ledger.py`). Nobody holds it, so nothing can be paid
out of it. Paying a share "x% of GDP" would mean creating that DragonCoin from
nothing, and DragonCoin converts into every server's currency at
`DRAGONCOIN_RATE` (docs/market.md section 2) - so it would inflate every server
at once, not just the one that paid it.

**The return is government revenue**: the Fee Share, the Market Tax and the
Exchange Tax (docs/government.md section 2). That is real currency, collected
from real activity, and it rises when the server is busier - so it tracks GDP
without printing anything. The cycle the design intends:

1. Members buy shares. Their DragonCoin is destroyed and the server's currency,
   `DRAGONCOIN_RATE` per DragonCoin, is minted into the treasury.
2. The Mayor spends it on a project - an enhancement, a Bonanza, mining slots -
   that makes the server produce more, which raises fees and so revenue.
3. Shareholders' slice of that revenue is turned into DragonCoin for them.

**Who pays the return** is the server's future projects: revenue the Mayor
would have spent goes to shareholders instead. In exchange the server gets
money now. That is the trade a bond makes (docs/government.md section 4), with
more risk and more upside: a bond repays a fixed amount, a share a fixed slice.

## 2. What a share is

- **Every share of a server is identical** and is owed the same fixed slice.
  That makes them one thing to trade - `/shares buy 3`, not a choice between
  issues - and means a new sale never dilutes an old holder: it adds slices, it
  does not shrink them. The slice per share is fixed, so "terms fixed at sale"
  (the rule bonds keep) holds without any per-issue record.
- **Shares pay forever** - the owner's choice. The only way a server gets its
  revenue back is a **buyback** (section 4).
- **The count that exists is never stored**: it is summed from
  `share_holdings` on read (`outstanding_shares`), as `slot_progress` is.

## 3. The Treasurer and the Mayor

- **The Treasurer sets the ceiling** (`/treasurer shares`): how many shares may
  exist, 0 to `MAX_SHARES` (40), so at most 40% of revenue can ever be promised.
  It changes once per game day like every other setting. Lowering it below what
  exists **cancels nothing** - a share is owed forever - and only stops sales.
  With identical shares the ceiling *is* the dividend rate the Treasurer
  controls.
- **The Mayor sells** (`/mayor shares <quantity> <price>`): new shares, within
  the room under the ceiling, at a DragonCoin price per share. 0 withdraws the
  sale. The room is checked when the sale opens and enforced at every purchase
  (`government_sale`), since the Treasurer may lower the ceiling in between.
  **A sale is the Mayor's**: it ends when the Mayor changes, with the bond sale,
  through `end_mayor_sales` - the one place both are withdrawn.
- **The Mayor buys back** (`/mayor buyback <quantity> <max_price>`): listed
  shares, cheapest first, paid out of the treasury, retired on the spot.

## 4. The price floor

A Mayor could sell 40% of the server's revenue to their own alt for 0.01 a
share. So **a sale may not ask less than `price_floor_cents`**: one year
(`SHARE_FLOOR_WEEKS`, 52) of the dividend a share would have earned at the
revenue of the last `SHARE_FLOOR_WINDOW_DAYS` (7) complete game days, rounded up
to the next hundredth of a DragonCoin, never under 0.01. That caps the yield a
new share can be sold at to about 100% a year at last week's pace.

**A buyback may not pay more than the same figure.** Without it the Mayor could
list their own shares at a fantastic price and buy them with the treasury.
With it, the most a buyback ever pays is roughly what the share would have
cost the server in dividends over a year anyway.

The floor reads *all* revenue, shareholders' slice included
(`government_tax_daily.amount + dividends`). The other defences: both officers
have a hand in every sale, `MAX_SHARES` caps the total, and the weekly
election is the remedy for a government that sells cheaply.

## 5. Money flow

- **A government sale** destroys the buyer's DragonCoin and mints
  `DRAGONCOIN_RATE` currency per DragonCoin into the treasury
  (`record_minted`). It is capital, not revenue: no slice of it goes to
  shareholders and it does not count toward the debt cap. No Exchange Tax - it
  would only be paid to the same government.
- **Dividends** come off the top: `collect_revenue` sends
  `outstanding × SHARE_REVENUE_PERCENT` of every unit of revenue to
  `server_config.dividend_pool`, and only the rest reaches the repayment pool or
  the treasury. `government_tax_daily` records the two parts apart: the debt cap
  reads only what the government kept, since the shareholders' slice is not
  the government's to repay bonds with. Bonds are repaid more slowly while
  shares exist, but they are still repaid.
- **The hourly payout** (`pay_dividends`, from `cogs/shares.py`'s loop) shares
  the pool out in proportion to shares held, into each holder's `accrued`, and
  turns every whole hundredth of a DragonCoin accrued into DragonCoin: the
  currency is burned and the DragonCoin minted, as `/exchange` does. Accrued
  **exactly**, as a REAL, rather than split in whole cents by `apportion()`: a
  pool of a cent or two an hour split by largest remainder gives the odd cent to
  the largest holder every time, so a small holder would never be paid at all.
  `tests/test_shares.py` runs a thousand one-cent pools to pin it.
- **A buyback** goes through `spend_treasury` - a burn, with no mining slot
  credit, since it buys the server nothing it keeps - and pays the seller
  DragonCoin.
- **A trade between players** moves shares one way and DragonCoin the other,
  and mints and burns nothing. There is no tax on it: DragonCoin belongs to no
  server, so there is no obvious government to collect it.
- `circulating_currency` counts the dividend pool and every holder's accrual,
  which are this server's currency still owed to somebody - the argument that
  counts order escrow.

So the government's rule (docs/government.md section 3) gains two exits, both
burns: currency leaves the government only as a burn, a bond repayment, a
dividend or a buyback.

## 6. Trading and membership

- **You may only trade a server's shares in that server** - the owner's rule.
  Every trading command acts on the server it is run in, with no server
  parameter, and Discord lets only members run commands in a server, so that
  is the whole membership check (no `guild.get_member`, which CLAUDE.md
  forbids).
- **Leaving keeps your shares and your dividends.** A holder who leaves cannot
  trade until they return, but is still owed their slice; dividends are paid in
  DragonCoin, which belongs to no server. There is no freeze, unlike bonds,
  because nothing here needs protecting from a kick: kicking a shareholder
  takes nothing from them and gives the server nothing.
- **The books** are `share_listings` and `share_orders`, per server, priced in
  hundredths of a DragonCoin, built the way the player market is
  (`utils/market_book.py`): entries last `MARKET_ENTRY_LIFETIME_DAYS`, every read
  spells `LIVE_ENTRY_SQL`, five of each per player per server, and an hourly
  sweep clears expired ones. **A listing reserves shares in place** rather than
  escrowing them, so they keep earning until sold and an expired listing has
  nothing to hand back; `available_shares` is the one place the reservation is
  subtracted. **An order escrows its DragonCoin**, as a market order escrows
  currency. Buying routes across players' listings and the government's sale,
  cheapest first, players first at one price. Removing the bot from a server
  withdraws its books (bids' DragonCoin back) and leaves the shares alone.

## 7. Scale

The incentive is only as big as revenue. In the 2026-08 production backups the
busiest server banked 85.3 of fees a week (docs/government.md section 5; a live
figure, not reproducible from this repo). At 100% Fee Share one share of that
server would earn 85.3 × 52 × 1% = 44.36 a year, 0.044 DragonCoin, so a share
priced at 0.10 DragonCoin would yield about 44% a year. `SHARE_REVENUE_PERCENT`,
`MAX_SHARES` and `SHARE_FLOOR_WEEKS` were proposals at that scale and should be
re-checked against current revenue.

## 8. Ruled out

- **Paying a share out of GDP** - section 1.
- **Shares that retire at a multiple of their price** - proposed, and declined
  in favour of shares that pay forever, with buybacks as the way back.
- **Holding shares of a server you are not in** for trading purposes - the
  owner ruled that trading requires membership.
