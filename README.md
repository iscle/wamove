# wamove

Move WhatsApp chats and media from an Android phone to an iPhone that is **already set up**, without erasing the iPhone.

WhatsApp's own Android → iPhone transfer only works while setting up a new or erased iPhone, through the Move to iOS app. If the iPhone is already in use, the official answer is to reset it. wamove works around that by editing WhatsApp's data inside an iPhone backup on your Mac and restoring only that data.

> [!WARNING]
> This uses undocumented formats and is not supported by WhatsApp or Apple. Keep the Android phone and its backup until you are happy with the result. If something goes wrong, `wamove rollback` puts back WhatsApp's data as it was before, and a full iPhone backup stays on your Mac.
>
> The restore makes the iPhone go through its setup screens. Afterwards the home screen layout may be reset and some apps, WhatsApp included, may show faded icons until they are downloaded again. Their data stays.

## How it works

1. `pull` copies the Android phone's encrypted chat backup (`msgstore.db.crypt15`), its media folders and contact names over USB with `adb`.
2. `decrypt` decrypts the backup with its 64-digit end-to-end encryption key.
3. `backup` takes a full backup of the iPhone, then keeps a copy with only WhatsApp's data in it.
4. `build` adds the Android chats and media to WhatsApp's iPhone database (`ChatStorage.sqlite`) in that copy. It learns the conventions from the rows WhatsApp itself wrote on the iPhone, so the new rows look like WhatsApp's own.
5. `restore` restores the WhatsApp-only copy to the iPhone. Everything else on the phone is left alone.

Nothing is uploaded anywhere. Both phones talk to the Mac over USB.

## Requirements

- A Mac with the Xcode Command Line Tools (`xcode-select --install`)
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- `adb` (`brew install --cask android-platform-tools`)
- A USB cable for each phone
- Free disk space for a full iPhone backup plus the Android media. `wamove doctor` shows both.
- WhatsApp (not WhatsApp Business) on both phones, with the same phone number

## Install

```sh
git clone https://github.com/alicin/wamove.git
./wamove/install.sh
```

or, without cloning:

```sh
uv tool install --python 3.13 git+https://github.com/alicin/wamove.git
```

## With a rooted Android phone: WhatsApp's own transfer (recommended)

If the Android phone is rooted, wamove can let WhatsApp move everything itself. It runs WhatsApp's own Move to iOS exporter on the Android phone, puts the result where WhatsApp on the iPhone looks for a Move to iOS import, and restores only WhatsApp's data. After registering, WhatsApp on the iPhone imports the history with its own code: groups, reactions, polls, edits, system messages, call logs and media included.

1. Make sure the shell can get root (`adb shell su -c id`). **Never open Settings → Chats → Transfer chats on the Android phone:** it tells WhatsApp's server that a phone-to-phone transfer is coming, and the iPhone then shows a QR code instead of importing (if that already happened, see below).
2. On the iPhone, install WhatsApp but do not register it. Turn off Find My and **Personal Hotspot** (the iPhone drops its hotspot USB link when a restore begins, and the USB reconfiguration that follows cuts the restore off). Quit Xcode, Photos, Image Capture, QuickTime, Console and AltServer.

```sh
wamove export     # WhatsApp's exporter on Android; WhatsApp on Android logs out when it finishes
wamove backup     # full iPhone backup, then a WhatsApp-only copy
wamove plant      # adds the export to the WhatsApp-only copy, encrypted like Move to iOS delivers it
wamove restore    # the iPhone restarts when done
```

3. Register WhatsApp on the iPhone with the same number. It offers to import the chats from Android; keep it open until it finishes. Do not log in on the Android phone again.

If it asks you to scan a QR code on the other phone instead, cancel, then skip the "transfer chat history from another phone" screen (do not choose iPhone or Android): the import follows.

**Saved stickers.** WhatsApp's exporter carries favorite and recent stickers only when a server A/B property is on, which it usually is not, so `export` turns it on locally for the export and puts it back afterwards (`--no-stickers` skips that), then reports how many the export carries. This path has not yet been confirmed end to end. If the export still lacks them, `wamove stickers --phone <number>` adds the favorites after the import: it links a temporary device to your account with [whatsmeow](https://github.com/tulir/whatsmeow), an unofficial client (against WhatsApp's terms; use at your own risk; needs Go and a source checkout of wamove, since the tool lives in `tools/favstickers`), adds each favorite the way WhatsApp's linked devices sync them, and unlinks again. Recent stickers cannot be added that way.

## Moving your chats from a chat backup

### 1. On the Android phone

1. In WhatsApp, open Settings → Chats → Chat backup → End-to-end encrypted backup, turn it on and choose **Use 64-digit encryption key instead**. Save the key.
2. Tap **Back up**. A backup made before the key was set cannot be decrypted with it.
3. Turn on USB debugging (Settings → Developer options), plug the phone into the Mac and allow it.

```sh
wamove doctor     # checks both phones and free space
wamove pull       # the chat backup and all media
wamove decrypt    # asks for the 64-digit key
```

### 2. On the iPhone

