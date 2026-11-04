# Thermostat widget

Always-on-top Windows desktop widget for Siemens Synco heating (OZW772 gateway) and a Samsung AC (SmartThings).

## Install on a PC

1. Install Python 3 from python.org.
2. Download `thermostat.pyw` and the `icons` folder into one folder.
3. Double-click `thermostat.pyw`.

Settings and logins are kept per Windows user in `%APPDATA%\ThermostatWidget` and `%APPDATA%\SamsungACRemote`, never in this folder.

## Room icons

Put a PNG or GIF in `icons` named like the room, for example `tomo_kambarys.png` for "Tomo kambarys". Case, spaces and accents don't matter.

## Releasing an update

1. Change the script.
2. Raise `__version__` near the top of `thermostat.pyw` (for example `1.0.0` to `1.0.1`).
3. Commit and push to `main`.

Every PC checks GitHub when the widget starts. If the version there is higher it replaces the script (keeping `thermostat.pyw.bak`), downloads new or changed icons and restarts.
To turn this off on a PC, add `"auto_update": false` to `%APPDATA%\ThermostatWidget\config.json`.
