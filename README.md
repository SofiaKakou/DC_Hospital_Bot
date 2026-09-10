# RoK Hospital Drop Bot

Reads Rise of Kingdoms **Unit Healing** screenshots posted in a Discord channel and
writes the results to a live Google Sheet: governor ID, name, heal cost in each
resource, every troop type by tier, and the total power dropped.

---

## How a submission works

A player posts their governor ID as text with **one** hospital screenshot attached:

```
1234567
[screenshot]
```

The bot replies with exactly what it read, and reacts ✅ (recorded), ⚠️ (needs more),
or ❌ (unreadable). One Discord account can submit for several IDs — one message each.

**The total is always recorded.** The "Severely Wounded Units" box at the bottom left
is printed whatever the list is scrolled to, so a submission is written as soon as
that is readable - the per-unit breakdown is a bonus, not a requirement. A row is
marked:

| Status | Meaning |
|---|---|
| `Complete` | Every troop attributed to a known unit; power is exact |
| `Totals only` | Total and cost recorded; some troops not broken down, so power is a **minimum** |
| `Needs review` | A contradiction - rows exceeding the total, or an unknown unit |

If a player wants the tier detail filled in, they send a second screenshot scrolled
to the rest of the list with the **same ID** and the bot merges them.

---

## Why the readings can be trusted

The Unit Healing window prints its own answer key. The counts in the list must add up
to the totals in the bottom-left panel:

```
Long Swordsman   47
Teutonic Knight  27     47 + 27 + 158 = 232   ->  "Severely Wounded Units 232/548,725" ✓
Crossbowman     158
Battering Ram    84                           ->  "Battering Ram Zone 84/50,000"       ✓
```

Every reading is checked against that sum before anything is written. That gives three
useful properties:

1. **Escalation is decided by arithmetic, not a confidence score.** Local Tesseract OCR
   runs first (free). If its numbers don't add up, the screenshot goes to Claude vision.
   If Claude's don't add up either, the submission is flagged for a human instead of
   being written.
2. **Neither reader is allowed to "fix" its own numbers.** The vision prompt explicitly
   forbids adjusting a digit to make the total work, because that would destroy the only
   independent evidence that the reading is correct.
3. **Siege units are separated using the unit table, not the on-screen layout.** Siege
   units are by definition the ones in the Battering Ram Zone, so there is no need to
   locate a section header in a window that is often scrolled or cropped.

A cut-off row is never guessed. If the name has scrolled out of view the bot says so and
asks for another screenshot, rather than inventing a tier and scoring power off it.

---

## Setup

### 1. Python packages

```bash
pip install -r requirements.txt
```

### 2. Tesseract (optional but recommended)

Tesseract is the free first-pass reader. Without it every screenshot goes straight to
the Claude API, which works fine but costs money per image.

Download the Windows installer from
<https://github.com/UB-Mannheim/tesseract/wiki>, install it, then set the path in `.env`:

