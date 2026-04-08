PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: all venv install run clean help

all: install

help:
	@echo "Usage:"
	@echo "  make            — create venv + install deps (default)"
	@echo "  make venv       — create virtual environment only"
	@echo "  make install    — create venv + install package"
	@echo "  make run        — run agenc (installs first if needed)"
	@echo "  make clean      — remove virtual environment"
	@echo ""
	@echo "Override python:  make PYTHON=python3.11"

venv: $(BIN)/activate

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: $(BIN)/activate
	$(BIN)/pip install -e .

run: install
	$(BIN)/python agent.py

clean:
	rm -rf $(VENV)PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: all venv install run clean help

all: install

help:
	@echo "Usage:"
	@echo "  make            — create venv + install deps (default)"
	@echo "  make venv       — create virtual environment only"
	@echo "  make install    — create venv + install deps"
	@echo "  make run        — run agenc (installs first if needed)"
	@echo "  make clean      — remove virtual environment"
	@echo ""
	@echo "Override python:  make PYTHON=python3.11"

venv: $(BIN)/activate

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: $(BIN)/activate
	$(BIN)/pip install openai rich

run: install
	$(BIN)/python agent.py

clean:
	rm -rf $(VENV)
