.PHONY: test install uninstall

test:
	bash -n incus-gpu install.sh uninstall.sh tests/test.sh
	bash tests/test.sh
	python3 -S -m unittest discover -s tests -p 'test_*.py' -v

install:
	./install.sh

uninstall:
	./uninstall.sh

.PHONY: test-gpu-mock test-all

test-gpu-mock:
	bash tests/run-gpu-mock.sh

test-all: test test-gpu-mock
