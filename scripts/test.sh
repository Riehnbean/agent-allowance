#!/bin/sh
# Run every offline test (no network, no keys needed).
cd "$(dirname "$0")/.." && exec python3 -W ignore::ResourceWarning -m unittest -b \
  tests.test_allowance tests.test_break tests.test_break_server tests.test_codex_review tests.test_nov1_review \
  tests.test_nov1_verify tests.test_indep tests.test_r3_provider tests.test_r3_normalizer tests.test_r3_concurrency \
  tests.test_watchdog tests.test_fx tests.test_wd_approve tests.test_verify_payee tests.test_r4_review tests.test_r5_review
