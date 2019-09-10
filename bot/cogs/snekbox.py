import datetime
import logging
import random
import re
import textwrap
from signal import Signals
from typing import Optional, Tuple

from discord import Colour, Embed
from discord.ext.commands import (
    Bot, CommandError, Context, NoPrivateMessage, command, guild_only
)

from bot.constants import Channels, ERROR_REPLIES, NEGATIVE_REPLIES, STAFF_ROLES, URLs
from bot.decorators import InChannelCheckFailure, in_channel
from bot.utils.messages import wait_for_deletion


log = logging.getLogger(__name__)

ESCAPE_REGEX = re.compile("[`\u202E\u200B]{3,}")
FORMATTED_CODE_REGEX = re.compile(
    r"^\s*"                                 # any leading whitespace from the beginning of the string
    r"(?P<delim>(?P<block>```)|``?)"        # code delimiter: 1-3 backticks; (?P=block) only matches if it's a block
    r"(?(block)(?:(?P<lang>[a-z]+)\n)?)"    # if we're in a block, match optional language (only letters plus newline)
    r"(?:[ \t]*\n)*"                        # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                        # extract all code inside the markup
    r"\s*"                                  # any more whitespace before the end of the code markup
    r"(?P=delim)"                           # match the exact same delimiter from the start again
    r"\s*$",                                # any trailing whitespace until the end of the string
    re.DOTALL | re.IGNORECASE               # "." also matches newlines, case insensitive
)
RAW_CODE_REGEX = re.compile(
    r"^(?:[ \t]*\n)*"                       # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                        # extract all the rest as code
    r"\s*$",                                # any trailing whitespace until the end of the string
    re.DOTALL                               # "." also matches newlines
)

MAX_PASTE_LEN = 1000


