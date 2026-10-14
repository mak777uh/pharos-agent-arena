#!/bin/sh
# Regenerates sample_output.txt / sample_output.json (real Docs MCP + Pharos mainnet runs, ~3 min).
echo "\$ python builder_agent.py --examples examples.json --json sample_output.json" > sample_output.txt
python builder_agent.py --examples examples.json --json sample_output.json >> sample_output.txt 2>&1
