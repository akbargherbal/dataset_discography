PYTHON ?= python3
CONFIG ?= config/settings.yaml
BUILD ?= build
SOURCES ?= $(BUILD)/sources
DATASET ?= $(BUILD)/dataset/v0.1
LOCAL_DATASET ?= dataset/v0.1
GCS_DEST ?= gs://akbar-december-2024-backup/DATASET/v0.1

.PHONY: bootstrap test scan core links lyrics package validate report build publish clean

bootstrap:
	$(PYTHON) -m pip install -r requirements.txt
	ffprobe -version >/dev/null
	@echo "bootstrap ok"

test:
	$(PYTHON) -m pytest -q

scan:
	$(PYTHON) -m src.scan_gcs --config $(CONFIG) --out $(BUILD)/manifest
	$(PYTHON) -m src.scan_github --config $(CONFIG) --out $(BUILD)/manifest --sources-dir $(SOURCES)

core:
	$(PYTHON) -m src.build_core --config $(CONFIG) --manifest $(BUILD)/manifest \
		--out $(BUILD)/core --sources $(SOURCES)

links:
	$(PYTHON) -m src.link_exact --config $(CONFIG) --core $(BUILD)/core \
		--out $(BUILD)/links --manifest $(BUILD)/manifest --sources $(SOURCES)

lyrics:
	$(PYTHON) -m src.ingest_lyrics --config $(CONFIG) --core $(BUILD)/core \
		--out $(BUILD)/artifacts --links $(BUILD)/links --sources $(SOURCES)

package:
	$(PYTHON) -m src.package_dataset --config $(CONFIG) --build $(BUILD) --out $(DATASET)

validate:
	$(PYTHON) -m src.validate --config $(CONFIG) --dataset $(DATASET)

report:
	$(PYTHON) -m src.report --config $(CONFIG) --dataset $(DATASET)

build: scan core links lyrics package validate report

publish:
	$(PYTHON) -m src.publish --config $(CONFIG) --dataset $(DATASET) \
		--local-dir $(LOCAL_DATASET) --dest $(GCS_DEST)

clean:
	rm -rf $(BUILD)/core $(BUILD)/links $(BUILD)/artifacts $(BUILD)/dataset
