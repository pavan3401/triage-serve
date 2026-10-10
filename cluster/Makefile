VENV := .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
export PYTHONPATH := $(CURDIR)

.PHONY: setup sync ssh cluster smoke test repl harness app-harness observe dashboards locust crew kernel evict warmup

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements.txt
	@echo
	@echo "Mac:     make sync && make ssh"
	@echo "Lambda:  bash setup/lambda_setup.sh && bash setup/lambda_cluster.sh && make smoke"

sync:
	bash setup/sync_to_lambda.sh

ssh:
	bash setup/ssh.sh

cluster:
	bash setup/lambda_cluster.sh

smoke:
	bash setup/smoke_sliced.sh

test:
	$(PYTHON) -m pytest tests/

kernel:
	$(PYTHON) -m kernels.engineering --q 128 --kv 2048

evict:
	$(PYTHON) -m router.kv_eviction --policy prefix_protect --need 1024

warmup:
	$(PYTHON) -m router.warmup --budget 1500 --prefix 256

repl:
	$(PYTHON) -m gateway.repl

harness:
	$(PYTHON) -m gateway.harness --list

app-harness:
	$(PYTHON) -m app.harness --list

dashboards:
	$(PYTHON) -m observability.dashboards

observe:
	bash setup/day2_observability.sh

locust:
	$(PYTHON) -m pip install -q -r requirements-load.txt
	$(PYTHON) -m locust -f app/locustfile.py

crew:
	$(PYTHON) -m pip install -q -r requirements-load.txt
	$(PYTHON) -m app.crew_flood --rounds 24 --workers 8