1. Install WhatsApp and register it with the same number. This signs WhatsApp out on the Android phone, which is expected. Skip restoring from iCloud.
2. Send and receive a message and a photo, so the database has rows to learn from.
3. Open WhatsApp once more, so any message it received in the background is saved to its database. Messages that only exist as notifications are lost by the restore.
4. Turn on Airplane mode and turn off Wi-Fi, so no new messages arrive until the restore is done. They wait on WhatsApp's servers and arrive afterwards.
5. Turn off Find My (Settings → your name → Find My). Restores are refused while it is on. Turning it off may need Airplane mode off for a moment: open WhatsApp again before switching Airplane mode back on.

Plug the iPhone into the Mac, unlock it and trust the Mac.

```sh
wamove backup     # full backup, then a WhatsApp-only copy; asks for the backup password if backups are encrypted
wamove inspect    # optional: what is on each side
wamove build      # adds the Android chats to the WhatsApp-only copy
wamove restore    # the iPhone restarts when done
```

### 3. After the restore

Finish the setup screens, reinstall WhatsApp from the App Store if its icon is faded, open it, then turn Airplane mode off and Find My back on.

If the result is not right, `wamove rollback` restores the untouched WhatsApp-only copy.

When you are happy, delete `~/wamove`. It holds your decrypted chats.

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | Shows the connected Android phone and iPhone, Find My, backup encryption, free space, and which steps are done |
| `pull [--no-media]` | Copies the chat backup, contact names and media off the Android phone |
| `decrypt [--key-file FILE]` | Decrypts the chat backup and prints how many chats and messages it has |
| `backup` | Takes a full iPhone backup and makes the WhatsApp-only copy |
| `inspect` | Shows the Android chats and what is in the iPhone's WhatsApp database |
| `build [--no-media]` | Writes the Android chats (and media) into the WhatsApp-only copy and checks the result |
| `export [--keep-live-locations] [--no-stickers]` | Runs WhatsApp's own Move to iOS export on the rooted Android phone and copies it to the Mac |
| `collect` | Copies a finished export off the Android phone again, without exporting |
| `plant` | Adds the export to the WhatsApp-only copy, where WhatsApp on the iPhone imports it from |
| `stickers --phone NUMBER [--yes]` | After the import, adds Android's favorite stickers through a temporary unofficial linked device |
| `restore` | Restores the built or planted copy to the iPhone |
| `rollback` | Restores the untouched WhatsApp-only copy to the iPhone |

Options that go before the command:

| Option | |
| --- | --- |
| `--work DIR` | Where everything is kept, default `~/wamove` (or `$WAMOVE_WORK`) |
| `--udid UDID` | Which iPhone to use when several are connected |
| `--contacts FILE.vcf` | Extra contact names for chats, on top of the ones read from the Android phone |

The key and the backup password are asked for in the terminal, or read from `WAMOVE_KEY` and `WAMOVE_BACKUP_PASSWORD`.

The work folder looks like this:

```
~/wamove/
  android/          chat backup, decrypted msgstore.db, contacts.json, Media/
  ios/full/         the full iPhone backup
  ios/pristine/     WhatsApp-only copy, as it was on the iPhone (used by rollback)
  ios/patched/      WhatsApp-only copy with the Android chats added (used by restore)
```

## What moves

Text messages, photos, videos, voice notes, audio, documents, GIFs, stickers, locations and contact cards, with their original dates. Chats that already exist on the iPhone are merged, and messages that are already there are skipped.

Not moved: system messages ("X joined"), deleted-message markers, call logs, reactions, polls, status updates and channels.

## Known issues

Tested once, in September 2026, from a Pixel 10 Pro XL to an iPhone on iOS 27.0: 64 one-to-one chats with about 25,000 messages and 889 media files came across, with thumbnails, and media opened.

- The history of **group chats** was written to the database but did not show up in WhatsApp after the restore. The cause is not known yet.
- WhatsApp changes its database between versions. If `build` fails its checks, WhatsApp on the iPhone has probably changed something wamove does not know about yet. Please open an issue with the output of `wamove inspect`.

## Troubleshooting

- **`adb` sees no device:** unlock the Android phone and accept the USB debugging prompt. `adb devices` should list it as `device`, not `unauthorized`.
- **No WhatsApp backup found on Android:** wamove looks in `Android/media/com.whatsapp/WhatsApp`. Make a backup in WhatsApp first.
- **`decrypt` fails:** the backup must be made after the 64-digit key was set. Make a new backup on the Android phone, then run `pull` again.
- **The restore is refused:** turn off Find My on the iPhone and run `wamove restore` again.
- **WhatsApp is not in the iPhone backup:** install and register WhatsApp on the iPhone, then run `backup` again.

## Development

```sh
uv run --group dev pytest
```

The tests build small Android and iPhone databases and run the conversion end to end, no phones needed.

## Credits

wamove started from [wabridge](https://github.com/parvesh-rm/wabridge) by Parvesh Kumar (GPL-3.0-or-later), whose ChatStorage writer, crypt15 reader and msgstore parser it adapts. Changes made in September 2026: a WhatsApp-only restore instead of a full one, encrypted backup support, corrected media paths, LID-aware group members and a trimmed command line. It also builds on:

- [green2blue](https://github.com/discordwell/green2blue) (MIT) for how new files are added to an encrypted backup
- [Jalquin's migration notes](https://github.com/Jalquin/whatsapp-android-ios-migration-notes) for the WhatsApp-only restore and the media path layout
- [oath](https://github.com/rohancodesss/oath) for the backup record rules
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) for talking to the iPhone

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
