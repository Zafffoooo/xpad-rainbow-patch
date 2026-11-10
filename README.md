# XPAD Rainbow temporary RAM patcher

`xpad_rainbow_patch.py` temporarily changes one early built-in entry in the
already-loaded alpha `xpad.sprx` VSH module:

```text
Logitech Chillstream: 046d:c242
                  to: 413d:2104 (Rainbow 2 Pro XInput dongle)
```

Only four bytes in XPAD's writable VSH module data segment are changed. The
plugin on disk, flash, LV1, LV2, and boot plugin lists are never modified.
The patch is lost on unload or reboot.

## Prerequisites

- Python 3 on the LAN computer; only the standard library is used.
- webMAN MOD loaded on the PS3 with its HTTP service and PS3MAPI enabled.
- The `xpad.sprx` VSH plugin from **mrkoelie's PS3xPAD Beta Release** ([PSX-Place thread](https://www.psx-place.com/threads/ps3xpad-beta-release.50533/#post-427470)) copied to `/dev_hdd0/plugins/xpad.sprx`.
- An empty VSH slot, by default slot 6. The script refuses an occupied slot;
  manually unload a known test plugin first if necessary.
- The Rainbow dongle **physically disconnected** while running either command.
- Do not create `/dev_hdd0/plugins/xpad_auto_inject.txt`.

The script loads XPAD itself into the selected empty slot. It does not unload
anything automatically, including XPAD after a dry run.

## Dry run first

From the repository root:

```sh
python3 xpad_rainbow_patch.py 192.168.1.41 --dry-run --slot 6
```

The offline parser checks make no network connection:

```sh
python3 xpad_rainbow_patch.py --self-test
```

To print the exact PS3MAPI request, selected parser, and raw response before
each parse during a dry run, add `--trace-api`.

This loads XPAD, dynamically identifies the VSH PID and the module matching
`/dev_hdd0/plugins/xpad.sprx`, obtains module segment ranges, reads only the
known writable data segment in sequential 256-byte reads, finds `046DC242`
exactly once across the complete file-backed segment, and verifies 16 bytes
before a write. XPAD's table entries are eight bytes: big-endian `u16 VID`,
`u16 PID`, then a `u32` name pointer. It performs no memory write.

Success must show exactly one match and a pre-patch prefix of:

```text
04 6D C2 42
```

## Real temporary patch

After a successful dry run, first manually unload XPAD from the selected slot
using webMAN, so the next run gets a fresh ten-second initialization delay.
Keep the dongle disconnected, then run:

```sh
python3 xpad_rainbow_patch.py 192.168.1.41 --slot 6 --patch --confirm-dongle-disconnected
```

The script allows only `41 3D 21 04` as a write payload. webMAN's legacy
`/setmem.ps3mapi` command returns its normal HTML page, which is treated only
as confirmation that the one request completed—not as proof of a write. The
script immediately performs a fresh `/getmem.ps3mapi` read-back and requires
both the new four-byte VID/PID and the unchanged following four-byte name
pointer before printing `PATCH VERIFIED`. It also refuses to write if discovery
exceeds eight seconds after the plugin-load request, leaving margin before
XPAD's ten-second USB-init delay ends.

Wait until XPAD's ten-second startup delay has elapsed, then connect the
Rainbow dongle in XInput mode. The alpha's existing `XTYPE_XBOX360` parser
should use it. The on-screen/log display name will remain Logitech Chillstream
Controller; only the table VID/PID was substituted.

## Recovery

Unload the selected VSH slot through webMAN or reboot the PS3. Rebooting always
removes the in-memory patch. No persistent system file was changed.

If any script step says `ABORT`, it made no write unless the message follows
`SETMEM request completed`; in that exceptional case reboot, because only the
following read-back can prove whether the request changed memory.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0) - see the [LICENSE](LICENSE) file for details.

## Credits & Acknowledgements

- **PS3xPAD:** OsirisX and [aldostools/PS3xPAD](https://github.com/aldostools/PS3xPAD).
- **PS3xPAD Beta / Alpha Build:** Modded menu-free build by **mrkoelie** ([PSX-Place Release Thread](https://www.psx-place.com/threads/ps3xpad-beta-release.50533/#post-427470)).
- **webMAN MOD:** Aldo Vargas ([aldostools](https://github.com/aldostools/webMAN-MOD)) for the PS3MAPI and HTTP interface.

