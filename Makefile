# Entry points. The lab is `bin/lab`; this is where "which script do I run"
# goes to die.

LAB      ?= ./bin/lab
NAME     ?= lab
NODES    ?= 1
DISKS    ?= 4
PROFILE  ?= ./profiles/zfs
PROFILES ?= $(PROFILE)
PYTEST   ?=

.PHONY: help
help:
	@echo "make image                  build the base image (once, ~10 min)"
	@echo "make up NODES=2             bring a lab up"
	@echo "make down                   stop it      (RESET=1 to discard disks)"
	@echo "make ssh NODE=2             shell into a node"
	@echo "make status                 what is running"
	@echo "make test PROFILE=./profiles/zfs"
	@echo "make matrix                 every profile, plus the cross pairs"
	@echo "make check                  syntax-check everything"

.PHONY: image
image:
	bash lab/build-image.sh

.PHONY: up
up:
	$(LAB) up --name $(NAME) --nodes $(NODES) --disks $(DISKS)

.PHONY: down
down:
	$(LAB) down --name $(NAME) $(if $(RESET),--reset,)

.PHONY: ssh
ssh:
	$(LAB) ssh --name $(NAME) --node $(or $(NODE),1)

.PHONY: status
status:
	$(LAB) status --name $(NAME)

.PHONY: test
test:
	$(LAB) run --name $(NAME) --nodes $(NODES) \
	    $(foreach p,$(PROFILES),--profile-dir $(p)) \
	    $(if $(PYTEST),-- $(PYTEST),)

# Every backend on its own, and every ordered pair against each other. Hours,
# deliberately - this is a release gate, not something to run on a change.
.PHONY: matrix
matrix:
	$(LAB) run --name $(NAME) --nodes 2 --cross \
	    --profile-dir ../pve-bcachefs/test/profile \
	    --profile-dir ./profiles/zfs \
	    --profile-dir ./profiles/btrfs \
	    --profile-dir ./profiles/dir \
	    --profile-dir ./profiles/lvm-thin

.PHONY: check
check:
	@for f in lab/*.sh lib/*.sh labkit/remote/*.sh profiles/*/*.sh; do \
	    [ -e "$$f" ] || continue; bash -n "$$f" || exit 1; done
	@python3 -m compileall -q labkit suite tools >/dev/null
	@python3 -m py_compile bin/lab
	@echo "shell and python parse"
