# Answer file for Proxmox's automated installer.
# Rendered by lab/build-image.sh; @PLACEHOLDERS@ are substituted there.
#
# Keys are kebab-case: the underscore spelling has been deprecated since
# PVE 8.4-1 and the tooling now rejects it outright.

[global]
keyboard = "en-us"
country = "at"
fqdn = "@FQDN@"
mailto = "root@localhost"
timezone = "UTC"
root-password = "@ROOT_PASSWORD@"
root-ssh-keys = ["@ROOT_SSH_KEY@"]
# Reboot on failure too. The build runs QEMU with -no-reboot, so either
# outcome ends as a clean exit and the build fails fast at the first-boot
# stage instead of sitting on an error screen until the timeout.
reboot-on-error = true

[network]
source = "from-dhcp"

[disk-setup]
filesystem = "ext4"
# The lab always presents the system disk first and as virtio, so this is
# stable regardless of how many extra test devices are attached.
disk-list = ["vda"]
