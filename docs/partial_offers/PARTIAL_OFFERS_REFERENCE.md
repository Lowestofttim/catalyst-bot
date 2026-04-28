# Chia Partial Offers Reference

> Plain Markdown transcription of the partial-offers reference notes used during planning. Treat external protocol details as planning context and verify against upstream CHIP/Sage sources before implementation.

What partial offers are, how they work, why they matter, and how they transform decentralised trading on Chia.

April 2026

## Table of Contents

## 1. Executive Summary

Partial offers are a new type of on-chain primitive for the Chia blockchain that allow an offer to be filled incrementally by one or more counterparties, rather than requiring the entire offer to be taken in a single atomic transaction. Introduced through CHIP-0052 (authored by Yakuhito, creator of TibetSwap), partial offers represent a major evolution of Chia's already-unique offer system.

Where standard Chia offers operate on a fill-or-kill basis, partial offers persist on-chain as a special coin that holds the remaining balance of an unfilled trade. Each time a taker fills a portion of the offer, the coin is spent and a new partial coin is created with the remainder, creating a transparent, auditable chain of fills until the offer is fully completed or cancelled by the maker.

This innovation enables professional market-making, limit order book behaviour, and decentralised exchange liquidity that was previously impossible without custodial intermediaries, all while preserving Chia's core values of trustlessness, self-custody, and atomic settlement.

Key Facts at a Glance

CHIP: 0052 (authored by Yakuhito, September 2025)

Status: In Review (as of Q4 2025)

Puzzle size: ~438 bytes in Chialisp

Cost: ~59.9 million CLVM cost units per partial fill

Asset support: One asset for one asset (single pair per offer)

Discovery: On-chain wallet hints (~100 bytes) enable automatic discovery

Cancellation: Maker spends the partial coin to cancel at any time

Compatibility: Works alongside standard fill-or-kill offers

## 2. Background: Standard Chia Offers

To understand partial offers, it is essential to first understand the standard Chia offer system that launched in January 2022 and underpins all peer-to-peer trading on the Chia blockchain.

2.1 What Are Standard Chia Offers?

A Chia offer is a trustless, peer-to-peer mechanism for exchanging assets without requiring a centralised exchange or intermediary. Two parties, a maker (who creates the offer) and a taker (who accepts it), can trade any combination of Chia assets including XCH, CATs (Chia Asset Tokens), NFTs, and DataLayer singletons.

An offer file is a string of characters representing an incomplete and partially signed spend bundle. The maker generates this file and can share it through any channel: email, QR code, a DEX aggregator website, or even printed on paper. Any taker who receives the file and agrees to the terms can complete and broadcast the transaction.

2.2 How Standard Offers Work

The offer mechanism is built on Chia's coin-set model and the settlement payments puzzle. The flow works as follows:

The maker selects coins they wish to offer and creates notarized coin payments specifying what they want in return.

A nonce is generated from the treehash of a sorted list of the coin IDs being offered. This cryptographically binds the offer to those specific coins, so any change to the offered coins invalidates the offer.

The maker's spend bundle pays to the settlement payments puzzle hash and is encoded as an offer file.

The taker constructs a complementary spend bundle that satisfies the maker's payment requirements.

Both sides are combined and broadcast. The transaction must settle atomically in the same block, meaning both sides complete simultaneously or neither does.

### The settlement payments puzzle processes notarized payments structured as

((Nonce . ((PuzzleHash1 Amount1 Memo?) (PuzzleHash2 Amount2 Memo?))) ...)

For each notarized payment set, the puzzle generates a CREATE_PUZZLE_ANNOUNCEMENT condition and a CREATE_COIN condition, ensuring all payments are verified and created together atomically.

2.3 Key Properties of Standard Offers

Atomic settlement: The entire trade completes or nothing happens (fill-or-kill).

Trustless: Neither maker nor taker needs to trust the other. The math enforces the terms.

