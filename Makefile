.PHONY: test install uninstall

test:
	bash -n incus-gpu install.sh uninstall.sh tests/test.sh
	./tests/test.sh

install:
	./install.sh

uninstall:
	./uninstall.sh