```
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

### 3. Discord bot

1. <https://discord.com/developers/applications> → **New Application** → **Bot**.
2. Under **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**.
   The bot cannot read the governor ID out of messages without it.
3. Copy the token into `DISCORD_TOKEN`.
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions
   *Read Messages*, *Send Messages*, *Read Message History*, *Add Reactions*,
   *Attach Files*. Open the generated URL and invite the bot.
5. Right-click your submission channel → **Copy Channel ID** → `SUBMISSION_CHANNEL_ID`.

### 4. Google Sheet

1. <https://console.cloud.google.com> → new project → enable the **Google Sheets API**.
2. **Credentials → Create credentials → Service account**, then **Keys → Add key →
   JSON**. Save it next to `bot.py` as `service_account.json`.
3. Open the JSON, copy the `client_email`, and **share your spreadsheet with that email
   as an Editor**. This is the step people miss.
4. Put the sheet ID (the long part of the URL between `/d/` and `/edit`) into
   `SPREADSHEET_ID`.

### 5. Config

```bash
cp .env.example .env
```

Fill it in, then start the bot:

```bash
python bot.py
```

---

## The fill check

A submission passes when the hospital holds at least **100,000 T4 or T5 troops**, of
any troop type. Set in `data/units.json`:

```json
"high_tiers": ["T4", "T5"],
"min_high_tier_troops": 100000
```

Because that is a *minimum*, one screenshot often settles it even when the list is
scrolled:

| Situation | Verdict |
|---|---|
| 150k T4 visible | **Pass** |
| 120k T4 visible, 60k more scrolled off | **Pass** - already over the line |
| 200k T1 rams plus a token 500 T4 | **FAIL** - 99,500 short |
| Hospital only holds 40k in total | **FAIL** - cannot reach 100k |
| 60k T4 visible, 90k not broken down | **Unconfirmed** - asks for another screenshot |

The padding case is the one worth noting: a check for "is a T4 present?" would wave
it through, because there is a T4 sitting at the top of the list. Counting them
does not.

Siege never counts towards the minimum - Battering Rams are T1.

## Sheet layout

You maintain **one** tab; the bot creates and owns the other two.

**`Roster`** — you paste your kingdom scan here. Needs a header row with `ID` and `Name`;
any other columns are ignored, so a raw scan export works as-is.

| ID | Name | Power | … |
|----|------|-------|---|
| 1234567 | SomePlayer | 50,000,000 | … |

**`Submissions`** — one row per governor, updated in place on resubmission. Three
columns let you check a submission by hand:

| Column | What it is |
|---|---|
| `Screenshot` | Clickable link straight to the image. **Discord attachment URLs expire after about a day**, so this is for checking now, not later. |
| `Discord Message` | Permalink to the message. Does not expire, and shows the image, the ID they typed, who posted it and the bot's reply together — the durable audit trail. |
| `All Images` | Every attachment URL for that governor, when they sent more than one. |

The full column list:
timestamp, ID, name, Discord user, **Total In Hospital**, wounded, ram zone,
identified, unaccounted, food/wood/stone/gold, whether the cost is exact,
**total power** and whether that power is a minimum, counts by tier (T1–T5), counts
by type (Infantry/Archer/Cavalry/Siege), status, which reader was used, screenshot
links, notes.

**`Troops`** — one row per governor per unit: unit, tier, type, count, power each,
power total, ram-zone flag. Rewritten whenever that governor resubmits.

---

## Before you go live

**1. Verify the tiers in `data/units.json`. This is the most important thing on the
list** - the tier decides the power, and reading the in-game badges showed two of the
seeded tiers were simply wrong (Long Swordsman is T4, not T3; Royal Crossbowman is T5,
not T4). Both are now corrected. **Crossbowman is still unconfirmed** - its portrait
backdrop is purple, which would make it T4 rather than the T3 recorded.

To check a unit yourself, look at the roman numeral on its portrait, or the backdrop
colour: grey T1, green T2, blue T3, purple T4, gold T5.

Two further things are seeded defaults, not verified facts:

- `tier_power` — currently `T1=1, T2=2, T3=4, T4=10, T5=20`. Confirm against your own
  numbers before trusting the power column.
- T4/T5 unit names are **civilisation-specific**, so the list is certainly incomplete for
  a whole kingdom. The bot never guesses an unknown name — it flags it, and an admin adds
  it permanently:

  ```
  /hospital learn unit:Onna-Musha tier:T4 troop_type:Archer
  ```

  Set `"verified_by_human": true` once you've checked it; the bot warns on startup until
  you do.

  Two specific things to confirm: **Royal Crossbowman** is recorded as T4 Archer,
  inferred from a calibration screenshot rather than looked up. And the `Siege`
  entry in `type_resources` is a guess - no single-troop siege screenshot was
  available to measure it.

**2. Test on your own screenshots** before pointing the bot at a live channel:

```bash
python tools/try_image.py path/to/shot1.png path/to/shot2.png
```

It prints every row, tier, type and power total plus whether the checksum reconciled —
no Discord, no sheet, no API calls if you pass `--no-vision`.

**3. Run the tests** after editing anything:

```bash
python -m pytest tests/ -q
```

---

## Commands

| Command | Who | Does |
|---|---|---|
| `/hospital status <id>` | anyone | Show the open (partial) submission for an ID |
| `/hospital health` | anyone | Which readers and integrations are live |
| `/hospital learn <unit> <tier> <type>` | admin | Teach a new unit name permanently |
| `/hospital reset <id>` | admin | Discard an open submission and start over |
| `/hospital refresh` | admin | Re-read the roster tab immediately |

Admin means *Manage Server*, or the role in `ADMIN_ROLE_ID`.

---

## Measured accuracy

Against the eleven sample screenshots in `tests/images/`, reading locally with
Tesseract and no API calls:

| | result |
|---|---|
| Troop counts and totals | correct on every sample |
| Heal-cost values | 41 of 43 correct, 0 blank, 2 wrong |
| Tier read from the portrait alone (name table blinded) | 9 of 16, **0 wrong**, 7 unknown |

The five that don't reconcile from one image are scrolled so a row is off-screen.
That is the multi-screenshot case, not a misread: the bot asks for another shot and
merges them. Two real screenshots merging into a complete 316-troop, 1,174-power
submission is covered by `test_two_screenshots_merge_into_one_complete_submission`.

Re-run these numbers any time with:

```bash
python tools/score_costs.py
```

## How the window is read

Nothing that matters depends on the unit's name. There are 20 civilisations and
the game ships in many languages - one of the sample screenshots is in Vietnamese
("Kiem si guom dai") - so names are treated as a label, not as data.

**Tier** comes from the portrait's rarity colour: grey T1, green T2, blue T3,
purple T4, gold T5. The portrait is located by finding its gold frame, which is a
3D bevel spanning hue 25-68 (the bright highlight *and* the darker shadow - reading
only the highlight was why this failed for a long time). Where the colour cannot be
read confidently the tier is left unknown, and the unit-name table is the fallback.

**Heal-cost columns** come from the resource icons, each read beside its own value:

| icon | signature |
|---|---|
| food | green leaves + yellow corn |
| wood | orange log |
| stone | grey-blue rock (pure-white digits excluded, or the text reads as stone) |
| gold | yellow coin with an orange rim |

This replaced working the columns out from the units' troop types, which needed the
name: on the Vietnamese screenshot the bot simply gave up and recorded no cost.

**Troop type** (Infantry/Archer/Cavalry/Siege) is recorded for information only.
Nothing depends on it - not the fill check, not the power figure. It comes from the
name table and is unreliable for unfamiliar civilisations.

## Known limits

- **Tier is read from the portrait about half the time.** The rest fall back to the
  unit-name table. Crucially it is never *wrong* - an unclear portrait reports no
  tier, and the fill check treats an unknown tier as uncertain and asks for another
  screenshot, rather than passing a hospital padded with cheap troops. The roman
  numeral on the badge was tried as a second signal and abandoned: Tesseract scored
  0 of 7 on those stylised glyphs.
- **Occasional digit misreads in the cost strip.** One of the 38 sample values
  reads `8.2K` as `3.2K`. There is no arithmetic in the window that can catch
  this the way the troop checksum catches a bad count, so treat the resource
  columns as good-but-not-guaranteed. The troop and power figures are checksum-
  verified; the costs are not.
- **Heal costs over 999 are rounded by the game itself.** The window shows
  `26.6K`, so the true cost is somewhere in 26,550-26,649. The bot records 26,600
  and marks *RSS Exact? = No*. Values under 1,000 (like `584`) are exact.
- **Power values and T4/T5 tiers are unverified defaults.** See "Before you go live".
- **Sessions are in-memory.** A partial submission is lost if the bot restarts;
  completed ones are already on the sheet. `SESSION_TIMEOUT_MINUTES` (default 30)
  controls how long a partial stays open.

## Cost

Only screenshots that fail the local checksum reach the API. Each one is a single
image plus a short prompt — a few cents at most on `claude-opus-5`. To cut that,
set `VISION_MODEL=claude-sonnet-5` in `.env`; verify accuracy with `tools/try_image.py`
first.
