# USB SSD boot

The repo names **boot from USB SSD, not SD** as a security invariant
(`README.md`, `HANDOFF.md`). CDR logging plus astdb writes will kill an SD
card. This is how to get there from a working SD install.

Session 3 found the Pi still on `/dev/mmcblk0p2` with no USB disk attached.

## Do the PSU first

`vcgencmd get_throttled` bits are **sticky since boot**, so a fresh power
supply changes nothing until you reboot. Reboot, then:

    vcgencmd get_throttled     # 0x0 is clean

Bits of interest: `0x1` under-voltage now, `0x10000` under-voltage has
occurred, `0x40000` throttling has occurred. Session 3 saw `0x50000`.

If it still trips on a known-good 5V/3A brick, suspect the **cable** — thin
USB-C cables drop enough voltage on their own to trigger it.

Do not clone onto a brownout-prone Pi. You will get a corrupt copy of a
working system and spend an evening working out which half is lying.

## Don't clone from Windows

Windows cannot read ext4, so the only option there is a raw sector clone
(read SD to `.img`, write `.img` to SSD). It works, and it hands you three
problems:

- **Stranded capacity.** A raw clone reproduces the SD's partition table
  verbatim. A 32GB image on a 500GB SSD gives you a 32GB root. Fixing it
  needs `parted` + `resize2fs` — back on the Pi.
- **Duplicate PARTUUIDs.** The serious one. `cmdline.txt` resolves root by
  `root=PARTUUID=`. A raw clone gives both disks the same PARTUUID, so with
  both inserted you can boot the SSD's kernel against the SD's root
  filesystem and not notice until your changes start disappearing.
- It copies every sector including free space, via a full-size intermediate
  file.

Windows will also offer to format the ext4 partition it cannot identify.
Decline.

None of this saves a Pi session anyway — the EEPROM boot order can only be
changed from the Pi.

## Clone on the Pi

Stop Asterisk first so the astdb sqlite file is not copied mid-write:

    sudo systemctl stop asterisk

Then:

    sudo apt install rpi-clone
    sudo rpi-clone sda

`rpi-clone` copies at filesystem level, resizes the root partition to fill
the target, and rewrites `/etc/fstab` and `/boot/firmware/cmdline.txt` with
the **new** PARTUUIDs. That last part is what removes the ambiguity a raw
clone creates.

The desktop image's SD Card Copier does the same job if you have a screen
attached.

## Set the boot order

    sudo rpi-eeprom-config --edit

Set:

    BOOT_ORDER=0xf14

Nibbles are read right-to-left: `4` = USB mass storage, `1` = SD card,
`f` = restart the loop. So `0xf14` is USB-then-SD, which keeps the SD as a
fallback. The common factory default is `0xf41` — SD first.

If the editor refuses `0xf14`, the bootloader is too old:

    sudo rpi-eeprom-update -a

Then reboot.

## Verify

    findmnt /                  # want /dev/sda2
    vcgencmd get_throttled     # want 0x0
    lsblk -o NAME,SIZE,MODEL,TRAN

**Pull the SD card once it boots clean off USB.** Keep it on a shelf as a
rollback image; do not leave it in the slot.

Restart Asterisk and confirm the state store survived:

    sudo systemctl start asterisk
    sudo asterisk -rx "database show cnam"

## If the clone stalls partway

Some USB-SATA bridge chipsets drop offline under sustained writes — which is
precisely the corruption you are migrating away from. Identify the bridge:

    lsusb

If it is a known-bad bridge, add a quirks entry to the **single line** in
`/boot/firmware/cmdline.txt` (space-separated, no newlines):

    usb-storage.quirks=VVVV:PPPP:u

using the vendor:product IDs from `lsusb`. The `u` flag disables UAS and
falls back to plain BOT, which is slower and does not hang.

## Unverified here

This procedure was written from the session 3 findings and has not been
executed on this Pi. The `rpi-clone` fstab/cmdline rewrite in particular is
worth spot-checking by hand before the first reboot:

    sudo cat /mnt/clone/etc/fstab
    sudo cat /mnt/clone/boot/firmware/cmdline.txt

Both should reference the SSD's PARTUUIDs, not the SD's. Compare against
`blkid`.