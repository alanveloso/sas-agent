# Frozen article campaign helpers (optional).
# Prefer: .venv/bin/python benchmarks/run_article_campaign.py

.PHONY: test article-smoke article-eval tables figures

test:
	.venv/bin/python -m unittest discover -s tests -v

article-smoke:
	.venv/bin/python benchmarks/run_article_campaign.py --smoke

article-eval:
	.venv/bin/python benchmarks/run_article_campaign.py --allow-dirty

tables:
	.venv/bin/python analysis/generate_tables.py

figures:
	@echo "Pass --campaign-id explicitly:"; \
	 .venv/bin/python analysis/generate_figures.py --help
