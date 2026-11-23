# Work Timer

Home Assistant add-on. It keeps a work timer for each configured user and writes each completed interval to a local calendar.

The add-on does not listen, and does not create a calendar, until `calendar_name` is set in the add-on options.

## Options

- `calendar_name`: name of the local calendar to create or reuse.
- `token`: shared secret for the timer API.
- `port`: HTTP port. Default `8097`.
- `users`: one timer per name. A panel uses the matching name.

## API

`GET /v1/timer?user=panel` and `POST /v1/timer` with a bearer token.

The POST body can include `command` (`start`, `pause`, `reset`, or `clear`) and a `state` object. `reset` stops and zeroes the timer. A second `reset`, while the timer is already stopped at zero, clears timestamps. `clear` removes timestamps and does not stop a running timer.

A restart of the add-on or the panel is not a pause. On reconnect, an open segment keeps the earlier start time.
