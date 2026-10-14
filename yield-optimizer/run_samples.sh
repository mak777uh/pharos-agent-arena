#!/bin/sh
# Regenerates the sample outputs in this folder (real mainnet runs; ~2 min each).
W=0xd27cb53f5920d484261cc14d32094d3f043265c9
run() { out=$1; shift; echo "\$ python yield_optimizer.py $*" > "$out"; python yield_optimizer.py "$@" >> "$out" 2>&1; }
run sample_wallet_usdc.txt --token USDC --wallet $W --prepare prepared --json sample_wallet_usdc.json
run sample_short_hold.txt --token USDC --wallet $W --hold-days 3 --min-gain-usd 25
run sample_wpros.txt --token WPROS --wallet $W --hold-days 14
run sample_idle_usdc.txt --token USDC --amount 25000 --exclude syzUSD
