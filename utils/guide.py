from cogs.public.ticket import TICKET_TYPES, MAX_OPEN_TICKETS_PER_USER, TICKET_CREATION_COOLDOWN
from utils.settings import CASINO_CONFIG, TRIVIA_CONFIG, TRIVIA_STREAK_REWARDS

TICKET_HELP = {
    "Request a bug":                   "report a bug or problem you found",
    "Referral request":                "ask for a referral",
    "Report a teammate":               "report a member of your team",
    "Report SWAT member":              "report a SWAT player",
    "Report Terrorists member":        "report a Terrorists player",
    "Request a promotion":             "ask HQ to review you for a promotion",
    "Request permission":              "ask HQ for permission to do something",
    "Request LOA (Leave of Absence)":  "let HQ know you'll be away and request leave",
    "Contact the HQ team":             "anything else you need HQ for",
}

PROFILE = (
    "Shows a member's service record: rank, service info, rank and career stats, and points.\n"
    "`/profile`: your own record\n"
    "`/profile member:@user`: someone else's record"
)

LEADERBOARD = (
    "Shows the top 10 for a category. Pick it in the `field` option.\n"
    "\u2022 **Activity**: most promotional points\n"
    "\u2022 **Leaders**: most leadership points\n"
    "\u2022 **Airforce**: most Air Force points\n"
    "\u2022 **Warzone**: most Warzone operations\n"
    "\u2022 **Doomsday**: most Doomsday operations\n"
    "\u2022 **Casino**: the richest casino players\n"
    "\u2022 **Trivia**: the best trivia streaks"
)

ONLINE = (
    "Shows who is on the server right now, grouped by team."
)

CASINO = (
    "Play games with casino credits. Your wallet is also shown in `/profile`.\n"
    "`/balance`: your credits, daily streak and wheel spins left\n"
    "`/dailyreward`: claim free credits (a streak makes it bigger)\n"
    "`/casino play game amount`: pick the game; amounts accept `5000`, `10k`, `1.5m`, `half`, `all`\n"
    "\u2022 **Slots**: match the reels, 7-7-7 is the top prize\n"
    "\u2022 **Roll**: a d100, 51 or higher wins up to x10\n"
    f"\u2022 **Fortune Wheel**: {CASINO_CONFIG['wheel_spin_cost']:,} per spin, {CASINO_CONFIG['wheel_spins_per_day']} spins a day (no amount needed)\n"
    "\u2022 **Poker**: hold cards then draw\n"
    "\u2022 **Blackjack**: buttons for Hit, Stand, Double, Split and Surrender\n"
)

TRIVIA = (
    "Answer questions with the buttons and win credits. It costs nothing to play, a wrong answer or running out of time only resets your streak.\n"
    "`/trivia play`: get a question (see `/leaderboard` for the best streaks)\n"
    f"You get {TRIVIA_CONFIG['time_limit_seconds']} seconds per question. The longer your streak, the more each answer pays "
    "(the reward goes up at streaks of " + ", ".join(str(s) for s, _ in TRIVIA_STREAK_REWARDS if s > 1) + "). "
    "Roles with a casino winnings bonus add their % to every trivia reward too."
)

SHOP = (
    "Spend your casino credits on weapons, armours, roles and more.\n"
    "`/shop list [category]`: see what is for sale\n"
    "`/shop buy item quantity`: buy it (start typing the item name; quantity is per bullet, per one...)\n"
    "`/shop orders`: your recent orders and their status\n"
    "Roles are given to you the moment you buy them and removed when their time ends. "
    "Everything else (weapons, armours...) is confirmed by HQ: your credits are taken when you buy, and if HQ rejects the order you get them back. "
    "Some roles also give a bigger daily reward or a bonus on casino and trivia winnings while you have them (shown in the shop)."
)

REPORT_CREATE = (
    "In the reporting channel, choose the operation in the dropdown, press **Create Report**, "
    "follow the steps, then press **Submit Report**.\n"
    "Air Force operations can only be submitted by Air Force members."
)

REPORT_EDIT = (
    "Choose the same operation in the dropdown, press **Edit Report** and enter the report ID "
    "(the number after `#` in the report's title). Then change what you need and submit again.\n"
    "You can only edit your own reports, and must wait 5 minutes between edits."
)

REPORT_DELETE = (
    "Choose the operation in the dropdown, press **Delete Report** and enter the report ID. "
    "You can only delete your own reports.\n"
    "Extra Points requests can't be edited or deleted: submit a new one, or ask HQ to decline it."
)


TICKET_FLOW = (
    "Pick a type from the menu in the tickets channel and a private channel (`tickets-0001` style) opens for you and HQ. "
    "When it closes, a transcript is saved and the channel is deleted."
)

TICKETS_BRIEF = "Need HQ? " + TICKET_FLOW


def tickets() -> str:
    lines = []
    for ticket_type in TICKET_TYPES:
        note = TICKET_HELP.get(ticket_type)
        lines.append(f"\u2022 **{ticket_type}**" + (f": {note}" if note else ""))
    return (
        TICKET_FLOW + "\n"
        + "\n".join(lines)
        + f"\nYou can have up to {MAX_OPEN_TICKETS_PER_USER} open tickets, "
          f"and must wait {TICKET_CREATION_COOLDOWN} seconds between new ones."
    )
