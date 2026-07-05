import json
import logging
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import gspread
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Project Tracker")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "America/Toronto"))
DAYS_BEFORE = int(os.getenv("DAYS_BEFORE", "2"))
SENT_REMINDERS_FILE = Path("sent_reminders.json")

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def load_sent_reminders() -> set[str]:
    if not SENT_REMINDERS_FILE.exists():
        return set()
    return set(json.loads(SENT_REMINDERS_FILE.read_text()))


def save_sent_reminders(sent: set[str]) -> None:
    SENT_REMINDERS_FILE.write_text(json.dumps(sorted(sent), indent=2))


def parse_due_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    logger.warning("Could not parse due date: %r", value)
    return None


def get_sheets_client() -> gspread.Client:
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        return gspread.service_account_from_dict(json.loads(credentials_json))
    return gspread.service_account(filename=CREDENTIALS_FILE)


def build_discord_lookup(members_sheet) -> dict[str, int]:
    lookup = {}
    for row in members_sheet.get_all_records():
        name = row.get("Name", "").strip()
        user_id = row.get("Discord User ID", "")
        if not name or not user_id:
            continue
        lookup[name] = int(str(user_id).strip())
    return lookup


def format_reminder(task: dict, due: date, days_left: int) -> str:
    work_item = task.get("Work Item", "Unknown task")
    progress = task.get("Progress", "Unknown")

    if days_left < 0:
        days_line = f"{abs(days_left)} day(s) overdue"
    elif days_left == 0:
        days_line = "Due today"
    elif days_left == 1:
        days_line = "1"
    else:
        days_line = str(days_left)

    return (
        "🔔 **Task Reminder**\n\n"
        f"**Work Item:**\n{work_item}\n\n"
        f"**Progress:**\n{progress}\n\n"
        f"**Due:**\n{due.strftime('%B %d')}\n\n"
        f"**Days Remaining:**\n{days_line}\n\n"
        "Please update progress if needed."
    )


async def send_task_reminders() -> None:
    gc = get_sheets_client()
    spreadsheet = gc.open(SPREADSHEET_NAME)
    tasks_sheet = spreadsheet.worksheet("Tasks")
    members_sheet = spreadsheet.worksheet("Members")

    discord_lookup = build_discord_lookup(members_sheet)
    task_rows = tasks_sheet.get_all_records()
    today = datetime.now(TIMEZONE).date()
    sent_reminders = load_sent_reminders()
    newly_sent: set[str] = set()

    for task in task_rows:
        work_item = task.get("Work Item", "").strip()
        assignee = task.get("Assignees", "").strip()
        due = parse_due_date(str(task.get("Due Date", "")))

        if not work_item or not assignee or due is None:
            continue

        days_left = (due - today).days
        if days_left > DAYS_BEFORE:
            continue

        user_id = discord_lookup.get(assignee)
        if user_id is None:
            logger.warning("No Discord User ID for assignee: %s", assignee)
            continue

        reminder_key = f"{work_item}|{assignee}|{due.isoformat()}|{today.isoformat()}"
        if reminder_key in sent_reminders:
            continue

        message = format_reminder(task, due, days_left)

        try:
            user = await bot.fetch_user(user_id)
            await user.send(message)
            newly_sent.add(reminder_key)
            logger.info("Sent reminder to %s for %r", assignee, work_item)
        except discord.NotFound:
            logger.error("Discord user not found for ID %s (%s)", user_id, assignee)
        except discord.Forbidden:
            logger.error("Cannot DM %s (%s) — DMs may be closed", assignee, user_id)
        except discord.HTTPException as exc:
            logger.error("Failed to DM %s: %s", assignee, exc)

    if newly_sent:
        save_sent_reminders(sent_reminders | newly_sent)


@tasks.loop(time=time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TIMEZONE))
async def reminder_check():
    logger.info("Running daily reminder check")
    await send_task_reminders()


@reminder_check.before_loop
async def before_reminder_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)
    if not reminder_check.is_running():
        reminder_check.start()


@bot.command(name="check")
@commands.has_permissions(administrator=True)
async def manual_check(ctx: commands.Context):
    """Manually run the reminder check (admin only)."""
    await ctx.send("Running reminder check...")
    await send_task_reminders()
    await ctx.send("Done.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