No custodial risk: Funds never pass through an intermediary.

Cancellation: The maker cancels by spending any of the offered coins, which invalidates the offer file.

Immutable terms: Any alteration to the offer file invalidates it.

Offer states: PENDING_ACCEPT, PENDING_CONFIRM, PENDING_CANCEL, CANCELLED, CONFIRMED, FAILED.

Asset flexibility: Any combination of XCH, CATs, NFTs, or DataLayer singletons.

The Limitation Standard Offers Could Not Solve

Standard offers are fill-or-kill: a taker must take the entire offer or none of it. This makes large offers illiquid, as a taker willing to buy only a portion has no mechanism to do so. A market maker wanting to offer 1,000 XCH for USDS would need a single counterparty willing to take all 1,000 XCH at once, or would have to manually split the offer into many smaller pieces and manage them individually.

Partial offers solve this problem by allowing any amount to be filled at any time.

## 3. Partial Offers: CHIP-0052

CHIP-0052, titled Partial Offers, was authored by Yakuhito and submitted as a draft on September 22, 2025, as part of an active collaboration with Chia Network. It introduces a new on-chain primitive that enables any portion of an offer to be filled by any counterparty, at any time, without requiring the maker to be online or to manually manage the remaining balance.

3.1 The Core Concept

A partial offer is not a file that sits off-chain (like a standard offer). It is an on-chain coin, called the partial coin, that acts as the living representation of the outstanding offer. The partial coin holds the remaining amount the maker wishes to trade and enforces the exchange rate.

When a taker fills the partial offer (in full or in part), they spend the partial coin. The spend produces two outputs:

A payment to the maker for the portion of the offer that was filled.

A new partial coin with the remaining balance, which persists on-chain and is available for the next taker.

This process continues until the offer is either completely filled or the maker cancels it by spending the coin themselves.

3.2 Step-by-Step: How a Partial Offer Works

### Step 1: The Maker Creates the Offer

The maker constructs an off-chain offer string that describes the exchange rate: how much of asset A they are offering per unit of asset B. This string communicates the terms but the actual offer exists as a coin on-chain. The maker funds the partial coin with the assets they want to trade.

### Step 2: The Partial Coin Appears On-Chain

The partial coin is a Chialisp puzzle (~438 bytes) that encodes the maker's terms. It is visible on-chain and discoverable by wallets via a hint mechanism (~100 bytes), meaning wallets do not need to rely on external offer-file backups or aggregator databases to find outstanding partial offers. They can be discovered simply by scanning the blockchain.

### Step 3: A Taker Accepts (Full or Partial)

A taker decides how much of the offer they want to fill. Their wallet calculates the minimum they must provide to receive their desired amount of the maker's asset, based on the exchange rate encoded in the puzzle:

taker_input = (other_asset_amount * PRICE_PRECISION) / PRECISION

Rounding is always in favour of the partial coin (the maker), ensuring the exchange rate is never violated in the taker's favour.

### Step 4: The Coin is Spent and a New One is Created

The taker broadcasts a transaction that spends the current partial coin. This transaction simultaneously pays the maker and creates a new partial coin with the reduced balance. The offer ID changes with each fill (since it is a new coin), but the chain of fills is fully traceable on-chain.

### Step 5: The Cycle Continues

The new partial coin is immediately available for the next taker. Multiple takers can fill the same partial offer over time, each getting their portion of the maker's assets at the encoded rate.

### Step 6: Offer Completes or is Cancelled

The offer ends in one of two ways. Either a taker fills the remaining balance entirely (no new partial coin is created), or the maker spends the partial coin themselves to cancel it. Cancellation is always available to the maker, at any time, without needing a counterparty.

Key Insight: The Offer IS the Coin

In standard offers, the offer is an off-chain file. In partial offers, the offer IS an on-chain coin. This means it is always discoverable, always verifiable, and cannot be lost or misplaced. The blockchain itself acts as the order book.

