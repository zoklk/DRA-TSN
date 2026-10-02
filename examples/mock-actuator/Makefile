# Build the actuator XDP reflect object (examples/mock-actuator).
# Requires: clang (>=11) + libbpf headers (libbpf-dev) + kernel uapi headers.
# Standalone — does NOT use the repo's bpf2go pipeline (this is not data-plane code).
CLANG ?= clang
TARGET := reflect.bpf.o
SRC    := reflect.bpf.c
HDR    := reflect.h

# Multiarch include so the uapi headers find <asm/types.h>.
UNAME_M := $(shell uname -m)
ARCH    := $(shell echo $(UNAME_M) | sed 's/x86_64/x86/;s/aarch64/arm64/')

# Override echo addressing/tag without editing reflect.h, e.g.:
#   make CFLAGS_EXTRA='-DECHO_VID=200 -DECHO_PCP=3'
CFLAGS_EXTRA ?=

CFLAGS := -O2 -g -Wall -target bpf -D__TARGET_ARCH_$(ARCH) \
          -I/usr/include/$(UNAME_M)-linux-gnu $(CFLAGS_EXTRA)

$(TARGET): $(SRC) $(HDR)
	$(CLANG) $(CFLAGS) -c $(SRC) -o $(TARGET)

.PHONY: clean
clean:
	rm -f $(TARGET)
