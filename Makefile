PY := .venv/bin/python

.PHONY: test cluster evidence agent

test:
	$(PY) -m pytest -q

cluster:
	$(PY) scripts/run_cluster.py --reset

evidence:
	$(PY) scripts/wire_evidence.py
	$(PY) scripts/load_test.py
	$(PY) scripts/registry_check.py

agent:
	$(PY) scripts/demo_agent.py
