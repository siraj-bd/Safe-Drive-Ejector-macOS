.PHONY: all build test clean run-cli run-menubar run-daemon install-mac

all: build test

build:
	mkdir -p bin .build/cache
	swiftc -module-cache-path .build/cache -O ui/SafeEjectMenuBar.swift -o bin/SafeEjectMenuBar
	chmod +x bin/SafeEjectMenuBar main.py run_menubar_mac.sh install.sh

test:
	python3 -m unittest discover tests

run-cli:
	./main.py list

run-menubar: build
	./run_menubar_mac.sh

run-daemon:
	./main.py daemon

install-mac: build
	./install.sh

clean:
	rm -rf bin .build .safe_eject_config __pycache__ core/__pycache__ platform_adapters/__pycache__ ui/__pycache__ tests/__pycache__
