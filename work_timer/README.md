# Work Timer

This add-on was built by AI. It might not be updated.

Home Assistant add-on. It keeps a work timer for each configured user and writes each completed interval to a local calendar.

## Install

1. In Home Assistant, open Settings, Add-ons, Add-on store, then the menu, Repositories.
2. Add `https://github.com/VelloMagu/work-timer`.
3. Install **Work Timer**.
4. Set `calendar_name` to the local calendar you want. The add-on does not listen, and does not create a calendar, until this is set.
5. Set a long random `token`. Leave `port` at `8097` unless that port is already in use. `users` lists one timer name per person. A panel uses the matching name, `panel` by default.
6. Start the add-on. It creates or reuses the local calendar with that name.

## Options

- `calendar_name`: name of the local calendar to create or reuse.
- `token`: shared secret for the timer API.
- `port`: HTTP port. Default `8097`.
- `users`: one timer per name. A panel uses the matching name. The sidebar can add or remove names after the add-on starts, and can set a default timer for each Home Assistant person.

The custom integration in `custom_components/work_timer` adds one Home Assistant device per timer name. Each device has Start, Pause, Clear, and Reset buttons, a status of `play`, `pause`, or `stopped`, and today's hours. Copy that folder into Home Assistant `custom_components`, restart Home Assistant, and add the Work Timer integration with the add-on address and token.

## API

`GET /v1/timer?user=panel` and `POST /v1/timer` with a bearer token.

The POST body can include `command` (`start`, `pause`, `reset`, or `clear`) and a `state` object. `reset` stops the timer and sets today's counter to `00:00`. `clear` asks the panel to drop its on-screen lines. Neither command removes stored lines or calendar events. The sidebar shows a total for each of the last 7 days and can start, pause, reset, clear the panel screen, or clear the add-on lines. Today follows the counter, so a reset shows `00:00`.

A restart of the add-on or the panel is not a pause. On reconnect, an open segment keeps the earlier start time.
