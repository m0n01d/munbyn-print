# Native CUPS filter for the Munbyn RW403B: CUPS raster -> verified TSPL subset.
#
#   make              build ./rastertotspl (universal: arm64 + x86_64)
#   make OUT=/x/y     build to another path (the tests do this)
#   make test-local   build, then run tests/test_cups_filter.py (no printer,
#                     no install; uses cupsfilter when available)
#   make clean
#
# Installing is scripts/install-cups-queue.sh (run with sudo by a human).

ifeq ($(origin CC),default)
CC = clang
endif
SDK     ?= $(shell xcrun --show-sdk-path 2>/dev/null)
ARCHS   ?= -arch arm64 -arch x86_64
MINOS   ?= -mmacosx-version-min=12.0
CFLAGS  ?= -O2 -g
WARN     = -Wall -Wextra -Wno-deprecated-declarations
SYSROOT  = $(if $(SDK),-isysroot $(SDK))
LIBS     = -lcups -lcupsimage -lm
OUT     ?= rastertotspl

.PHONY: all clean test-local

all: $(OUT)

$(OUT): rastertotspl.c Makefile
	$(CC) $(ARCHS) $(MINOS) $(SYSROOT) $(CFLAGS) $(WARN) -o $@ rastertotspl.c $(LIBS)

test-local: $(OUT)
	cd .. && .venv/bin/pytest -q --timeout=300 tests/test_cups_filter.py

clean:
	rm -rf $(OUT) $(OUT).dSYM
