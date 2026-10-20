.PHONY: test install uninstall

test:
	bash -n incus-gpu install.sh uninstall.sh tests/test.sh
	bash tests/test.sh
	python3 -S -m unittest discover -s tests -p 'test_*.py' -v

install:
	./install.sh

uninstall:
	./uninstall.sh