class Snekbox:
    """Safe evaluation of Python code using Snekbox."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.jobs = {}

    async def post_eval(self, code: str) -> dict:
        """Send a POST request to the Snekbox API to evaluate code and return the results."""
        url = URLs.snekbox_eval_api
        data = {"input": code}
        async with self.bot.http_session.post(url, json=data, raise_for_status=True) as resp:
            return await resp.json()

    async def upload_output(self, output: str) -> Optional[str]:
        """Upload the eval output to a paste service and return a URL to it if successful."""
        log.trace("Uploading full output to paste service...")

        if len(output) > MAX_PASTE_LEN:
            log.info("Full output is too long to upload")
            return "too long to upload"

        url = URLs.paste_service.format(key="documents")
        try:
            async with self.bot.http_session.post(url, data=output, raise_for_status=True) as resp:
                data = await resp.json()

            if "key" in data:
                return URLs.paste_service.format(key=data["key"])
        except Exception:
            # 400 (Bad Request) means there are too many characters
            log.exception("Failed to upload full output to paste service!")

    @staticmethod
    def prepare_input(code: str) -> str:
        """Extract code from the Markdown, format it, and insert it into the code template."""
        match = FORMATTED_CODE_REGEX.fullmatch(code)
        if match:
            code, block, lang, delim = match.group("code", "block", "lang", "delim")
            code = textwrap.dedent(code)
            if block:
                info = (f"'{lang}' highlighted" if lang else "plain") + " code block"
            else:
                info = f"{delim}-enclosed inline code"
            log.trace(f"Extracted {info} for evaluation:\n{code}")
        else:
            code = textwrap.dedent(RAW_CODE_REGEX.fullmatch(code).group("code"))
            log.trace(
                f"Eval message contains unformatted or badly formatted code, "
                f"stripping whitespace only:\n{code}"
            )

        return code

    @staticmethod
    def get_results_message(results: dict) -> Tuple[str, str]:
        """Return a user-friendly message and error corresponding to the process's return code."""
        stdout, returncode = results["stdout"], results["returncode"]
        msg = f"Your eval job has completed with return code {returncode}"
        error = ""

        if returncode is None:
            msg = "Your eval job has failed"
            error = stdout.strip()
        elif returncode == 128 + Signals.SIGKILL:
            msg = "Your eval job timed out or ran out of memory"
        elif returncode == 255:
            msg = "Your eval job has failed"
            error = "A fatal NsJail error occurred"
        else:
            # Try to append signal's name if one exists
            try:
                name = Signals(returncode - 128).name
                msg = f"{msg} ({name})"
            except ValueError:
                pass

        return msg, error

    async def format_output(self, output: str) -> Tuple[str, Optional[str]]:
        """
        Format the output and return a tuple of the formatted output and a URL to the full output.

        Prepend each line with a line number. Truncate if there are over 10 lines or 1000 characters
        and upload the full output to a paste service.
        """
        log.trace("Formatting output...")

        output = output.strip(" \n")
        original_output = output  # To be uploaded to a pasting service if needed
        paste_link = None

        if "<@" in output:
            output = output.replace("<@", "<@\u200B")  # Zero-width space

        if "<!@" in output:
            output = output.replace("<!@", "<!@\u200B")  # Zero-width space

        if ESCAPE_REGEX.findall(output):
            return "Code block escape attempt detected; will not output result", paste_link

        truncated = False
        lines = output.count("\n")

        if lines > 0:
            output = output.split("\n")[:10]  # Only first 10 cause the rest is truncated anyway
            output = (f"{i:03d} | {line}" for i, line in enumerate(output, 1))
            output = "\n".join(output)

        if lines > 10:
            truncated = True
            if len(output) >= 1000:
                output = f"{output[:1000]}\n... (truncated - too long, too many lines)"
            else:
                output = f"{output}\n... (truncated - too many lines)"
        elif len(output) >= 1000:
            truncated = True
            output = f"{output[:1000]}\n... (truncated - too long)"

        if truncated:
            paste_link = await self.upload_output(original_output)

        output = output.strip()
        if not output:
            output = "[No output]"

        return output, paste_link

    @command(name="eval", aliases=("e",))
    @guild_only()
    @in_channel(Channels.bot, bypass_roles=STAFF_ROLES)
    async def eval_command(self, ctx: Context, *, code: str = None) -> None:
        """
        Run Python code and get the results.

        This command supports multiple lines of code, including code wrapped inside a formatted code
        block. We've done our best to make this safe, but do let us know if you manage to find an
        issue with it!
        """
        if ctx.author.id in self.jobs:
            return await ctx.send(
                f"{ctx.author.mention} You've already got a job running - "
                f"please wait for it to finish!"
            )

        if not code:  # None or empty string
            return await ctx.invoke(self.bot.get_command("help"), "eval")

        log.info(
            f"Received code from {ctx.author.name}#{ctx.author.discriminator} "
            f"for evaluation:\n{code}"
        )

        self.jobs[ctx.author.id] = datetime.datetime.now()
        code = self.prepare_input(code)

        try:
            async with ctx.typing():
                results = await self.post_eval(code)
                msg, error = self.get_results_message(results)

                if error:
                    output, paste_link = error, None
                else:
                    output, paste_link = await self.format_output(results["stdout"])

                msg = f"{ctx.author.mention} {msg}.\n\n```py\n{output}\n```"
                if paste_link:
                    msg = f"{msg}\nFull output: {paste_link}"

                response = await ctx.send(msg)
                self.bot.loop.create_task(
                    wait_for_deletion(response, user_ids=(ctx.author.id,), client=ctx.bot)
                )

                log.info(
                    f"{ctx.author.name}#{ctx.author.discriminator}'s job had a return code of "
                    f"{results['returncode']}"
                )
        finally:
            del self.jobs[ctx.author.id]

    @eval_command.error
    async def eval_command_error(self, ctx: Context, error: CommandError) -> None:
        """Eval commands error handler."""
        embed = Embed(colour=Colour.red())

        if isinstance(error, NoPrivateMessage):
            embed.title = random.choice(NEGATIVE_REPLIES)
            embed.description = "You're not allowed to use this command in private messages."
            await ctx.send(embed=embed)

        elif isinstance(error, InChannelCheckFailure):
            embed.title = random.choice(NEGATIVE_REPLIES)
            embed.description = str(error)
            await ctx.send(embed=embed)

        else:
            original_error = getattr(error, 'original', "no original error")
            log.error(f"Unhandled error in snekbox eval: {error} ({original_error})")
            embed.title = random.choice(ERROR_REPLIES)
            embed.description = "Some unhandled error occurred. Sorry for that!"
            await ctx.send(embed=embed)


def setup(bot: Bot) -> None:
    """Snekbox cog load."""
    bot.add_cog(Snekbox(bot))
    log.info("Cog loaded: Snekbox")
