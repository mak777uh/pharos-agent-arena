#!/bin/sh
# Regenerates the sample outputs in this folder (real Pharos mainnet runs).
W=0xd27cb53f5920d484261cc14d32094d3f043265c9     # USDC in Avalon + 2 SharpByte vaults, WPROS in the Vishwa vault
I=0x84dbd23178ba7fc6a2a396559139aef69d0f072a     # holds idle USDC in the wallet
run() { out=$1; shift; echo "\$ python aggregator.py $*" > "$out"; python aggregator.py "$@" >> "$out" 2>&1; }
run sample_rates_usdc.txt rates --token USDC --json sample_rates_usdc.json
run sample_positions.txt positions --wallet $W --token USDC --token WPROS
run sample_deposit.txt ask $I "deposit 5000 USDC" --exclude syzUSD --prepare prepared
run sample_withdraw.txt withdraw --wallet $W --token USDC --amount 100000 --prepare prepared
run sample_outage_rates.txt rates --token USDC --simulate-outage avalon
run sample_outage_deposit.txt deposit --wallet $I --token USDC --amount 5000 --exclude syzUSD --simulate-outage vault
run sample_ask_unknown.txt ask $W "swap my USDC for ETH on a DEX"
