# Kobo reading-data uploader

Kobo firmware 4.x syncs annotations and reading progress, which CWA stores when
`CWA_KOBO_READING_DATA=true`. Some data never leaves the device over sync:
My Words, the Event counters behind Reading Life, and highlights in books
copied over USB. These files upload the device database to CWA after every
successful sync, so that data reaches the server too.

| File | Installed to | Role |
|---|---|---|
| `99-cwa-reading-data.rules` | `/etc/udev/rules.d/` | starts the watcher at boot |
| `watch-start.sh`, `watch.sh` | `/usr/local/cwa/` | polls `LastSync=` and runs the upload after each sync |
| `upload.sh`, `probe.sh` | `.adds/cwa/` (USB partition) | gzips `KoboReader.sqlite` and POSTs it to `/kobo/<token>/v1/cwa/device-db` |
| `nickelmenu-cwa` | `.adds/nm/cwa` (USB partition) | NickelMenu items: *Reading data status*, *CWA device probe* |

The upload uses the `api_endpoint` the Kobo already syncs with, so it needs no
configuration and stores no credentials. CWA must be reachable over plain HTTP,
because the device has no HTTPS-capable client. When CWA is not reachable (away
from home), the upload is recorded as skipped and nothing is retried; the
device keeps its data and the next sync at home sends it.

## Install

1. Build the root filesystem archive, using the official
   [NickelMenu release](https://github.com/pgaskin/NickelMenu/releases) `KoboRoot.tgz`:

   ```sh
   sh kobo-device/build-koboroot.sh ~/Downloads/NickelMenu/KoboRoot.tgz /tmp/KoboRoot.tgz
   ```

2. With the Kobo connected over USB (`/Volumes/KOBOeReader` on macOS):

   ```sh
   K=/Volumes/KOBOeReader
   mkdir -p "$K/.adds/cwa" "$K/.adds/nm"
   cp kobo-device/upload.sh kobo-device/probe.sh "$K/.adds/cwa/"
   cp kobo-device/nickelmenu-cwa "$K/.adds/nm/cwa"
   cp /tmp/KoboRoot.tgz "$K/.kobo/KoboRoot.tgz"
   ```

3. Eject. The Kobo installs the archive and reboots.

Firmware updates replace the root filesystem, so repeat step 1-3 (only the
`KoboRoot.tgz` copy is needed) after updating the Kobo.

## Check

* NickelMenu → *Reading data status* shows the last upload result.
* NickelMenu → *CWA device probe* writes `.adds/cwa/probe.txt` listing the
  tools the firmware provides.

## Turn off or remove

* Off: create an empty `.adds/cwa/disabled` on the USB partition. The watcher
  exits within a minute and does not start again while the file exists.
* NickelMenu: create `.adds/nm/uninstall` and reboot.
* The watcher's files in `/usr/local/cwa` and the udev rule stay on the root
  filesystem until the next firmware update; with `disabled` present they do nothing.
