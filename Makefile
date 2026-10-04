.PHONY: doctor test-doctor test-evaluation

doctor:
	PYTHONPATH=src python3 -m foundation.doctor

test-doctor:
	python3 -m pytest tests/unit/foundation/test_doctor.py -v

test-evaluation:
	python3 -m pytest tests/evaluation -v
	PYTHONPATH=src python3 -m retrieval_answering.evaluation \
		--baseline fixtures/evaluation/baseline.jsonl \
		--results fixtures/evaluation/baseline_results.jsonl \
		--output build/evaluation-report.json
	@! PYTHONPATH=src python3 -m retrieval_answering.evaluation \
		--baseline fixtures/evaluation/baseline.jsonl \
		--results fixtures/evaluation/below_profile_results.jsonl \
		--output build/below-profile-report.json
