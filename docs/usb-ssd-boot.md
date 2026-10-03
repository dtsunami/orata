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
    findmnt /boot/firmware     # want /dev/sda1
    vcgencmd get_throttled     # want 0x0
    lsblk -o NAME,SIZE,MODEL,TRAN

If `/` is still `/dev/mmcblk0p2` but `/boot/firmware` is `/dev/sda1`, the
firmware **did** boot off USB — the SSD's `cmdline.txt` just points `root=`
at the SD's PARTUUID. That is the silent mismatch described below, not a
boot-order problem, and no error is printed either way.

**Pull the SD card once it boots clean off USB.** Keep it on a shelf as a
rollback image; do not leave it in the slot.

Restart Asterisk and confirm the state store survived:

    sudo systemctl start asterisk
    sudo asterisk -rx "database show cnam"

## If the clone stalls partway

**Check the link speed before reaching for quirks.** A USB 2 link presents
exactly like a flaky bridge — slow, and likelier to stall under sustained
writes.

    lsusb -t

You want the disk on a **5000M** link. If it reads 480M, fix that first: a
blue USB 3 port directly on the Pi, no hub, and a different cable. A USB 2
link was the actual trigger here; adding a quirks entry at that point would
have masked a cable problem rather than fixed it.

Only once the link is 5000M and it *still* stalls is the bridge suspect. Some
USB-SATA bridge chipsets drop offline under sustained writes — which is
precisely the corruption you are migrating away from. Identify the bridge:

    lsusb

If it is a known-bad bridge, add a quirks entry to the **single line** in
`/boot/firmware/cmdline.txt` (space-separated, no newlines):

    usb-storage.quirks=VVVV:PPPP:u

using the vendor:product IDs from `lsusb`. The `u` flag disables UAS and
falls back to plain BOT, which is slower and does not hang.

## If the bootloader can't see the disk, try the other port

Reported empirically in session 4: the same SSD in the same enclosure would
not boot from one port and booted first try from another. No diagnosis beyond
that. **If the disk is invisible at boot, move it before you debug anything
else** — this swallowed hours that went into EEPROM and clone theories.

Candidate causes, none confirmed here:

- the blue USB 3.0 and black USB 2.0 ports enumerate differently, and some
  SATA bridges only negotiate cleanly on one of them
- marginal power delivery, especially with a bus-powered 2.5" drive
- a bad socket or cable on one side

After it boots, `lsblk -o NAME,SIZE,MODEL,TRAN` and `dmesg | grep -i usb`
will say which link actually came up and whether it fell back.

Note this interacts with the PSU section: a port that browns out under a
drive's spin-up load looks exactly like a port that doesn't support boot.

## The UAS quirk that is already live

`/boot/firmware/cmdline.txt` on this Pi carries:

    usb-storage.quirks=0781:558c:u

`0781:558c` is the SanDisk bridge in the attached Extreme Portable SSD, and
`:u` disables UAS per the "clone stalls partway" section above. Who added it
is not recorded, but it is load-bearing — do not strip it while tidying
`cmdline.txt`.

It is probably the same fault as the port swap rather than a second one. A
bridge that only enumerates on one port *and* needs UAS disabled is one
marginal link presenting twice. The two sections above are written as separate
problems; on this hardware they were likely not.

## Status — verified session 4

Confirmed by execution, SD card **out of the slot** (no `mmcblk*` present, so
this is not the both-inserted case that proves nothing):

    findmnt /          ->  /dev/sda2  ext4  rw,noatime
    lsblk              ->  sda  931.5G  SanDisk Extreme Portable SSD  usb
    blkid              ->  only sda1/sda2 exist; no second disk, no
                           duplicate PARTUUID
    df -h /            ->  916G total, 8.5G used, 1% — full-disk root,
                           not stranded at the SD's size
    vcgencmd get_throttled -> 0x0

So all three of the old open items are closed: it really is booting off USB,
the root filesystem really was resized to fill the SSD, and the PSU problem is
gone (`0x50000` -> `0x0`, clean since last boot).

`/etc/fstab` and `cmdline.txt` agree on `PARTUUID=af8a94c4-02`, and that is
the only disk in `blkid`, so the duplicate-PARTUUID trap does not apply.

**Still not recorded:** whether the SSD was `rpi-clone`d from the SD or
flashed fresh with rpi-imager. `cmdline.txt` contains an
`i=rpi-imager-1790647557738` tag, but `rpi-clone` copies that string through
from the source disk, so it does not settle the question either way.

### Pre-reboot spot-check — keep this for the next clone

Recorded in session 3 and still the sharpest trap here: `rpi-clone` was
observed rewriting `/etc/fstab` correctly **while leaving `cmdline.txt`
pointing at the SD**. That combination boots the SSD's kernel against the SD's
root filesystem and gives no error. Treat the two files as **two independent
checks**, not one.

`/boot/firmware` is a **separate FAT partition** (`sda1`), not part of the
root filesystem. Mounting `sda2` alone leaves `/mnt/clone/boot/firmware`
empty, so the `cat` returns nothing — which reads like a passing check rather
than a skipped one. Mount both:

    sudo mkdir -p /mnt/clone
    sudo mount -o ro /dev/sda2 /mnt/clone
    sudo mount -o ro /dev/sda1 /mnt/clone/boot/firmware
    sudo cat /mnt/clone/etc/fstab
    sudo cat /mnt/clone/boot/firmware/cmdline.txt
    sudo umount /mnt/clone/boot/firmware /mnt/clone

Both should reference the SSD's PARTUUIDs, not the SD's. Compare against
`blkid`.

On the currently running system this check has since passed — `/etc/fstab`
and `cmdline.txt` both name `PARTUUID=af8a94c4-02`, and `blkid` reports only
one disk. Whether the earlier mismatch was corrected or the SSD was
ultimately flashed fresh is not recorded.
