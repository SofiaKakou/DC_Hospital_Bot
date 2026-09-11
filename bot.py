"""Rise of Kingdoms hospital-drop bot.

Players post their governor ID plus one hospital screenshot in the submission
channel. The bot reads the screenshot, resolves the units to tier/type/power,
looks the player's name up from the roster tab, and writes the result to the sheet.

Run with:  python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

from rok import config as config_module
from rok import ocr, pipeline
from rok.session import SessionStore, Submission
from rok.sheets import SheetsClient
from rok.troop_grid import SiegeReading, check_siege_rules, read_siege
from rok.units import TIERS, TYPES, UnitTable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rok.bot")

CFG = config_module.load()
TABLE = UnitTable(CFG.units_file)

# Point pytesseract at the binary up front. The pipeline does this per call too,
# but without it here every ocr.available() check reports a working install as
# missing - a health line that lies is worse than no health line.
try:
    ocr.configure(CFG.tesseract_cmd)
except Exception:  # pytesseract absent; ocr.available() reports it correctly
    pass
SESSIONS = SessionStore(CFG.session_timeout_minutes)
SHEETS = (
    SheetsClient(
        TABLE,
        CFG.google_credentials_file,
        CFG.spreadsheet_id,
        CFG.roster_tab,
        CFG.submissions_tab,
        CFG.troops_tab,
        CFG.siege_check_tab,
    )
    if CFG.sheets_enabled
    else None
)

# A governor ID is the only long digit run players paste. Commas and the "ID:" label
# are stripped first so "ID: 1,234,567" still matches.
_ID_PATTERN = re.compile(r"\b(\d{5,12})\b")
_IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp")

OK, PARTIAL, FAIL = "\N{WHITE HEAVY CHECK MARK}", "\N{WARNING SIGN}", "\N{CROSS MARK}"
QUEUED = "\N{HOURGLASS WITH FLOWING SAND}"

# Below this, the nuanced fill_check/breakdown gate still applies. At or
# above it, a submission gets a green check outright regardless of unknown
# units or an incomplete breakdown - see process_submission for the tradeoff
# this was an explicit, informed choice against (it bypasses the tier/type
# anti-padding check for anyone clearing this floor on raw wounded troops).
SIMPLE_PASS_WOUNDED_FLOOR = 200_000

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --------------------------------------------------------------------------- #
# Submission queue
#
# Reacting and downloading the image happen the instant a message arrives -
# the queue only defers the slow, CPU-bound OCR itself. This keeps the one
# property the checksum design depends on: a player whose screenshot turns
# out to be unreadable still finds out with time left to send another one,
# just not necessarily in the next second during a burst of submissions.
# --------------------------------------------------------------------------- #


@dataclass
class SubmissionJob:
    message: discord.Message
    image_bytes: bytes
    governor_id: str
    image_url: str
    image_filename: str


QUEUE: asyncio.Queue[SubmissionJob] = asyncio.Queue()


@dataclass
class SiegeJob:
    message: discord.Message
    image_bytes: bytes
    governor_id: str
    image_url: str


SIEGE_QUEUE: asyncio.Queue[SiegeJob] = asyncio.Queue()


@dataclass
class TestJob:
    message: discord.Message
    image_bytes: bytes
    governor_id: str


# A single worker, deliberately not CFG.submission_workers: this queue was
# originally fired off with asyncio.create_task (no limit at all), which let
# an admin's test run fully concurrently with real submissions and starve
# them of CPU on a resource-constrained host - measured in production, not
# hypothetical. One worker means a test can never take capacity away from
# real traffic; it just waits its turn like everything else.
TEST_QUEUE: asyncio.Queue[TestJob] = asyncio.Queue()
_workers_started = False


def extract_governor_id(text: str) -> str | None:
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text or "")
    match = _ID_PATTERN.search(cleaned)
    return match.group(1) if match else None


def is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(_IMAGE_TYPES)


def save_submission(image_bytes: bytes, governor_id: str, filename: str) -> Path | None:
    """Keep a copy of every submitted screenshot.

    A misread can only be diagnosed against the actual pixels, and a screenshot
    that lives only in a Discord message is effectively gone. Named by governor
    and timestamp so a reported problem maps to a file.
    """
    folder = CFG.save_submissions_dir
    if folder is None:
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower() or ".png"
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            suffix = ".png"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = folder / f"{stamp}_{governor_id}{suffix}"
        path.write_bytes(image_bytes)
        return path
    except Exception as exc:  # never let bookkeeping break a submission
        log.warning("Could not save the submitted screenshot: %s", exc)
        return None


def build_embed(submission: Submission, name: str, merge_notes: list[str]) -> discord.Embed:
    summary = submission.summary
    problems = submission.missing()

    # See SIMPLE_PASS_WOUNDED_FLOOR / process_submission: a submission whose
    # raw Severely Wounded total already clears this floor is treated as
    # Recorded regardless of unknown units or an incomplete breakdown, so
    # the log entry's own colour/title agree with the reaction it gets in
    # the submission channel instead of showing a gold/red "needs attention"
    # title next to a green check.
    big_enough = (
        submission.wounded_current is not None
        and submission.wounded_current >= SIMPLE_PASS_WOUNDED_FLOOR
    )

    if submission.breakdown_complete or big_enough:
        colour, title = discord.Colour.green(), "Recorded"
    elif submission.needs_human:
        colour, title = discord.Colour.red(), "Needs review"
    elif submission.recordable:
        colour, title = discord.Colour.gold(), "Recorded - totals only"
    else:
        colour, title = discord.Colour.red(), "Could not read"

    who = f"{name} ({submission.governor_id})" if name else str(submission.governor_id)
    embed = discord.Embed(title=title, description=who, colour=colour)

    if submission.all_rows:
        lines = []
        for row in submission.all_rows:
            tier = row.tier or "??"
            kind = row.type or "unknown"
            power = f"{row.power_total:,}" if row.known else "?"
            lines.append(f"`{row.count:>6,}`  {row.name} - {tier} {kind} - {power} power")
        embed.add_field(name="Troops", value="\n".join(lines)[:1024], inline=False)

    totals = []
    if submission.total_in_hospital is not None:
        totals.append(f"**Total in hospital: {submission.total_in_hospital:,}**")
    if submission.wounded_current is not None:
        capacity = f"/{submission.wounded_capacity:,}" if submission.wounded_capacity else ""
        totals.append(f"Wounded: {submission.wounded_current:,}{capacity}")
    if submission.ram_current:
        capacity = f"/{submission.ram_capacity:,}" if submission.ram_capacity else ""
        totals.append(f"Ram zone: {submission.ram_current:,}{capacity}")

    unaccounted = submission.unaccounted or 0
    if unaccounted or summary["power_is_partial"]:
        totals.append(
            f"Power dropped: **at least {summary['total_power']:,}** "
            f"({unaccounted:,} troops not broken down)"
        )
    else:
        totals.append(f"Power dropped: **{summary['total_power']:,}**")
    embed.add_field(name="Totals", value="\n".join(totals), inline=False)

    verdict, note = submission.fill_check(TABLE)
    if verdict != "Unknown":
        icon = {"Pass": OK, "FAIL": FAIL}.get(verdict, PARTIAL)
        embed.add_field(name=f"{icon} Fill check: {verdict}", value=note, inline=False)

    def money(value: int | None) -> str:
        return f"{value:,}" if value is not None else "?"

    suffix = " *(game-rounded)*" if submission.rss_approx else ""
    embed.add_field(
        name="Heal cost",
        value=(
            f"Food {money(submission.food)} - Wood {money(submission.wood)} - "
            f"Stone {money(submission.stone)} - Gold {money(submission.gold)}{suffix}"
        ),
        inline=False,
    )

    if problems:
        embed.add_field(name="Still needed", value="\n".join(f"- {p}" for p in problems)[:1024], inline=False)
    if merge_notes:
        embed.add_field(name="Note", value="\n".join(merge_notes)[:1024], inline=False)

    read_by = "/".join(dict.fromkeys(submission.sources)) or "?"
    embed.set_footer(text=f"Read by {read_by} - {len(submission.image_urls)} screenshot(s)")
    return embed


async def log_result(
    message: discord.Message, *, content: str | None = None, embed: discord.Embed | None = None
) -> None:
    """Mirror one verification outcome to the log channel, if configured.

    Every outcome goes here - success, needs-review, unreadable, and crashed -
    not just the clean ones, so the log is a complete record rather than a
    highlight reel. A separate channel from where players submit, so an admin
    can watch every result without sitting in the (possibly noisy) submission
    channel.
    """
    if not CFG.verification_log_channel_id:
        return
    channel = client.get_channel(CFG.verification_log_channel_id)
    if channel is None:
        log.warning(
            "Verification log channel %s not found - not in this guild, or not "
            "cached yet.",
            CFG.verification_log_channel_id,
        )
        return
    attribution = f"**{message.author.display_name}** - {message.jump_url}"
    try:
        await channel.send(
            content=f"{attribution}\n{content}" if content else attribution,
            embed=embed,
        )
    except Exception:
        log.exception("Could not mirror a result to the verification log channel")


async def process_submission(job: SubmissionJob) -> None:
    """Run the actual OCR/checksum pipeline for one queued submission.

    Everything here was previously inline in on_message. Moved out so it runs
    from the queue worker instead of blocking the next message's reaction.
    """
    message = job.message
    try:
        async with message.channel.typing():
            result = await asyncio.to_thread(
                pipeline.extract,
                job.image_bytes,
                TABLE,
                anthropic_api_key=CFG.anthropic_api_key,
                vision_model=CFG.vision_model,
                tesseract_cmd=CFG.tesseract_cmd,
            )

            try:
                await message.remove_reaction(QUEUED, client.user)
            except Exception:
                pass  # reaction already gone, or we lack history - not fatal

            if result.reading.wounded_current is None:
                text = (
                    f"{FAIL} I could not read that screenshot.\n"
                    + "\n".join(f"- {p}" for p in result.problems[:3])
                    + "\nMake sure the 'Severely Wounded Units' box at the bottom left is in frame."
                )
                await message.add_reaction(FAIL)
                await log_result(message, content=text)
                return

            # Checked before a session is created for this ID, so a mistyped
            # number never gets recorded or leaves a phantom open submission
            # behind - only a genuine roster match gets that far. A lookup
            # that raised (network/API hiccup) is not the same as "checked
            # and not found", so that case falls through and still records
            # with a blank name rather than rejecting an honest submission
            # over a transient Sheets error.
            name = ""
            roster_checked = False
            if SHEETS:
                try:
                    name = await asyncio.to_thread(SHEETS.name_for, job.governor_id)
                    roster_checked = True
                except Exception as exc:
                    log.warning("Roster lookup failed: %s", exc)

            if roster_checked and not name:
                text = (
                    f"{FAIL} Governor ID `{job.governor_id}` is not on the roster. "
                    "Check the number and resubmit - nothing was recorded."
                )
                await message.add_reaction(FAIL)
                await log_result(message, content=text)
                return

            submission = SESSIONS.get_or_create(
                job.governor_id, message.author.id, message.author.display_name
            )
            merge_notes = submission.merge(result, job.image_url, message.jump_url)

            # By explicit request: a submission whose raw Severely Wounded
            # total already clears this floor gets a green check outright,
            # full stop - no unknown-unit or partial-breakdown nuance. That
            # is a deliberate trade against the tier/type padding check
            # (fill_check, still shown in the log embed below for whoever
            # wants the detail); the alternative - keeping the nuanced
            # gate - was flagged and turned down as too many false-looking
            # warnings on otherwise-fine submissions in practice.
            big_enough = (
                submission.wounded_current is not None
                and submission.wounded_current >= SIMPLE_PASS_WOUNDED_FLOOR
            )

            written = False
            if SHEETS and submission.recordable:
                try:
                    await asyncio.to_thread(SHEETS.write, submission, name)
                    written = True
                    if submission.breakdown_complete or big_enough:
                        SESSIONS.clear(job.governor_id)
                except Exception as exc:
                    log.exception("Sheet write failed")
                    merge_notes.append(f"Could not write to the sheet: {exc}")

            embed = build_embed(submission, name, merge_notes)
            if written:
                embed.set_footer(text=embed.footer.text + " - written to the sheet")

            await message.add_reaction(OK if (big_enough or submission.breakdown_complete) else PARTIAL)
            await log_result(message, embed=embed)
    except Exception as exc:
        # A crashed job must not take the worker down with it, and the player
        # deserves to know their submission didn't silently vanish.
        log.exception("Processing failed for governor %s", job.governor_id)
        try:
            await message.remove_reaction(QUEUED, client.user)
        except Exception:
            pass
        text = (
            f"{FAIL} Something went wrong while reading that screenshot ({exc}). "
            "Please try resubmitting."
        )
        try:
            await message.add_reaction(FAIL)
            await log_result(message, content=text)
        except Exception:
            log.exception("Could not even report the failure back to Discord")


async def submission_worker(worker_id: int) -> None:
    """Pull one job at a time off the queue and process it, forever.

    A bare while-True around process_submission's own try/except: that inner
    one keeps one bad job from crashing the worker, this loop is just the
    belt-and-suspenders in case something outside process_submission itself
    (queue bookkeeping) ever raises.
    """
    log.info("Submission worker %d started.", worker_id)
    while True:
        job = await QUEUE.get()
        try:
            await process_submission(job)
        except Exception:
            log.exception("Worker %d: unhandled error outside process_submission", worker_id)
        finally:
            QUEUE.task_done()


def build_siege_embed(
    governor_id: str, name: str, reading: SiegeReading, verdict: str, notes: list[str]
) -> discord.Embed:
    colour = discord.Colour.green() if verdict == "Pass" else discord.Colour.red()
    icon = OK if verdict == "Pass" else FAIL
    who = f"{name} ({governor_id})" if name else governor_id
    embed = discord.Embed(title=f"{icon} Siege check: {verdict}", description=who, colour=colour)

    lines = [f"T{t}: {reading.by_tier.get(f'T{t}', 0):,}" for t in range(1, 6)]
    embed.add_field(name="Siege by tier", value="\n".join(lines), inline=False)
    embed.add_field(name="Notes", value="\n".join(f"- {n}" for n in notes)[:1024], inline=False)
    if reading.warnings:
        embed.add_field(
            name="Warnings", value="\n".join(f"- {w}" for w in reading.warnings)[:1024], inline=False
        )
    return embed


async def process_siege_submission(job: SiegeJob) -> None:
    """Read one Troop Details screenshot and check it against the siege
    composition rules. Mirrors process_submission's shape (roster check
    before recording anything, every outcome mirrored to the log channel)
    but is otherwise independent - different screen, different sheet tab,
    no session/merge concept since one screenshot is the whole submission.
    """
    message = job.message
    try:
        async with message.channel.typing():
            reading = await asyncio.to_thread(read_siege, job.image_bytes)

            try:
                await message.remove_reaction(QUEUED, client.user)
            except Exception:
                pass

            # "No troop icons found" is read_siege's own signal that this
            # doesn't look like the right screen at all (its global frame
            # scan found nothing) - that's the one case worth rejecting
            # outright. Zero siege with no such warning is a completely
            # legitimate result, not a failure: it means the icon grid was
            # read fine and none of the icons on it were siege. Production
            # report: a player who disbands/loses all their siege units
            # doesn't just show 0 counts - the siege icon can disappear
            # from the grid entirely, which used to be indistinguishable
            # here from a genuinely unreadable screenshot and got rejected
            # instead of correctly recorded as a Pass.
            if "No troop icons found in the screenshot." in reading.warnings:
                text = (
                    f"{FAIL} I could not find any siege units in that screenshot. "
                    "Make sure it's the 'Troop Details' > 'Total Number of Units' screen."
                )
                await message.add_reaction(FAIL)
                await log_result(message, content=text)
                return

            name = ""
            roster_checked = False
            if SHEETS:
                try:
                    name = await asyncio.to_thread(SHEETS.name_for, job.governor_id)
                    roster_checked = True
                except Exception as exc:
                    log.warning("Roster lookup failed: %s", exc)

            if roster_checked and not name:
                text = (
                    f"{FAIL} Governor ID `{job.governor_id}` is not on the roster. "
                    "Check the number and resubmit - nothing was recorded."
                )
                await message.add_reaction(FAIL)
                await log_result(message, content=text)
                return

            verdict, notes = check_siege_rules(reading)

            if SHEETS:
                try:
                    await asyncio.to_thread(
                        SHEETS.write_siege,
                        job.governor_id,
                        name,
                        message.author.display_name,
                        reading.by_tier,
                        verdict,
                        "; ".join(notes),
                        job.image_url,
                        message.jump_url,
                    )
                except Exception:
                    log.exception("Siege sheet write failed")
                    notes.append("Could not write to the sheet.")

            embed = build_siege_embed(job.governor_id, name, reading, verdict, notes)
            await message.add_reaction(OK if verdict == "Pass" else FAIL)
            await log_result(message, embed=embed)
    except Exception as exc:
        log.exception("Siege processing failed for governor %s", job.governor_id)
        try:
            await message.remove_reaction(QUEUED, client.user)
        except Exception:
            pass
        text = (
            f"{FAIL} Something went wrong while reading that screenshot ({exc}). "
            "Please try resubmitting."
        )
        try:
            await message.add_reaction(FAIL)
            await log_result(message, content=text)
        except Exception:
            log.exception("Could not even report the siege failure back to Discord")


async def siege_worker(worker_id: int) -> None:
    log.info("Siege worker %d started.", worker_id)
    while True:
        job = await SIEGE_QUEUE.get()
        try:
            await process_siege_submission(job)
        except Exception:
            log.exception("Siege worker %d: unhandled error outside process_siege_submission", worker_id)
        finally:
            SIEGE_QUEUE.task_done()


async def sync_commands() -> None:
    """Publish the slash commands.

    A global sync can take Discord up to an hour to show up in the client, which
    reads as "the bot is online but has no commands". Syncing per-guild is
    immediate, so every guild the bot is in gets a copy and the global sync just
    backs it up for guilds joined later.
    """
    for guild in client.guilds:
        try:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info("Synced %d commands to %s.", len(synced), guild.name)
        except discord.Forbidden:
            log.error(
                "No permission to add commands in %s. The bot was invited without "
                "the applications.commands scope - re-invite it with the URL below.",
                guild.name,
            )
        except Exception as exc:
            log.error("Command sync failed for %s: %s", guild.name, exc)

    # Guild copies and global registrations both show in the picker, so a command
    # registered in each place appears twice. The guild copies are what we want -
    # they publish instantly - so the global list is emptied to clear the pair.
    try:
        tree.clear_commands(guild=None)
        await tree.sync()
    except Exception as exc:
        log.warning("Could not clear the global command list: %s", exc)


def invite_url() -> str:
    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=True,
        embed_links=True,
    )
    return discord.utils.oauth_url(
        client.application_id,
        permissions=permissions,
        scopes=("bot", "applications.commands"),
    )


async def campaign_embed() -> discord.Embed:
    """How the drop is going: submissions in, power dropped so far."""
    embed = discord.Embed(title="Hospital drop so far", colour=discord.Colour.blurple())
    if not SHEETS:
        embed.description = "ping (Google Sheets is not configured, so there is nothing to total)"
        return embed

    try:
        totals = await asyncio.to_thread(SHEETS.campaign_totals)
    except Exception as exc:
        embed.description = f"ping - but I could not read the sheet: {exc}"
        embed.colour = discord.Colour.red()
        return embed

    if not totals["submissions"]:
        embed.description = "ping - no submissions recorded yet."
        return embed

    embed.add_field(name="Submissions", value=f"**{totals['submissions']:,}** governors", inline=True)
    embed.add_field(name="Troops in hospitals", value=f"**{totals['troops']:,}**", inline=True)
    embed.add_field(name="Power dropped", value=f"~**{totals['power']:,}**", inline=True)

    checks = []
    if totals["passed"]:
        checks.append(f"{OK} {totals['passed']:,} passed the fill check")
    if totals["failed"]:
        checks.append(f"{FAIL} {totals['failed']:,} failed")
    pending = totals["submissions"] - totals["passed"] - totals["failed"]
    if pending:
        checks.append(f"{PARTIAL} {pending:,} unconfirmed")
    if checks:
        embed.add_field(name="Fill checks", value="\n".join(checks), inline=False)

    embed.set_footer(
        text="Power is approximate - rows still missing a tier are not counted."
    )
    return embed


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s", client.user)

    # on_ready can fire again after a reconnect; workers must only start once.
    global _workers_started
    if not _workers_started:
        for worker_id in range(CFG.submission_workers):
            client.loop.create_task(submission_worker(worker_id))
        for worker_id in range(CFG.submission_workers):
            client.loop.create_task(siege_worker(worker_id))
        client.loop.create_task(test_worker(0))
        log.info("Started %d submission worker(s), %d siege worker(s), 1 test worker.",
                 CFG.submission_workers, CFG.submission_workers)
        _workers_started = True

    await sync_commands()
    log.info("Invite URL (use this if commands are missing): %s", invite_url())
    log.info("Vision fallback: %s", "on" if CFG.vision_enabled else "OFF (no ANTHROPIC_API_KEY)")
    log.info("Local OCR: %s", "on" if ocr.available() else "OFF (Tesseract binary not found)")
    if not TABLE.verified:
        log.warning(
            "data/units.json is not marked verified. Check tier_power and the unit tiers, "
            "then set verified_by_human to true."
        )
    if SHEETS:
        try:
            title = await asyncio.to_thread(SHEETS.check_access)
            roster = await asyncio.to_thread(SHEETS.roster, True)
            log.info("Sheet %r reachable, %d roster entries.", title, len(roster))
        except Exception as exc:
            log.error("Cannot reach the spreadsheet: %s", exc)
    else:
        log.warning("Sheets disabled - set SPREADSHEET_ID and the credentials file in .env.")


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # A plain mention reports where the drop stands. Checked before the
    # channel-specific routing below so it works from anywhere the bot can
    # see it, not just the submission channel - but the reply itself always
    # goes to the log channel, never wherever the ping came from. The
    # submission channel is meant to hold nothing but reactions and
    # screenshots (see process_submission), and a status ping isn't a
    # submission result either, so it does not belong inline in the log
    # channel's own test-submission flow if that is where the ping happened
    # to land - it is routed the same way regardless of origin.
    # message.mentions excludes @everyone/@here, so only a real tag counts.
    if client.user in message.mentions:
        embed = await campaign_embed()
        if CFG.verification_log_channel_id:
            channel = client.get_channel(CFG.verification_log_channel_id)
            if channel is not None:
                try:
                    await channel.send(
                        content=f"Status requested by **{message.author.display_name}** - {message.jump_url}",
                        embed=embed,
                    )
                except Exception:
                    log.exception("Could not post status to the verification log channel")
            else:
                log.warning(
                    "Verification log channel %s not found - not in this guild, or not cached yet.",
                    CFG.verification_log_channel_id,
                )
        else:
            # No log channel configured at all - falling back to an inline
            # reply beats a ping that silently does nothing.
            await message.reply(embed=embed, mention_author=False)
        return

    if CFG.verification_log_channel_id and message.channel.id == CFG.verification_log_channel_id:
        await handle_log_test_message(message)
        return

    if CFG.siege_check_channel_id and message.channel.id == CFG.siege_check_channel_id:
        await handle_siege_message(message)
        return

    if CFG.submission_channel_id and message.channel.id != CFG.submission_channel_id:
        return

    images = [a for a in message.attachments if is_image(a)]
    if not images:
        return

    if len(images) > 1:
        await message.reply(
            f"{FAIL} One screenshot per message, please - post each ID separately so I can "
            "tell them apart.",
            mention_author=False,
        )
        return

    governor_id = extract_governor_id(message.content)
    if not governor_id:
        await message.reply(
            f"{FAIL} I need the governor ID in the message text with the screenshot. "
            "Example: `1234567` with the image attached.",
            mention_author=False,
        )
        return

    # Download and save immediately, even though OCR itself is queued: Discord
    # attachment URLs expire in about a day, and a screenshot that only ever
    # lived as a Discord attachment is effectively gone once that happens.
    try:
        image_bytes = await images[0].read()
    except Exception as exc:
        await message.reply(f"{FAIL} Could not download that attachment: {exc}", mention_author=False)
        return

    saved = save_submission(image_bytes, governor_id, images[0].filename)
    if saved:
        log.info("Saved %s", saved)

    await message.add_reaction(QUEUED)
    await QUEUE.put(
        SubmissionJob(
            message=message,
            image_bytes=image_bytes,
            governor_id=governor_id,
            image_url=images[0].url,
            image_filename=images[0].filename,
        )
    )


async def handle_siege_message(message: discord.Message) -> None:
    """The siege-check channel's on_message: same shape as the hospital flow
    (one image, an ID in the text, instant ack, queued for the slow part) but
    reading the Troop Details screen and its own rules instead.
    """
    images = [a for a in message.attachments if is_image(a)]
    if not images:
        return

    if len(images) > 1:
        await message.reply(
            f"{FAIL} One screenshot per message, please - post each ID separately.",
            mention_author=False,
        )
        return

    governor_id = extract_governor_id(message.content)
    if not governor_id:
        await message.reply(
            f"{FAIL} I need the governor ID in the message text with the screenshot. "
            "Example: `1234567` with the image attached.",
            mention_author=False,
        )
        return

    try:
        image_bytes = await images[0].read()
    except Exception as exc:
        await message.reply(f"{FAIL} Could not download that attachment: {exc}", mention_author=False)
        return

    saved = save_submission(image_bytes, governor_id, images[0].filename)
    if saved:
        log.info("Saved %s", saved)

    await message.add_reaction(QUEUED)
    await SIEGE_QUEUE.put(
        SiegeJob(
            message=message,
            image_bytes=image_bytes,
            governor_id=governor_id,
            image_url=images[0].url,
        )
    )


async def handle_log_test_message(message: discord.Message) -> None:
    """Let an admin test either screenshot type directly in the log channel,
    where regular players never look, instead of posting in a real
    submission channel just to check a reading. Tries hospital first, then
    siege, whichever the screenshot actually is - the caller doesn't have to
    say which. Nothing is written to the sheet and no roster check applies:
    this is for "can it read this at all", not a real submission.
    """
    images = [a for a in message.attachments if is_image(a)]
    if not images or len(images) > 1:
        return

    governor_id = extract_governor_id(message.content) or "test"
    try:
        image_bytes = await images[0].read()
    except Exception as exc:
        await message.reply(f"{FAIL} Could not download that attachment: {exc}", mention_author=False)
        return

    await message.add_reaction(QUEUED)
    await TEST_QUEUE.put(TestJob(message=message, image_bytes=image_bytes, governor_id=governor_id))


async def test_worker(worker_id: int) -> None:
    log.info("Test worker %d started.", worker_id)
    while True:
        job = await TEST_QUEUE.get()
        try:
            await _run_log_test(job.message, job.image_bytes, job.governor_id)
        except Exception:
            log.exception("Test worker %d: unhandled error outside _run_log_test", worker_id)
        finally:
            TEST_QUEUE.task_done()


async def _run_log_test(message: discord.Message, image_bytes: bytes, governor_id: str) -> None:
    try:
        async with message.channel.typing():
            result = await asyncio.to_thread(
                pipeline.extract,
                image_bytes,
                TABLE,
                anthropic_api_key=CFG.anthropic_api_key,
                vision_model=CFG.vision_model,
                tesseract_cmd=CFG.tesseract_cmd,
            )

            try:
                await message.remove_reaction(QUEUED, client.user)
            except Exception:
                pass

            if result.reading.wounded_current is not None:
                submission = Submission(
                    governor_id=governor_id,
                    discord_user_id=message.author.id,
                    discord_user_name=message.author.display_name,
                )
                merge_notes = submission.merge(result, "", message.jump_url)
                embed = build_embed(submission, "", merge_notes)
                embed.title = f"[TEST - not recorded] {embed.title}"
                await message.reply(embed=embed, mention_author=False)
                return

            siege_reading = await asyncio.to_thread(read_siege, image_bytes)
            if "No troop icons found in the screenshot." not in siege_reading.warnings:
                verdict, notes = check_siege_rules(siege_reading)
                embed = build_siege_embed(governor_id, "", siege_reading, verdict, notes)
                embed.title = f"[TEST - not recorded] {embed.title}"
                await message.reply(embed=embed, mention_author=False)
                return

            await message.reply(
                f"{FAIL} Could not read this as either a hospital or a siege screenshot.",
                mention_author=False,
            )
    except Exception as exc:
        log.exception("Log-channel test failed")
        try:
            await message.remove_reaction(QUEUED, client.user)
        except Exception:
            pass
        await message.reply(f"{FAIL} Test failed: {exc}", mention_author=False)


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #

hospital = app_commands.Group(name="hospital", description="Hospital drop tracking")


def is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if isinstance(user, discord.Member):
        if user.guild_permissions.manage_guild:
            return True
        if CFG.admin_role_id and any(r.id == CFG.admin_role_id for r in user.roles):
            return True
    return False


@hospital.command(name="status", description="Show the open submission for a governor ID")
@app_commands.describe(governor_id="The governor ID to look up")
async def status(interaction: discord.Interaction, governor_id: str) -> None:
    submission = SESSIONS.get(governor_id.strip())
    if submission is None:
        await interaction.response.send_message(
            f"No open submission for `{governor_id}`. Either it was completed and written "
            "to the sheet, or it timed out.",
            ephemeral=True,
        )
        return
    name = SHEETS.name_for(submission.governor_id) if SHEETS else ""
    await interaction.response.send_message(embed=build_embed(submission, name, []), ephemeral=True)


@hospital.command(name="learn", description="Teach the bot a unit name (admin)")
@app_commands.describe(
    unit="Unit name exactly as it appears in game",
    tier="T1 - T5",
    troop_type="Infantry, Archer, Cavalry or Siege",
    power="Optional power per troop, overriding the tier default",
)
@app_commands.choices(
    tier=[app_commands.Choice(name=t, value=t) for t in TIERS],
    troop_type=[app_commands.Choice(name=t, value=t) for t in TYPES],
)
async def learn(
    interaction: discord.Interaction,
    unit: str,
    tier: app_commands.Choice[str],
    troop_type: app_commands.Choice[str],
    power: int | None = None,
) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    try:
        key = await asyncio.to_thread(TABLE.learn, unit, tier.value, troop_type.value, power)
    except ValueError as exc:
        await interaction.response.send_message(f"{FAIL} {exc}", ephemeral=True)
        return
    each = power if power is not None else TABLE.tier_power.get(tier.value)
    await interaction.response.send_message(
        f"{OK} Learned **{key}** as {tier.value} {troop_type.value} at {each} power each. "
        "Ask the player to resubmit."
    )


@hospital.command(name="reset", description="Discard the open submission for an ID (admin)")
async def reset(interaction: discord.Interaction, governor_id: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    cleared = SESSIONS.clear(governor_id.strip())
    await interaction.response.send_message(
        f"{OK} Cleared `{governor_id}`." if cleared else f"Nothing open for `{governor_id}`.",
        ephemeral=True,
    )


@hospital.command(name="refresh", description="Re-read the roster tab from the sheet (admin)")
async def refresh(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not SHEETS:
        await interaction.response.send_message("Sheets are not configured.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        roster = await asyncio.to_thread(SHEETS.roster, True)
        await interaction.followup.send(f"{OK} Roster reloaded: {len(roster)} governors.")
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} {exc}")


@hospital.command(name="totals", description="Submissions recorded and power dropped so far")
async def totals(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    await interaction.followup.send(embed=await campaign_embed())


@hospital.command(name="remove", description="Delete a governor's entry from the sheet (admin)")
@app_commands.describe(governor_id="The governor ID to delete")
async def remove(interaction: discord.Interaction, governor_id: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not SHEETS:
        await interaction.response.send_message("Sheets are not configured.", ephemeral=True)
        return

    await interaction.response.defer()
    governor_id = governor_id.strip()
    try:
        removed = await asyncio.to_thread(SHEETS.remove, governor_id)
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not remove `{governor_id}`: {exc}")
        return

    SESSIONS.clear(governor_id)
    if removed is None:
        await interaction.followup.send(f"No row on the sheet for `{governor_id}`.")
        return

    # Echo what was deleted so a mistaken removal can be typed back in.
    summary = " - ".join(
        f"{k} {removed.get(k)}"
        for k in ("Name", "Total In Hospital", "T4+T5 Troops", "Fill Check")
        if removed.get(k)
    )
    await interaction.followup.send(
        f"{OK} Removed `{governor_id}` from the sheet.\n"
        f"Was: {summary or '(empty row)'}"
    )


@hospital.command(name="edit", description="Change one field on a governor's row (admin)")
@app_commands.describe(
    governor_id="The governor ID to edit",
    field="Which column to change",
    value="The new value",
)
@app_commands.choices(
    field=[app_commands.Choice(name=f, value=f) for f in SheetsClient.EDITABLE]
)
async def edit(
    interaction: discord.Interaction,
    governor_id: str,
    field: app_commands.Choice[str],
    value: str,
) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not SHEETS:
        await interaction.response.send_message("Sheets are not configured.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        note = await asyncio.to_thread(
            SHEETS.edit,
            governor_id.strip(),
            field.value,
            value,
            interaction.user.display_name,
        )
    except (ValueError, LookupError) as exc:
        await interaction.followup.send(f"{FAIL} {exc}")
        return
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not edit `{governor_id}`: {exc}")
        return

    await interaction.followup.send(f"{OK} `{governor_id}` - {note}")


@hospital.command(
    name="reset-all",
    description="Delete EVERY recorded submission from the sheet (admin)",
)
async def reset_all(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not SHEETS:
        await interaction.response.send_message("Sheets are not configured.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        count = await asyncio.to_thread(SHEETS.submission_count)
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not read the sheet: {exc}")
        return

    if not count:
        await interaction.followup.send("There are no submissions to clear.")
        return

    try:
        removed = await asyncio.to_thread(SHEETS.clear_all)
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not clear the sheet: {exc}")
        return

    for governor_id in SESSIONS.open_ids():
        SESSIONS.clear(governor_id)
    log.warning("%s cleared all %d submissions.", interaction.user, removed)
    await interaction.followup.send(
        f"{OK} Cleared **{removed:,} submissions** and all troop rows. "
        "The Roster tab is untouched."
    )


@hospital.command(
    name="siege-reset-all",
    description="Delete EVERY recorded siege check from the sheet (admin)",
)
async def siege_reset_all(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if not SHEETS:
        await interaction.response.send_message("Sheets are not configured.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        count = await asyncio.to_thread(SHEETS.siege_submission_count)
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not read the sheet: {exc}")
        return

    if not count:
        await interaction.followup.send("There are no siege checks to clear.")
        return

    try:
        removed = await asyncio.to_thread(SHEETS.clear_siege_all)
    except Exception as exc:
        await interaction.followup.send(f"{FAIL} Could not clear the sheet: {exc}")
        return

    log.warning("%s cleared all %d siege checks.", interaction.user, removed)
    await interaction.followup.send(
        f"{OK} Cleared **{removed:,} siege checks**. "
        "Hospital submissions and the Roster tab are untouched."
    )


@hospital.command(name="health", description="Show which readers and integrations are live")
async def health(interaction: discord.Interaction) -> None:
    lines = [
        f"Local OCR (Tesseract): {'on' if ocr.available() else 'OFF'}",
        f"Vision fallback: {'on (' + CFG.vision_model + ')' if CFG.vision_enabled else 'OFF'}",
        f"Google Sheets: {'on' if SHEETS else 'OFF'}",
        f"Unit table: {len(TABLE.units)} units, "
        f"{'verified' if TABLE.verified else 'NOT VERIFIED - check data/units.json'}",
        f"Open submissions: {len(SESSIONS.open_ids())}",
        f"Queued for OCR: {QUEUE.qsize()} ({CFG.submission_workers} worker(s))",
        f"Queued for siege check: {SIEGE_QUEUE.qsize()}",
        f"Queued test submissions: {TEST_QUEUE.qsize()}",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


tree.add_command(hospital)


def main() -> None:
    if not CFG.discord_token:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not Path(CFG.units_file).exists():
        raise SystemExit(f"Unit table not found at {CFG.units_file}")
    client.run(CFG.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
