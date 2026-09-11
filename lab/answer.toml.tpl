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
# Static, not from-dhcp. The lab network is always the same user-mode net, so
# there is nothing to discover - and a DHCP address is actively harmful here:
# the host forwards its SSH port to one fixed guest address, and any test guest
# that bridges onto vmbr0 can take that address out from under the node.
source = "from-answer"
cidr = "@NODE_CIDR@"
gateway = "@GATEWAY@"
dns = "@DNS@"
# Match on the MAC-derived name so the filter survives a PCI topology change,
# for the same reason the installed system names its NIC that way.
filter.ID_NET_NAME_MAC = "@IFNAME@"

[disk-setup]
filesystem = "ext4"
# The lab always presents the system disk first and as virtio, so this is
# stable regardless of how many extra test devices are attached.
disk-list = ["vda"]
