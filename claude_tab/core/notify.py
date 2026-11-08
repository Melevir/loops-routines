"""macOS notifications, without an app of our own.

    banner   Notification Center (`display notification`): info, success, warning, error; a sound per kind.
             No buttons: macOS gives those only to a signed app with its own notification permission.
    dialog   a system dialog (`display dialog`) with 1-3 buttons of the agent's wording and a time limit:
             the way an agent asks "yes or no?" and waits. The button it recommends is the default one
             (Return), on the right.

Both go through /usr/bin/osascript with every text as an argument, never inside the script. The first
banner may make macOS ask whether "Script Editor" may notify (System Settings > Notifications).
"""

import asyncio
from dataclasses import dataclass

KINDS = ("info", "success", "warning", "error")
MAX_BUTTONS = 3
MAX_BUTTON = 30
MAX_QUESTION = 600
OSASCRIPT = "/usr/bin/osascript"
MARK = {"info": "", "success": "✅ ", "warning": "⚠️ ", "error": "❌ "}

BANNER_SCRIPT = """
on run argv
  set theTitle to item 1 of argv
  set theText to item 2 of argv
  set theSubtitle to item 3 of argv
  set theSound to item 4 of argv
  if theSound is "" then
    display notification theText with title theTitle subtitle theSubtitle
  else
    display notification theText with title theTitle subtitle theSubtitle sound name theSound
  end if
end run
"""

DIALOG_SCRIPT = """
on run argv
  set theTitle to item 1 of argv
  set theQuestion to item 2 of argv
  set theSeconds to (item 3 of argv) as integer
  set theIcon to item 4 of argv
  set theButtons to items 5 thru -1 of argv
  try
    if theIcon is "caution" then
      set r to display dialog theQuestion with title theTitle buttons theButtons default button (last item of theButtons) giving up after theSeconds with icon caution
    else
      set r to display dialog theQuestion with title theTitle buttons theButtons default button (last item of theButtons) giving up after theSeconds with icon note
    end if
  on error number -128
    return "dismissed|"
  end try
  if gave up of r then return "timed_out|"
  return "answered|" & (button returned of r)
end run
"""


@dataclass(frozen=True)
class Answer:
    status: str  # answered | timed_out | dismissed | cancelled | failed
    answer: str | None = None
    error: str | None = None


def check_buttons(buttons: list[str] | None) -> list[str]:
    buttons = [" ".join(str(b).split()) for b in (["Yes", "No"] if buttons is None else buttons)]
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        raise ValueError(f"give 1 to {MAX_BUTTONS} buttons")
    if any(not b or len(b) > MAX_BUTTON for b in buttons):
        raise ValueError(f"each button is 1..{MAX_BUTTON} characters")
    if len(set(b.lower() for b in buttons)) != len(buttons):
        raise ValueError("buttons must differ")
    return buttons


async def _osascript(script: str, *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        OSASCRIPT, "-", *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)


class MacNotifier:
    async def banner(self, title: str, text: str, kind: str = "info", sound: str = "") -> None:
        proc = await _osascript(BANNER_SCRIPT, (MARK.get(kind, "") + title)[:120], text[:400], "", sound)
        try:
            _, err = await asyncio.wait_for(proc.communicate(BANNER_SCRIPT.encode()), 15)
        except TimeoutError:
            proc.kill()
            raise RuntimeError("osascript did not answer in 15 s") from None
        if proc.returncode:
            raise RuntimeError(f"osascript: {err.decode(errors='replace').strip()[:300]}")

    async def ask(self, title: str, question: str, buttons: list[str], timeout_seconds: int,
                  caution: bool = False) -> Answer:
        """Show the dialog and wait. Cancelling the task closes the dialog."""
        # display dialog lists buttons left to right and the default is the last: the recommended one,
        # given first, goes on the right
        proc = await _osascript(DIALOG_SCRIPT, title[:120], question[:MAX_QUESTION], str(int(timeout_seconds)),
                                "caution" if caution else "note", *reversed(buttons))
        try:
            out, err = await proc.communicate(DIALOG_SCRIPT.encode())
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            raise
        if proc.returncode:
            return Answer("failed", error=err.decode(errors="replace").strip()[:300])
        status, _, answer = out.decode(errors="replace").strip().partition("|")
        return Answer(status, answer or None)


def instructions(notify: str) -> str:
    """What a run's prompt says about notifying and asking the user, per its routine's `notify`."""
    ask = ("If you need a decision only the user can make, call `ask_user` with a short question and 1-3 short "
           "buttons, the one you recommend first. It waits for the click up to its timeout; without an answer, "
           "take the safe path and say so in your report. Do not ask what you can decide yourself.")
    if notify == "always":
        return ("When you finish, tell the user with `notify`: your summary in one line, kind `error` for an alert, "
                "`success` otherwise. " + ask)
    if notify == "on_alert":
        return ("Tell the user only about an alert: if your status is `alert`, call `notify` with kind `error` and "
                "your summary in one line. Send nothing when all is well. " + ask)
    return "Do not notify or ask the user: your result file is enough."
