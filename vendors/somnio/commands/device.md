---
name: device
description: Drive an iOS simulator or Android emulator for mobile UAT — boot, install, navigate, capture screenshots, and record device evidence.
argument-hint: "[ios|android] [--scenario <name>]"
---
Use the `agent-device` agent for this. Mobile UAT is the last part of the delivery loop still done by hand,
so the point is to produce reproducible evidence, not just to poke the app.

Delegate to `agent-device` with the platform and scenario. Requirements it must honour, all learned the
hard way and documented in its own reference:

- Android: boot with `-no-snapshot -no-boot-anim -dns-server 8.8.8.8,1.1.1.1` — a fresh boot otherwise has
  broken DNS and clock. Re-send the geo fix **after every app launch**, not once.
- The APK must be built with `--dart-define-from-file`, or the app shows "No internet connection" while the
  emulator is perfectly online.
- Navigation is via FCM push (`FLUTTER_NOTIFICATION_CLICK`), not URL-scheme deep links.
- Finish by writing `<git-dir>/device-evidence.json` plus timestamped screenshots.
