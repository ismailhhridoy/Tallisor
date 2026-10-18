PY ?= python3

.PHONY: install sample test eval app run clean

install:
	$(PY) -m pip install -r requirements.txt

sample:
	$(PY) scripts/make_sample.py

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m tallisor eval --k 3

run:
	$(PY) -m tallisor run

app:
	$(PY) -m streamlit run app.py

clean:
	rm -rf .pytest_cache __pycache__ */__pycache__ */*/__pycache__
