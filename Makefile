# Короткі команди. Все, що треба, робиться звідси.
PY := .venv/bin/python
PIP := .venv/bin/pip
export PYTHONPATH := src

.PHONY: help setup run smoke test calibrate brands clean

help:
	@echo "make setup      - створити venv і поставити залежності"
	@echo "make run        - запустити бота"
	@echo "make smoke      - 3 справжні цикли без відправки в Telegram"
	@echo "make test       - тести"
	@echo "make calibrate  - показати, наскільки базові ціни розійшлись з ринком"
	@echo "make brands     - перезібрати кеш id брендів після правки brands.yaml"
	@echo "make clean      - прибрати кеші та локальну базу"

setup:
	python3 -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt
	$(PIP) install --quiet pytest
	@test -f .env || (cp .env.example .env && echo "Створив .env, впиши туди TELEGRAM_BOT_TOKEN")
	@echo "Готово. Далі: make run"

run:
	$(PY) -m vintsniper

smoke:
	$(PY) scripts/smoke_test.py 3

test:
	$(PY) -m pytest tests/ -q

calibrate:
	$(PY) scripts/calibrate.py

brands:
	$(PY) scripts/resolve_brands.py

clean:
	rm -rf .pytest_cache .ruff_cache data/vintsniper.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "Прибрано. Конфіги і .env не чіпав."
