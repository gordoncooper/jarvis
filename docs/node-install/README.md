# Node install

Two Ubuntu Server 26.04 autoinstall profiles for a blank node, used before `tools/prep-node`. They name no host. Copy the profile you want onto the install media as `autoinstall.yaml`, and copy `tools/node-install-storage` to that same media as `node-install-storage` (next to the `casper` directory).

Do not add `autoinstall` to the kernel command line. On Subiquity 26.04 that parameter is what skips the confirmation prompt. Leave it unset. The early-command only probes and writes the storage config. Curtin changes disks after you answer the confirmation prompt.

Replace `REPLACE_WITH_SSH_PUBLIC_KEY` before booting the installer. The login user is `ubuntu`, the password is locked (`!`, not a credential), and OpenSSH is installed. A sudoers drop-in lets that user sudo without a password. `tools/prep-node` prompts for a sudo password when the account does not already have passwordless sudo. The hostname `ubuntu` is temporary; prep-node records the real name.

## Profiles

`autoinstall-wipe.yaml` reformats the only non-removable disk that is not the install medium. It refuses if that rule matches any other count.

`autoinstall-dualboot.yaml` uses the only non-removable disk that has an EFI System partition and an NTFS filesystem. Delete the old Linux partitions on that disk (ext4, swap, Linux LVM, and the other Linux type GUIDs) before you boot the installer. If any remain, the early-command stops the install and does not change the disk. EFI, Microsoft reserved, NTFS, and Recovery stay, and the existing EFI filesystem is not formatted. It refuses if the rule does not match exactly one disk.

A second disk that also has an EFI System partition is not chosen unless it also has NTFS. Disks that are not chosen are absent from the storage config, so the installer does not partition them.

## Layout

EFI 1 GiB on a wipe (dual-boot reuses the ESP already there), `/boot` 2 GiB ext4, LVM `vg-os` with `lv-root` 60 GiB at `/` and `lv-data` ext4 `LABEL=jarvis-data` mounted at `/var/lib/jarvis` with `defaults,nofail`, about 10% of the volume group left free, no swap. The installer's `G` suffix is 2^30 bytes (framework §6).

Guided `layout: {name: lvm}` cannot name the volume group or the logical volumes, and it does not make a 60 GiB root. The early-command writes an explicit storage config and does not partition the disk. Subiquity re-reads `/autoinstall.yaml` after early-commands. Curtin applies that config only after the confirmation prompt. The storage block shipped in each profile matches nothing, so a skipped early-command does not pick a disk. `storage` must stay the last key in the file; the early-command replaces from that key to the end.

Dual-boot leaves Recovery out of the storage config. Subiquity emits partitions in number order, and curtin starts a new partition after the previous one in that list. Recovery would be that previous partition when it has a lower number and sits at the end of the disk. The preserved partition table keeps Recovery where it is. Curtin creates `/boot` and the LVM partition in the gap.
