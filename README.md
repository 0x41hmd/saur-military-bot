# Military Bot

## Note

If you are a avg SAUR player, you probably retarded so don't read that shit and don't message my discord 'how to setup this' or I'll block you

Okay so, this is a basic Discord bot for a SAUR squad. does member stuff, points, rewards, tickets, reports, events, promotions, and some casino/trivia vibes.

## Install

### requirements

- Python 3.10+
- Discord bot token
- TiDB or MySQL database
- access to the server hosting it

### setup

```bash
git clone https://github.com/0x41hmd/saur-military-bot/
cd military-bot-main
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

### install deps

```bash
pip install -r requirements.txt
```

### .env

make a `.env` in the root and drop this in:

```env
DISCORD_TOKEN=your_discord_bot_token
TIDB_HOST=your_database_host
TIDB_PORT=4000
TIDB_USER=your_database_user
TIDB_PASSWORD=your_database_password
TIDB_DATABASE=your_database_name
TIDB_SSL_CA=ce.pem
```

bot reads this when it starts. if token/db stuff missing, it aint gonna boot right.

if TiDB needs a cert, put the cert in a file like `ce.pem` and point `TIDB_SSL_CA` at it.

```env
TIDB_SSL_CA=ce.pem
```

### run it

```bash
python main.py
```

if it starts clean, it loads the cogs, sets up the DB, syncs slash commands, and runs the background checks.

## bot settings

main customization stuff is in [utils/settings.py](utils/settings.py).

- `DEVELOPER_ID` is the bot owner/developer ID. change this if you want the bypass for dev-only actions and admin checks.
- `EMBED_COLOR_HEX` is the main embed color.
- `EMBED_THUMBNAIL` is the thumbnail/icon image.
- `EMBED_FOOTER_TEXT` is the footer text.

example:

```python
DEVELOPER_ID: int = 927022013560811592
EMBED_THUMBNAIL: str = "https://i.imgur.com/G7epaBX.png"
EMBED_COLOR_HEX: str = "#009900"
EMBED_FOOTER_TEXT: str = "Military Intelligence"
```

change those to match your server vibe and owner account.

## how it works

main file is `main.py`.

- creates the bot instance
- loads env vars from `.env`
- enables Discord intents
- starts the DB layer
- loads all the cogs from `cogs/`
- syncs slash commands with Discord
- runs background checks like DB heartbeat

project layout:

- `main.py` handles startup and lifecycle
- `cogs/` holds command groups and features
- `database/` handles DB access and setup
- `utils/` has helper tools, menus, settings, and support stuff
- `data/` has static data like trivia questions

bot also checks if a user is registered before letting them use restricted stuff like balance, rewards, shop, and casino commands.

## features

### member stuff

- member registration and profile setup
- daily reward and balance tracking
- points and rankings
- promotion flow
- activity and analytics tracking

### community tools

- ticket system
- report workflow
- admin controls
- backup and restore tools
- logging and event tracking

### public server stuff

- player profiles
- leaderboards
- online member list
- recruitment tools
- event management
- help commands
- shop system

### fun stuff

- trivia game
- casino flow
- rewards and item systems
- leaderboard progress tracking
