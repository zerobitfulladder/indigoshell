---
name: phone
description: The user's Android phone, over SSH. Read their texts, call log, contacts and the notifications on it (WhatsApp and other apps' messages included); send a text or place a call; find where the phone is, its battery and Wi-Fi; take a photo or record audio with it; torch, volume, brightness, vibration, speaking aloud; its clipboard; open a link or file on it. Use whenever the user asks about their phone or wants something done on it.
---

# The phone

`ssh phone '<command>'` runs a command on the user's phone, in Termux,
as the Termux app. Termux:API's `termux-*` commands reach the phone
itself, and most print JSON. For copying files use the `phone_files`,
`phone_pull` and `phone_push` tools instead: they show progress bars.

## Reaching it

- It works while the **uplink** is on. The user turns it on with the
  uplink widget on their home screen; a pinned "UPLINK HOT" notification
  shows it is on, and its SEVER button turns it off.
- `phone: The phone didn't answer — its uplink is probably off` means
  exactly that: ask the user to tap the uplink widget. Don't retry in a
  loop.
- The first command takes about a second (finding the phone on the
  network); the connection then stays open for a minute, so the next
  ones are instant.
- Single-quote the whole remote command; it runs in bash on the phone.
  `jq` is on this computer, not the phone, so filter here:
  `ssh phone 'termux-sms-list -l 50' | jq '.[] | {number, body}'`.
- Commands that wait on the phone (location, recording, the camera) can
  hang if Termux:API doesn't answer: wrap them in `timeout`, as below.
- `ssh phone '~/.shortcuts/tasks/uplink off'` severs the link from here
  (it also ends this connection), when the user asks to close it.

## Reading

| What | Command |
|---|---|
| Latest texts, newest last | `termux-sms-list -l 20` (address, number, body, received, read, type, threadid) |
| From one sender | `termux-sms-list -l 50 -f '<address as the list shows it>'` |
| One line per conversation | `termux-sms-list -c --conversation-limit=15` |
| Received only / unread | `termux-sms-list -t inbox -l 20` / `--message-selection='read == 0'` |
| Search the text | `termux-sms-list -l 50 --message-selection="body LIKE '%word%'"` |
| Call log, newest first | `termux-call-log -l 20` (name, phone_number, type, date, duration, sim_id) |
| Contacts | `termux-contact-list` (name, number; hundreds: filter with jq `select(.name\|test("ali";"i"))`) |
| Notifications on the phone now | `termux-notification-list` (packageName, title, content, when): WhatsApp, Instagram, bank and other apps' messages |
| Where the phone is | `timeout 30 termux-location -p network -r once` (fast, street-level); `-p gps` for precise (slow, needs sky) |
| Battery | `termux-battery-status` |
| Wi-Fi it is on | `termux-wifi-connectioninfo` |
| Clipboard | `termux-clipboard-get` |
| Volume levels | `termux-volume` |

To show a location, open
`https://www.openstreetmap.org/?mlat=LAT&mlon=LON#map=17/LAT/LON`.

## Doing

| What | Command |
|---|---|
| Send a text | `termux-sms-send -n '<number>' '<text>'` |
| Call someone | `termux-telephony-call '<number>'` (dials at once, on the phone) |
| A notification on the phone | `termux-notification -t '<title>' -c '<text>'` |
| A toast | `termux-toast '<text>'` |
| Say something aloud | `termux-tts-speak '<text>'` |
| Vibrate | `termux-vibrate -d 500` |
| Torch | `termux-torch on` / `termux-torch off` |
| Volume | `termux-volume music 8` (music, ring, notification, alarm, call, system) |
| Brightness | `termux-brightness 120` (0–255) or `termux-brightness auto` |
| Set its clipboard | `termux-clipboard-set '<text>'` |
| Open a link on the phone | `termux-open-url '<url>'` |
| Open a file on the phone | `termux-open '/storage/emulated/0/Download/<file>'` |
| Take a photo | `timeout 30 termux-camera-photo -c 0 /storage/emulated/0/DCIM/indigo-<time>.jpg` (0 back, 1 front: `termux-camera-info`), then `phone_pull` it and look |
| Record audio | `termux-microphone-record -f /storage/emulated/0/Recordings/<name>.m4a -l 30`; `-q` stops early |
| Play a file on the phone | `termux-media-player play '<file>'`, then `pause` / `play` / `stop` |

When a number comes from a contact search with several matches, ask
which one before texting or calling.

## Files in place

Internal storage is `/storage/emulated/0`: DCIM/Camera, Pictures/
Screenshots, Download, Documents, Recordings, and WhatsApp's media under
`Android/media/com.whatsapp/WhatsApp/Media`. Quick looks are fine over
ssh (`ls -t /storage/emulated/0/DCIM/Camera | head`, `du -sh`, `find`);
copying is the phone tools.

## What it can't do

- The screen: no taps, swipes, typing into apps, or screenshots of the
  phone. Say so plainly.
- Other apps' private data (chat histories, app databases) is closed
  without root; their notifications are the way in.
- `{"error": "Please grant the following permission..."}`: tell the user
  which one to allow (Settings → Apps → Termux:API → Permissions). If
  `termux-notification-list` hangs, Termux:API lost notification access
  (Settings → search "notification access").

## Privacy

Texts and notifications hold login codes, bank messages and other
people's words. Quote only what the user asked about; summarise the
rest.
