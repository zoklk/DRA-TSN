# Build the mock-vPLC busy-poll probe as a STATIC binary so it runs in any pod
# image (kubectl cp + exec into netshoot, no toolchain in the container).
CC      ?= cc
TARGET  := mock_vplc
SRC     := mock_vplc.c
CFLAGS  ?= -O2 -Wall -Wextra
LDFLAGS ?= -static

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $(TARGET) $(SRC) $(LDFLAGS)

# musl build (smaller, fully static even where glibc -static warns):
#   make CC=musl-gcc
.PHONY: clean
clean:
	rm -f $(TARGET)
