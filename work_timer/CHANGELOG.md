# Changelog

## 0.1.2

- Create the local calendar with the `calendar_name` field Home Assistant requires.

## 0.1.1

- Start the timer with the add-on container environment so the Home Assistant API token is visible.

## 0.1.0

- Keep a work timer for each configured user and restore it after a restart.
- Refuse to listen until a calendar name is set.
- Write each completed interval to a Home Assistant local calendar, and create that calendar when it does not exist.
