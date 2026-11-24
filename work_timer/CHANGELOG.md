# Changelog

## 0.1.16

- Security: trust the ingress header only from the Supervisor gateway. Before, any LAN client could send it and skip the token. Compare the token in constant time and cap request bodies.
- Start after Home Assistant Core and wait for it, so the time zone is no longer lost to a UTC fallback after a boot. Retry the calendar setup.
- Users removed in the sidebar stay removed after a restart. Adding the name again brings it back.
- Add a watchdog on `/v1/health`, install `tzdata` in the image, and drop the unused `hassio_api`.

## 0.1.15

- Show the Work Timer sidebar panel to every user, including non-admins.

## 0.1.14

- Add, remove, and assign timer users, including a default user for each Home Assistant person.
- Expose each user to Home Assistant with start, pause, clear, reset, a play/pause/stopped status, and today's hours.

## 0.1.13

- Add a Clear day button with inline confirmation to the selected calendar date panel to delete that day's calendar entries (and reset today's timer if today is cleared).
- Remove the Clear add-on timestamps button from the header.

## 0.1.12

- Replace browser confirmation dialog with inline warning and error banners inside the card.

## 0.1.11

- In the Last 5 days section, show only past days that have timestamp data (plus today when any day has data), and hide the section completely when there is no data.
- Add month and year dropdown selectors to the calendar header for quick navigation.

## 0.1.10

- Replace horizontal daily bars with 5 separate day timetable panels showing date on top, green start and red stop times, and daily total hours and minutes underneath.
- Add an interactive monthly calendar underneath where clicking any day displays its timetable.

## 0.1.9

- A pause from the panel stays paused. An older running update no longer starts the timer again.

## 0.1.8

- A timer started in the add-on stays running when the panel syncs, and the panel follows it.

## 0.1.7

- Publish port 8097 so the panel can start, pause, and reset the add-on timer.

## 0.1.6

- The sidebar can start, pause, and reset the timer, clear the panel screen, and clear the add-on lines.
- The last 7 days show each day's total and a week total.

## 0.1.5

- The sidebar loads the timer when Home Assistant requires a response from the calendar event lookup.

## 0.1.4

- A clear asks the panel to drop its on-screen lines and leaves the add-on lines and the calendar in place.
- Reset sets today's counter back to 00:00.
- The sidebar shows a work total for each of the last 7 days and follows light and dark mode.

## 0.1.3

- Leave the calendar and the add-on lines in place when a timer is cleared or reset.
- Add a sidebar page with the live timer, today's lines, the last 7 calendar days, and a separate button that clears only the add-on lines.

## 0.1.2

- Create the local calendar with the `calendar_name` field Home Assistant requires.

## 0.1.1

- Start the timer with the add-on container environment so the Home Assistant API token is visible.

## 0.1.0

- Keep a work timer for each configured user and restore it after a restart.
- Refuse to listen until a calendar name is set.
- Write each completed interval to a Home Assistant local calendar, and create that calendar when it does not exist.
