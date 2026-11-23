# Changelog

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
