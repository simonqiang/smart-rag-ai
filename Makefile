.PHONY: doctor test-doctor

doctor:
	PYTHONPATH=src python3 -m foundation.doctor

test-doctor:
	python3 -m pytest tests/unit/foundation/test_doctor.py -v