## 4. Technical Architecture

4.1 The Partial Puzzle

The partial offer puzzle is a Chialisp smart contract that encodes all the rules governing the offer. Its defining characteristic is its compactness: approximately 438 bytes in standard Chialisp, with an optimised Rue-language version saving an additional ~17 bytes and reducing the CLVM execution cost by approximately 200,000 units.

### The puzzle encodes the following parameters

Parameter

Description

PRECISION

The scale factor for the asset being offered. Controls decimal precision for the maker’s asset.

PRICE_PRECISION

The exchange rate scaling factor. Determines how much of the counter-asset the taker must provide per unit of the offered asset.

Wallet Hints

~100 bytes of metadata attached to the coin for on-chain discoverability by wallets, without needing external offer file sharing.

4.2 Cost Profile

Partial offers are designed to be blockspace-efficient. The cost benchmarks (for an XCH-for-CAT pair example) are:

Partial fill transaction: approximately 59.9 million CLVM cost units (average)

Full fill transaction: approximately 58.2 million CLVM cost units (average)

These costs are consistent across multiple consecutive transactions, meaning the cost does not increase as an offer is progressively filled over time.

For context, a standard XCH transaction costs approximately 10-15 million CLVM cost units, so a partial offer fill is roughly comparable to a moderately complex DeFi operation.

4.3 Asset Pair Restriction

CHIP-0052 intentionally restricts partial offers to a single asset pair: one asset offered in exchange for one other asset. Multi-asset support is theoretically possible but would approximately double the puzzle size, consuming significantly more blockspace per transaction. The CHIP leaves multi-asset partial offers for a future standard, prioritising efficiency for the common use case of two-asset trades.

### Supported pairs include

XCH for CAT

CAT for XCH

CAT for CAT

NFTs and DataLayer singletons are not the target asset type for partial offers (they are typically traded one-for-one via standard offers).

4.4 Precision and Rounding

