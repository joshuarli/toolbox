CC ?= cc
PORT ?= 8080
DARKHTTPD = web/bin/darkhttpd
DARKHTTPD_SRC = web/vendor/darkhttpd/darkhttpd.c
SITE_ASSETS = web/assets/site.css web/assets/site.js web/assets/carousel.svg web/assets/play.svg

.PHONY: fmtlint test sync-py build-site site

fmtlint:
	uvx ruff@latest check --fix --unsafe-fixes
	uvx ruff@latest format
	uvx ty@latest check

test:
	# joshuarli-pytest-parallel (dev dependency) auto-shards across cpu_count workers
	uv run pytest

# Install the optional browser automation dependency.
sync-py:
	uv sync --extra browser

build-site: instagram.db $(SITE_ASSETS)
	uv run tool ig build
	@touch site

$(DARKHTTPD): $(DARKHTTPD_SRC)
	@mkdir -p web/bin
	@if [ "$$(uname -s)" = "Darwin" ]; then \
		$(CC) -O3 $(CFLAGS) $(LDFLAGS) $< -o $@; \
	else \
		$(CC) -O3 -static $(CFLAGS) $(LDFLAGS) $< -o $@ || \
		$(CC) -O3 $(CFLAGS) $(LDFLAGS) $< -o $@; \
	fi

site: build-site $(DARKHTTPD)
	@printf 'Serving site/ at http://127.0.0.1:%s/\n' "$(PORT)"
	./$(DARKHTTPD) site --addr 127.0.0.1 --port $(PORT) --no-listing --default-mimetype text/html
