# Grubhub Skill for Claude Code

Claude Code skill for ordering food from Grubhub conversationally. Search restaurants, browse menus, build carts, and place orders -- all from the CLI.

## Architecture

**Hybrid approach: API reads + Playwright writes**

- **Reads** (search, menu, history, favorites, ratings, offers): Direct HTTP calls to `api-gtm.grubhub.com` via `grubhub-cli.py`. Fast, no browser needed.
- **Writes** (add to cart, checkout): Chrome DevTools Protocol (CDP) driving a Playwright browser. Required because Grubhub menu items have mandatory customization modals that can only be navigated through the UI.

## Files

| File | Purpose |
|------|---------|
| `grubhub-cli.py` | Python CLI (stdlib + certifi + websockets). Handles API reads and CDP browser automation for cart operations. |
| `grubhub.md` | Claude Code skill definition. Conversational ordering flow with safety rules, cost estimation, and company policy. |

## Setup

```bash
cp grubhub-cli.py ~/.claude/tools/grubhub-cli.py
cp grubhub.md ~/.claude/commands/grubhub.md
chmod +x ~/.claude/tools/grubhub-cli.py
pip3 install certifi websockets
```

Requires a Playwright MCP browser session logged into Grubhub (Google SSO).

## Usage

```
/grubhub                    # Show recent orders + favorites
/grubhub search sushi       # Search restaurants
/grubhub menu 8519672       # Browse a restaurant menu
/grubhub history            # Order history with full receipts
```