All amounts are denominated in mojos (the smallest unit of XCH and CATs). The puzzle handles precision through the PRECISION and PRICE_PRECISION parameters. Whenever rounding occurs, it is always rounded in favour of the partial coin (the maker's side), not the taker. This prevents any scenario where a taker could systematically extract fractionally more value than the exchange rate entitles them to through repeated fills.

### The calculation for how much counter-asset a taker must provide

required_input = ceil((desired_output * PRICE_PRECISION) / PRECISION)

4.5 Multi-Taker Scenarios and Replace-by-Fee

A nuanced situation arises when two takers attempt to fill the same partial offer simultaneously within the same block. Because the partial coin can only be spent once per block, only one taker's transaction can succeed. The mechanism for resolving this is Replace-by-Fee (RBF):

Taker A broadcasts a transaction spending the partial coin and creating a new partial coin (with the remainder).

Taker B also broadcasts a transaction spending the same partial coin.

Only one can be included in the block. The other's transaction becomes invalid.

If Taker B wants to fill the offer after Taker A's transaction is confirmed, they must spend the newly-created partial coin (not the original one).

Wallets can facilitate multi-taker fills in the same block by using the RBF mechanism: Taker B's transaction includes Taker A's spend and adds their own fill on top of the new partial coin that Taker A's spend creates.

RBF in Practice

Replace-by-Fee allows a new transaction to replace an unconfirmed transaction in the mempool if it spends at least the same coins and offers a higher fee. In the context of partial offers, this means aggregators and wallets can coordinate multiple fills within a single block by chaining their transactions together.

5. Partial Offers vs. Standard Offers

The table below compares the key properties of standard (full) Chia offers and partial offers side by side.

Feature

Standard Offers

Fill behaviour

Fill-or-kill (all or nothing)

Incremental (any amount, any time)

Where the offer lives

Off-chain (offer file / string)

On-chain (partial coin)

Discoverability

Requires external sharing or aggregator

On-chain wallet hints, blockchain-native

Multiple fills

No (one taker takes all)

Yes (unlimited takers over time)

Cancellation

Spend the offered coins

Spend the partial coin

Remaining balance management

Manual (maker splits into new offers)

Automatic (new partial coin created)

Professional market-making

Complex and manual

Purpose-built for this use case

Offer ID stability

Fixed (tied to offer file)

Changes with each fill (new coin ID)

Traceability on-chain

Limited

Full chain of fills traceable

Maker online required?

No

No

Custodial risk

None

None

Atomic settlement

Yes

Yes (per fill)

Compatibility with wallets

All Chia wallets

Requires wallet support for CHIP-0052

Standard offers remain the default for most wallet users. Partial offers are designed to complement them: wallets can default to fill-or-kill for standard users while allowing advanced users and market-making software to create and interact with partial offers.

## 6. Related CHIPs and Context

6.1 CHIP-0042: Protected Single Sided Offers

CHIP-0042 addresses a different but related problem: the security vulnerability in single-sided offers (also called one-sided offers). A single-sided offer is where the maker offers assets but receives nothing in return, used for giveaways, invoicing, or onboarding new users.

The security problem: in a standard single-sided offer, anyone with access to the pending transaction can use Replace-by-Fee to redirect the assets to their own wallet before the transaction is confirmed. CHIP-0042 solves this by introducing an intermediate ephemeral coin spend that generates an aggregated signature, making it cryptographically impossible to redirect the assets without the original offer file's private key.

CHIP-0042 is distinct from CHIP-0052 and addresses a different use case. The two CHIPs can coexist and complement each other in the Chia ecosystem.

6.2 The Original Chia Offer System

The base offer system was launched in January 2022 and was a groundbreaking innovation in blockchain design, enabling fully trustless peer-to-peer asset exchange without any smart contract protocol risk. The settlement payments puzzle at its core is the foundation upon which both CHIP-0042 and CHIP-0052 build.

6.3 One Market

Chia Network has articulated a vision called One Market: because all offer liquidity is shared across the entire Chia ecosystem, any aggregator (dexie, SpaceScan, or a future platform) can see and fill the same offers. There is no liquidity fragmentation across platforms as occurs in multi-chain or multi-protocol DeFi. Partial offers extend this vision by making liquidity more granular and enabling professional-grade order book behaviour within the One Market framework.

7. Ecosystem and Real-World Applications

7.1 TibetSwap and Yakuhito

TibetSwap is Chia's leading decentralised exchange, operating as an Automated Market Maker (AMM) with over 50,000 XCH worth of liquidity locked in its protocol. Its creator, Yakuhito, is also the author of CHIP-0052. This combination of AMM expertise and protocol design authority gives partial offers a natural ecosystem home from day one.

Yakuhito's vision for how partial offers and AMMs will coexist is clear: AMMs will serve the average user who wants simple swaps, while partial offers will serve professional market-makers who want to deploy capital with specific price limits and strategies. Aggregators will route trades through both mechanisms simultaneously to find the best available price for any given trade.

TibetSwap stated upon the announcement: We are excited for the potential of partial offers to deliver the experience of CEX orders, on-chain and without a middleman. The primitive can become an essential liquidity source for One Market.

7.2 dexie

dexie is a Chia offer aggregator and DEX that indexes offer files and allows users to browse and fill them through a web interface. It has been a pioneer in demonstrating how Chia's offer-based liquidity model differs fundamentally from AMM-based DeFi: dexie benefits from liquidity posted on other aggregators and vice versa, since all aggregators index the same on-chain offers.

dexie has also introduced features like the auto dexie tool, which automatically creates and manages offer files based on a user's chosen trading strategy, effectively enabling non-custodial automated market-making. Partial offers strengthen this model by reducing the management overhead for liquidity providers.

dexie has also introduced a Liquidity Rewards Programme that incentivises market makers with DBX token rewards based on the proximity of their offer prices to the current market rate and the size of their offers.

7.3 Professional Market-Making Use Cases

Partial offers are purpose-built for scenarios where a sophisticated participant wants to offer a large amount of an asset at a fixed price, available to anyone in any size. Real-world applications include:

Limit orders: A market maker sets a bid or ask at a specific price. Any counterparty can fill any portion at any time, exactly like a limit order on a centralised exchange.

OTC desks: A large holder of XCH or a CAT who wants to sell gradually without impacting the spot price can post a partial offer and let the market fill it over days or weeks.

Liquidity provision: Instead of depositing into an AMM pool, a sophisticated market maker can place a partial offer at a specific price point and earn the spread without relinquishing custody of their assets.

Automated trading strategies: Software can monitor the on-chain state of partial offers and create, cancel, or replace them dynamically as market conditions change.

Cross-platform liquidity: Because partial offers are discoverable on-chain, the same offer is visible to every aggregator simultaneously, maximising the chance of a fill.

7.4 Wallet Support

CHIP-0052 is in review status as of late 2025. Wallet support will follow formal approval and implementation. The Chia ecosystem's major wallets include:

Chia Reference Wallet: The official wallet maintained by Chia Network, expected to add support following CHIP approval.

Sage Wallet: A cross-platform light wallet with full offer support (CATs, NFTs, standard offers, DIDs). Positioned to be an early adopter of partial offers.

Goby: A browser-based wallet similar to MetaMask for Chia.

Pawket: A mobile-friendly Chia wallet.

8. Security Considerations

8.1 Arithmetic Safety

The partial puzzle includes explicit protections against arithmetic underflow and overflow errors. All rounding is performed in the partial coin's favour, meaning no taker can systematically extract more value than the exchange rate entitles them to, even through repeated small fills designed to exploit rounding.

8.2 No Custodial Risk

Like all Chia offer types, partial offers never place assets in a trusted intermediary's custody. The partial coin is a pure on-chain construct governed entirely by the Chialisp puzzle. The maker retains the ability to cancel at any time by spending the coin; no third party can prevent this.

8.3 Replace-by-Fee Attack Resistance

The partial offer mechanism is inherently resistant to the RBF attack that affects single-sided standard offers (addressed separately by CHIP-0042). Because partial offers involve a taker providing value to the maker, the transaction includes both sides' signatures, making it impossible for an attacker to redirect the maker's assets without also providing the required counter-asset.

8.4 Price Manipulation

The exchange rate is encoded in the puzzle at creation time and cannot be changed without creating a new partial coin (which would require spending the existing one, effectively cancelling the original offer). Takers cannot unilaterally change the price; they can only decide how much of the offer to fill at the maker's stated rate.

8.5 Maker Control

The maker retains full control over the partial coin at all times. They can cancel it instantly by spending it (creating no successor coin). There is no time lock or minimum duration that would prevent the maker from exiting. This means partial offers do not expose market makers to the impermanent loss risk associated with AMM liquidity provision.

8.6 On-Chain Discoverability vs. Privacy

The wallet hints that make partial offers discoverable on-chain also mean the maker's offer terms are publicly visible to anyone monitoring the blockchain. Market makers should treat their partial offer positions as public information, similar to placing a visible bid/ask on a public order book. This is analogous to a centralised exchange limit order, which is also public, and should not be considered a privacy vulnerability so much as an expected property of a transparent, on-chain order book.

9. Known Limitations and Open Questions

9.1 Single Asset Pair Only

Each partial offer can only trade one asset for one other asset. A market maker who wants to offer XCH for multiple different CATs must create a separate partial offer for each pair. Multi-asset partial offers are a known future enhancement but are not part of CHIP-0052.

9.2 Wallet Support Not Yet Universal

As of the date of this document, CHIP-0052 is in review and not yet finalized. Full wallet support across all Chia wallets will take time to roll out following approval. During this period, users wishing to interact with partial offers may need to use the command-line tools from the reference implementation (available at github.com/Yakuhito/partial) or wait for their wallet of choice to add support.

9.3 Offer ID Changes with Each Fill

Unlike a standard offer (which has a stable off-chain identifier), the partial offer changes its on-chain coin ID with every fill. While the chain of fills is fully traceable, tooling that tracks offers by ID will need to follow the coin lineage rather than a single static identifier. This is a known design consideration and is explicitly addressed in the CHIP.

9.4 Simultaneous Fill Complexity

While the RBF mechanism allows multiple takers to fill a partial offer within the same block, this requires wallet coordination. Simple wallets that do not implement RBF chaining will experience failed transactions if they attempt to fill a partial offer that another taker is simultaneously filling. This is expected to improve as wallet implementations mature.

9.5 Mempool Eviction

If a partial fill transaction is pending in the mempool and the block fills up, the transaction may be evicted and will need to be rebroadcast. This is a general Chia mempool property rather than a partial offer-specific issue, but market makers should be aware of it when operating in high-activity periods.

## 10. Summary and Significance

Partial offers represent the most significant extension of Chia's offer system since its launch in 2022. By moving the offer state on-chain into a Chialisp puzzle, they achieve something that no off-chain offer file system can: a truly permanent, discoverable, incrementally-fillable limit order that persists on the blockchain until it is cancelled or completely filled, with no maker interaction required between fills.

### The key innovations are

On-chain persistence: The offer does not expire when the maker goes offline. It lives as a coin until spent.

Incremental filling: Any amount can be taken by any counterparty at any time, enabling genuine market-maker behaviour.

Automatic bookkeeping: The blockchain automatically tracks the remaining balance via the successor partial coin. No off-chain database is required.

Trustless throughout: Every fill is a standard Chia spend bundle with atomic settlement. There is no moment of counterparty risk.

Ecosystem synergy: Partial offers integrate into the One Market vision, where all Chia aggregators share the same liquidity pool, making every partial offer visible to the entire ecosystem simultaneously.

For the Chia blockchain, partial offers bridge the gap between the simplicity of the existing offer system and the sophistication of professional order-book trading found on centralised exchanges, without sacrificing self-custody, trustlessness, or the unique shared-liquidity properties that make Chia's DeFi model distinct.

## In One Sentence

Partial offers give Chia a native, on-chain, trustless, self-custodial limit order system where any portion of any offer can be filled by anyone at any time, without the maker needing to do anything after posting.

## 11. Sources and Further Reading

### The following sources were used in the preparation of this document

1. CHIP-0052: Partial Offers — github.com/Chia-Network/chips/pull/174

2. Partial Reference Implementation (Yakuhito) — github.com/Yakuhito/partial

3. Offers — Chialisp Documentation — chialisp.com/offers/

4. Chia Offers Academy — Chia Documentation — docs.chia.net/academy-offers/

5. CHIP-0042: Protected Single Sided Offers — github.com/Chia-Network/chips/blob/main/CHIPs/chip-0042.md

6. Decentralized Liquidity with Chia Offers — dexie’s Notes — notes.dexie.space/p/decentralized-liquidity-with-chia

7. Chia Offers: Unlocking Global Peer-to-Peer Markets — chia.net/2024/02/16/chia-offers-unlocking-global-peer-to-peer-markets/

8. Interview: Yakuhito, creator of TibetSwap and warp.green — xch.today/2025/09/23/peer-to-peer-an-interview-with-yakuhito

9. TibetSwap announcement on X (September 2025) — x.com/TibetSwap/status/1969928612758618517

10. Ideas about Offers on Chia (richardkiss) — gist.github.com/richardkiss/0e165ce111ee50e66eb27d2f45f3cc5e

End of Document
